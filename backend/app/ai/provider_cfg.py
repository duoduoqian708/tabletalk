"""解析生效的 AI provider 配置：model_id 命中 ai_models 优先，支持逐次覆盖与 reasoning。

loop（查询模式）与 report（报告模式）共用，保证两种模式对「按对话切模型 + 思考强度」
行为一致——报告模式此前忽略 model_id / reasoning，属承诺缺口（PRD §9）。
"""
from __future__ import annotations

from typing import Any


def resolve_provider_cfg(state, req) -> dict[str, Any]:
    rs = state.runtime.get()
    mid = getattr(req, "model_id", None)
    if mid:
        target = next((m for m in rs.ai_models if m.id == mid), None)
        if target is not None:
            cfg = {
                "provider": target.provider,
                "base_url": target.base_url,
                "api_key": target.api_key,
                "model": target.model,
                "temperature": target.temperature,
                "timeout": target.timeout,
                "reasoning": target.reasoning,
            }
        else:
            cfg = rs.provider_config()
    else:
        cfg = rs.provider_config()
    for key in ("provider", "base_url", "api_key", "model"):
        override = getattr(req, key, None)
        if override is not None:
            cfg[key] = override
    if getattr(req, "reasoning", None) is not None:
        cfg["reasoning"] = req.reasoning
    if getattr(req, "temperature", None) is not None:
        cfg["temperature"] = req.temperature
    return cfg
