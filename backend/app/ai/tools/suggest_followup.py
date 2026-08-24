"""suggest_followup 工具：根据对话上下文生成追加追问建议。

不放在任何技能里——由 loop 在回答完成后自动调用，不依赖模型主动调用。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _suggest_followup(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    question = (args or {}).get("question", "").strip()
    answer = (args or {}).get("answer", "").strip()

    if not question and not answer:
        return ToolOutcome(result={"ok": True, "suggestions": []})

    prompt = (
        f"用户问题：{question}\n"
        f"助手回答摘要：{answer[:500]}\n\n"
        "基于以上对话，生成 2-3 个自然的追问建议，帮助用户深入探索。\n"
        "要求：与当前数据库业务相关，不要重复已回答的内容。\n"
        '只返回 JSON 数组：["问题1", "问题2", "问题3"]'
    )

    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg

        class _Req:
            pass
        provider_cfg = resolve_provider_cfg(state, _Req())
        provider = gw.build_provider(provider_cfg)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        text = (getattr(resp, "content", "") or "").strip()
        import json
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        suggestions = json.loads(text)
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = [str(s) for s in suggestions[:3]]
    except Exception:
        suggestions = []

    return ToolOutcome(result={"ok": True, "suggestions": suggestions})


def register() -> None:
    register_tool(
        "suggest_followup",
        "生成对话追加追问建议（由系统自动调用，非用户主动触发）。",
        {
            "question": {"type": "string", "description": "用户问题"},
            "answer": {"type": "string", "description": "助手回答摘要"},
        },
        [],
        _suggest_followup,
        trust="readonly",
    )
