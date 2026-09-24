"""初始提示词 API：根据数据库 schema + 领域标签生成快捷提示词。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any

router = APIRouter(prefix="/api/v1/suggestions", tags=["suggestions"])


class InitialRequest(BaseModel):
    connection_id: str


@router.post("/initial")
async def initial_suggestions(body: InitialRequest, request) -> dict[str, Any]:
    from app.config import get_env
    from app.state import app_state

    state = app_state
    conn_id = body.connection_id

    # 取 schema 摘要
    try:
        from app.core.schema import get_schema, summarize
        schema = await get_schema(state, conn_id)
        schema_text = summarize(schema)
    except Exception:
        schema_text = "（无法获取 schema）"

    # 取已确认标签（统一走 state.knowledge，与知识库同存储）
    try:
        tag_names = state.knowledge.confirmed_tags(conn_id)
    except Exception:
        tag_names = []

    tags_str = "、".join(tag_names) if tag_names else "（暂无领域标签）"

    from app.ai.prompts import render
    prompt = render("suggestions_initial", schema_text=schema_text[:2000], tags=tags_str)

    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg

        class _Req:
            pass
        provider_cfg = resolve_provider_cfg(state, _Req())
        provider = gw.build_provider(provider_cfg)
        # 中央记账：llm_log + cost + egress 审计（铁律4——每次模型调用可审计，不漏）
        try:
            conn_name = state.connections.get(conn_id).name
        except Exception:
            conn_name = conn_id
        resp = await provider.chat(
            [{"role": "user", "content": prompt}], tools=None,
            ctx={"conn_id": conn_id, "connection": conn_name, "skill": "suggestions_initial",
                 "source": "egress", "status": "egress", "include_data": False})
        import json, re
        text = (getattr(resp, "content", "") or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL).strip()
        suggestions = json.loads(text)
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = [str(s) for s in suggestions[:5]]
    except Exception:
        suggestions = [
            "查看数据库有哪些表",
            "统计各表的记录数",
            "查看表之间的关联关系",
        ]

    return {"ok": True, "suggestions": suggestions}
