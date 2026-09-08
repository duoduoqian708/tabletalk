"""火山方舟（Ark）适配器。

caps 依据（2026-09 官方文档，用户提供）：
- reasoning_effort：none/minimal/low/medium/high/xhigh/max 全七档 —— 统一方言零映射直传
- thinking.type：enabled/disabled/auto（auto 为方舟独家：模型自主判断是否思考）
- 策略：只用 reasoning_effort 单字段（off→none），不发 thinking.type，避免双字段冲突；
  auto 是唯一例外（effort 无 auto 档，用 thinking.type=auto 原生表达）。
- 各模型对参数的支持/默认不同（文档明示）→ 上线后经 /ai/test 校准回填。
"""
from __future__ import annotations

from app.ai.providers.base import DegradeNote, ProviderAdapter, map_effort


class ArkAdapter(ProviderAdapter):
    name = "volcano"
    display = "火山方舟"
    effort_enum = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    supports_auto = True
    supports_off = True
    reasoning_field = "reasoning_content"   # 实测确认

    def apply_thinking(self, payload: dict, thinking: str, model: str, has_tools: bool) -> list[DegradeNote]:
        notes: list[DegradeNote] = []
        if thinking == "off":
            payload["reasoning_effort"] = "none"
        elif thinking == "auto":
            payload["thinking"] = {"type": "auto"}
        else:
            # low/medium/high 全部 ∈ 官方枚举 → 直传零映射
            applied = map_effort(thinking, self.effort_enum, notes, self.display)
            if applied:
                payload["reasoning_effort"] = applied
        return notes
