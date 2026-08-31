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

    prompt = (
        f"你是 TableTalk 的数据库助手。当前数据库结构：\n{schema_text[:2000]}\n\n"
        f"领域标签：{tags_str}\n\n"
        "请根据以上数据库结构和业务领域，生成 4-5 个用户最可能想问的问题。\n"
        "要求：\n"
        "- 问题必须与当前数据库的业务高度相关\n"
        "- 覆盖不同场景：查数据、看结构、统计分析\n"
        "- 语言自然，像真人会问的问题\n"
        "- 不要问与数据库无关的问题\n"
        '只返回 JSON 数组：["问题1", "问题2", ...]'
    )

    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg

        class _Req:
            pass
        provider_cfg = resolve_provider_cfg(state, _Req())
        provider = gw.build_provider(provider_cfg)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
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
