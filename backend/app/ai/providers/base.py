"""供应商适配层：统一思考方言 → 各家协议参数。

统一方言（thinking）：off | low | medium | high | auto
- off   显式关闭思考（有开关语义的家发关闭参数，无开关语义的不发参数）
- low/medium/high  档位直传（该家枚举不含时由 adapter 只降不升就近替代）
- auto  开启思考、深度由模型自主（方舟原生 thinking.type=auto；其余家=开思考不控深度）

归一兼容（normalize_thinking）：True→high、False/None/""→auto、"thinking"→auto。
注意 None→auto（模型默认行为）而非 off：旧配置未设档位时保持"模型默认"，与历史行为一致。

降级三规则（2026-09 定稿）：
1. 请求值 ∈ 该家接受集 → 直传（服务端自映射，如 DeepSeek medium→high）
2. 不在 → 客户端只降不升就近替代（medium→low）
3. 能力整体不支持（如 GLM-5.3 强制思考无法关闭）→ 不发/最接近参数 + 降级说明

降级说明（DegradeNote）由 gateway 收进 last_meta["degrade"]，审计页可见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 统一档位序（只降不升的排序基准；auto 无序、不参与就近替代）
EFFORT_ORDER: dict[str, int] = {"off": 0, "low": 1, "medium": 2, "high": 3}

THINKING_VALUES = ("off", "low", "medium", "high", "auto")


@dataclass
class DegradeNote:
    """一条降级说明：请求了什么、实际发了什么、为什么。"""
    kind: str          # "thinking" | "reasoning_backfill" | ...
    requested: str
    applied: str
    reason: str

    def as_note(self) -> str:
        return f"{self.kind}: {self.requested}→{self.applied}（{self.reason}）"


def normalize_thinking(raw: Any) -> str:
    """任意历史形态 → 统一方言。未知值兜底 auto（模型自主，最不意外）。"""
    if raw is True:
        return "high"
    if raw is False:
        return "auto"          # 旧布尔 False=不控档位（非"显式关闭"），保持模型默认
    if raw is None or raw == "":
        return "auto"
    s = str(raw).strip().lower()
    if s in THINKING_VALUES:
        return s
    if s == "thinking":        # 旧 KB 特判值：开思考不控深度 ≈ auto
        return "auto"
    return "auto"


def map_effort(want: str, enum: tuple[str, ...], notes: list[DegradeNote],
               display: str, kind: str = "thinking") -> str | None:
    """档位映射（规则 1+2）：接受集直传；否则只降不升就近替代；无可替代返回 None。"""
    if want in enum:
        return want
    w = EFFORT_ORDER.get(want)
    if w is not None:
        candidates = [e for e in enum if (o := EFFORT_ORDER.get(e)) is not None and o <= w]
        if candidates:
            best = max(candidates, key=lambda e: EFFORT_ORDER[e])
            notes.append(DegradeNote(kind, want, best, f"{display} 档位集 {enum} 不含 {want}，只降不升"))
            return best
    notes.append(DegradeNote(kind, want, "off", f"{display} 无可接受档位，放弃控制"))
    return None


class ProviderAdapter:
    """适配器基类 = OpenAI 兼容 + reasoning_effort 直传（openai/custom 的默认行为）。

    子类按需覆盖：apply_thinking（思考参数构造）、reasoning_field（流式思考字段名）、
    needs_reasoning_backfill（思考+tools 是否强制回传历史思考链）。
    """

    name = "custom"
    display = "自定义（OpenAI 兼容）"
    effort_enum: tuple[str, ...] = ("low", "medium", "high")
    supports_auto = False       # 该家是否有"开思考不控深度/auto"语义（无则 auto→不发参数）
    supports_off = False        # 该家能否显式关思考（False 则 off→不发参数+降级说明）
    reasoning_field = "reasoning_content"
    needs_reasoning_backfill = False

    # ---- 思考参数构造：统一方言 → payload 增补；返回降级说明 ----
    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        if thinking in ("low", "medium", "high"):
            applied = map_effort(thinking, self.effort_enum, notes, self.display)
            if applied:
                payload["reasoning_effort"] = applied
        elif thinking == "auto":
            if self.supports_auto:
                # 基类无原生 auto 字段 → 不发参数（模型自主）；有原生 auto 的家覆盖本方法
                pass
        elif thinking == "off":
            if not self.supports_off:
                notes.append(DegradeNote("thinking", "off", "模型默认", f"{self.display} 无显式关闭语义，不发送控制参数"))
            # supports_off=False → 不发；有显式关闭语义的家覆盖本方法
        return notes

    # ---- 请求前消息修补：思考链回填（DeepSeek 思考+tools 缺回传直接 400）----
    def patch_messages(self, messages: list[dict], thinking: str, has_tools: bool,
                       last_reasoning: str | None) -> list[DegradeNote]:
        if not (self.needs_reasoning_backfill and thinking != "off" and has_tools and last_reasoning):
            return []
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("tool_calls") and not m.get("reasoning_content"):
                m["reasoning_content"] = last_reasoning
                return [DegradeNote("reasoning_backfill", "缺失", "已回填",
                                    f"{self.display} 思考+tools 必须回传历史思考链，否则 400")]
        return []

    # ---- caps 快照（前端展示 / 校准回填参考）----
    def caps(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "thinking_mode": "effort",
            "effort_enum": list(self.effort_enum),
            "supports_auto": self.supports_auto,
            "supports_off": self.supports_off,
            "reasoning_field": self.reasoning_field,
            "needs_reasoning_backfill": self.needs_reasoning_backfill,
        }
