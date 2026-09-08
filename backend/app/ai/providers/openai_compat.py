"""OpenAI 兼容族适配器：openai / deepseek / qwen / zhipu / ollama / custom。

caps 依据（2026-09 官方文档 + 实测校准，厂商改协议时优先改这里的声明）：
- OpenAI：reasoning_effort low/medium/high，仅 o 系列 / gpt-5 系列接受（按模型族门控；
  gpt-4o 等非推理模型发参数会被 400）；无 auto/关闭语义（由模型决定）
- DeepSeek：thinking {type: enabled/disabled}（默认开）+ reasoning_effort low/high/max
  （medium 官方服务端映射 high，直传）；思考+tools 必须回传历史 reasoning_content 否则 400
- Qwen：enable_thinking true/false + reasoning_effort（各模型枚举不同，直传；
  effort 与 thinking_budget 互斥 → 本层只用 effort，永不发 budget）
- 智谱 GLM：thinking {type: enabled/disabled}；reasoning_effort 仅 5.2+（5.2 全档，
  5.3/5.3-flash 仅 max/high/low 且强制思考不可关）→ 按模型名分派
- Ollama：think bool 开关（本地模型无档位细分，待校准）
"""
from __future__ import annotations

from app.ai.providers.base import DegradeNote, ProviderAdapter, map_effort


class OpenAIAdapter(ProviderAdapter):
    name = "openai"
    display = "OpenAI"
    effort_enum = ("low", "medium", "high")
    supports_auto = False
    supports_off = False

    # reasoning_effort 仅推理模型接受（官方文档：o 系列 / gpt-5 系列）；
    # 其余模型（gpt-4o 等）发该参数会被 400 → 门控 + 降级说明
    _EFFORT_PREFIX = ("o1", "o3", "o4", "gpt-5")

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        if thinking in ("low", "medium", "high"):
            m = (model or "").lower()
            if m.startswith(self._EFFORT_PREFIX):
                applied = map_effort(thinking, self.effort_enum, notes, self.display)
                if applied:
                    payload["reasoning_effort"] = applied
            else:
                notes.append(DegradeNote(
                    "thinking", thinking, "模型默认",
                    f"{model or '未知名模型'} 非 OpenAI 推理模型（o 系列/gpt-5），不发 reasoning_effort"))
        return notes


class CustomAdapter(OpenAIAdapter):
    """自定义供应商：唯一承诺 = OpenAI 兼容。保守假设 + 静默降级（被拒不重试、参数发最小集）。"""
    name = "custom"
    display = "自定义（OpenAI 兼容）"

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        # 自定义端点模型名五花八门（vllm 私有命名等），不做 OpenAI 模型族门控，effort 直传
        return ProviderAdapter.apply_thinking(self, payload, thinking, model, has_tools)


class DeepSeekAdapter(ProviderAdapter):
    name = "deepseek"
    display = "DeepSeek"
    # medium 在官方映射表里（medium→high 服务端处理），直传即算"接受"
    effort_enum = ("low", "medium", "high", "max")
    supports_auto = True       # thinking.type enabled = 开思考不控深度
    supports_off = True        # thinking.type disabled
    needs_reasoning_backfill = True

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        if thinking == "off":
            payload["thinking"] = {"type": "disabled"}
        elif thinking == "auto":
            payload["thinking"] = {"type": "enabled"}
        else:
            applied = map_effort(thinking, self.effort_enum, notes, self.display)
            if applied:
                # 与官方样例一致：effort 与 thinking.enabled 同发（思考开启是 effort 生效前提）
                payload["reasoning_effort"] = applied
                payload["thinking"] = {"type": "enabled"}
        return notes


class QwenAdapter(ProviderAdapter):
    name = "qwen"
    display = "通义千问"
    effort_enum = ("low", "medium", "high")   # 各模型枚举不同（xhigh 等不入统一方言），直传
    supports_auto = True       # enable_thinking true
    supports_off = True        # enable_thinking false（纯推理模型无法关 → 服务端忽略/报错，由校准发现）

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        # effort 与 thinking_budget 互斥（同传报错）→ 本层永不发 budget
        if thinking == "off":
            payload["enable_thinking"] = False
        elif thinking == "auto":
            payload["enable_thinking"] = True
        else:
            applied = map_effort(thinking, self.effort_enum, notes, self.display)
            payload["enable_thinking"] = True
            if applied:
                payload["reasoning_effort"] = applied
        return notes


class ZhipuAdapter(ProviderAdapter):
    name = "zhipu"
    display = "智谱 GLM"
    effort_enum = ("low", "medium", "high")   # 5.2 全档直传（low/medium 服务端映射 high）
    supports_auto = True       # thinking.type enabled（GLM 无原生 auto）
    supports_off = True        # thinking.type disabled（GLM-5.3 除外，见下）

    # GLM-5.3 / 5.3-flash：强制思考（disabled 不接受），effort 仅 max/high/low
    _FORCED_PREFIX = ("glm-5.3",)
    _FORCED_ENUM = ("low", "high", "max")

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        m = (model or "").lower()
        forced = m.startswith(self._FORCED_PREFIX)
        if thinking == "off":
            if forced:
                notes.append(DegradeNote("thinking", "off", "enabled", "GLM-5.3 强制思考，无法关闭"))
                payload["thinking"] = {"type": "enabled"}
            else:
                payload["thinking"] = {"type": "disabled"}
        elif thinking == "auto":
            payload["thinking"] = {"type": "enabled"}
        else:
            enum = self._FORCED_ENUM if forced else self.effort_enum
            applied = map_effort(thinking, enum, notes, self.display)
            if applied:
                payload["reasoning_effort"] = applied
            if not forced:
                payload["thinking"] = {"type": "enabled"}
        return notes


class OllamaAdapter(ProviderAdapter):
    name = "ollama"
    display = "Ollama（本地）"
    effort_enum = ()           # 本地模型无档位细分（待校准；新版对部分模型支持 think 档位）
    supports_auto = True
    supports_off = True

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        if thinking == "off":
            payload["think"] = False
        else:
            # off/auto/low/medium/high → bool 开关；档位不可细分 → 降级说明
            payload["think"] = True
            if thinking in ("low", "medium", "high"):
                notes.append(DegradeNote("thinking", thinking, "bool:on", "Ollama 本地模型档位不可细分，仅开关"))
        return notes
