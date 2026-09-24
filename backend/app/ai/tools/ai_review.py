"""ai_review 工具：对 SQL 进行语义级安全审查，返回风险评估和建议。

trust=readonly, confirm=none（只出意见，不具放行权）。
本地规则引擎（app/safety/gate.py）始终在线强制拦截，本工具是额外的语义增强层。
自身是 LLM 调用，走单管道（脱敏→清单→审计），source=egress-review。
"""
from __future__ import annotations

import json
from app.core.timeutil import utcnow_iso
from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _ai_review(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    sql = (args or {}).get("sql", "").strip()
    context = (args or {}).get("context", "").strip()

    if not sql:
        return ToolOutcome(
            result={"ok": False, "error": "缺少 sql 参数"},
            think="ai_review 缺少 SQL。",
        )

    context_section = f"业务上下文：{context}" if context else ""
    from app.ai.prompts import render
    prompt = render("ai_review", sql=sql, context=context_section)

    # 铁律3：LLM 调用前构建清单 + 调用后写审计（source=egress-review）
    try:
        from app.ai.provider_cfg import resolve_provider_cfg
        class _Req:
            pass
        provider_cfg = resolve_provider_cfg(state, _Req())
    except Exception:
        provider_cfg = {"provider": "mock", "model": "mock"}

    manifest: dict[str, Any] = {
        "tables": [], "kb_docs": 0, "history_turns": 0,
        "include_data": False, "mode": "ai_review",
        "ts": utcnow_iso(),
        "model": provider_cfg.get("model", ""), "provider": provider_cfg.get("provider", "mock"),
        "source": "egress-review",
    }

    # 调 LLM 做语义审查
    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg

        class _Req:
            pass
        _req = _Req()
        provider_cfg = resolve_provider_cfg(state, _req)
        provider = gw.build_provider(provider_cfg)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None,
                                    # 中央记账：一次调用一条 egress-review（成本 + 出网清单）
                                    ctx={
                                        "conn_id": conn_id, "connection": conn_id,
                                        "skill": "ai_review", "source": "egress",
                                        "status": "egress-review", "include_data": False,
                                        "manifest": manifest,
                                    })
        text = (getattr(resp, "content", "") or "").strip()
        manifest["model"] = provider_cfg.get("model", "")
        manifest["provider"] = provider_cfg.get("provider", "mock")

        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        verdict = result.get("verdict", "safe")
        reasons = result.get("reasons", [])
        suggestions = result.get("suggestions", [])
    except json.JSONDecodeError:
        verdict = "safe"
        reasons = ["审查结果解析失败，默认safe"]
        suggestions = []
    except Exception:
        verdict = "safe"
        reasons = ["审查服务暂时不可用，默认safe"]
        suggestions = []

    # 出网清单审计已由中央记账拦截器（gateway）统一写（source=egress，status=egress-review）

    return ToolOutcome(
        result={
            "ok": True,
            "verdict": verdict,
            "reasons": reasons,
            "suggestions": suggestions,
        },
        think=f"AI安全审查结果：{verdict}",
    )


def register() -> None:
    register_tool(
        "ai_review",
        "对SQL语句进行语义级安全审查，返回风险评估(verdict)和建议。只出意见，不具放行权。",
        {
            "sql": {"type": "string", "description": "待审查的SQL语句"},
            "context": {"type": "string", "description": "业务上下文说明（可选）"},
        },
        ["sql"],
        _ai_review,
        trust="readonly",
    )
