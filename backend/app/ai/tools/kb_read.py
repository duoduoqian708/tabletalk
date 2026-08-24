"""kb_read 工具：查询知识库文档、注释、领域标签。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _kb_read(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    from app.ai.knowledge.store import KnowledgeStore
    from app.config import get_env

    store = KnowledgeStore(get_env().data_dir)
    table_name = (args or {}).get("table_name", "").strip()
    keyword = (args or {}).get("keyword", "").strip()
    query_type = (args or {}).get("query_type", "docs")  # docs | tags | all

    results: dict[str, Any] = {"ok": True}

    if query_type in ("docs", "all"):
        if keyword:
            results["docs"] = store.search_docs(conn_id, keyword)
        elif table_name:
            results["docs"] = store.list_docs(conn_id, table_name=table_name, status="confirmed")
        else:
            results["docs"] = store.list_docs(conn_id, status="confirmed")[:20]

    if query_type in ("tags", "all"):
        results["tags"] = store.get_confirmed_tags(conn_id)

    if table_name:
        results["context"] = store.get_confirmed_context(conn_id, table_name)

    count = len(results.get("docs", [])) + len(results.get("tags", []))
    return ToolOutcome(
        result=results,
        think=f"知识库查询：{count} 条结果。",
    )


def register() -> None:
    register_tool(
        "kb_read",
        "查询知识库：按表名/关键词搜索文档，或查看已确认的领域标签。",
        {
            "table_name": {"type": "string", "description": "表名（可选）"},
            "keyword": {"type": "string", "description": "搜索关键词（可选）"},
            "query_type": {"type": "string", "enum": ["docs", "tags", "all"], "description": "查询类型（默认 docs）"},
        },
        [],
        _kb_read,
        trust="readonly",
    )
