"""kb_read 工具：查询知识库表知识卡（一表一卡）与已确认领域标签。

spec §4：检索/路由/工具全部消费表级知识卡；手写文档不再进向量检索，
但表卡 payload.draft_count 反映未确认草案量。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _kb_read(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    store = state.knowledge
    store.ensure_loaded(conn_id)  # 重启后恢复内存态（concept/tags/docs 统一入口）
    table_name = (args or {}).get("table_name", "").strip()
    keyword = (args or {}).get("keyword", "").strip()
    column = (args or {}).get("column", "").strip()
    query_type = (args or {}).get("query_type", "docs")  # docs | tags | concept | all

    results: dict[str, Any] = {"ok": True}

    if query_type in ("docs", "all"):
        if keyword:
            cards = await store.retrieve(conn_id, keyword, k=10)
            results["docs"] = [c.to_dict() for c in cards]
        elif table_name:
            card = store.table_card(conn_id, table_name)
            results["docs"] = [card] if card else []
        else:
            results["docs"] = store.table_cards(conn_id)

    if query_type in ("tags", "all"):
        lib = store.tags(conn_id)["library"]
        results["tags"] = [t for t in lib if t["status"] == "confirmed"]

    if query_type in ("concept", "all"):
        cs = store.concept_store
        if table_name and column:
            c = cs.get_for_column(conn_id, table_name, column)
            results["concept"] = c.to_dict() if c else None
        elif keyword:
            hits = [c.to_dict() for c in cs.list(conn_id) if keyword.lower() in c.name.lower()]
            results["concepts"] = hits
        else:
            results["concepts"] = [c.to_dict() for c in cs.list(conn_id)]

    if table_name:
        card = store.table_card(conn_id, table_name)
        if card:
            results["context"] = card["text"]

    count = (len(results.get("docs", [])) + len(results.get("tags", []))
             + len(results.get("concepts", [])))
    return ToolOutcome(
        result=results,
        think=f"知识库查询：{count} 条结果。",
    )


def register() -> None:
    register_tool(
        "kb_read",
        "查询知识库：按表名/关键词搜索表知识卡，查看已确认的领域标签，或按表列查概念字典（值落地）。",
        {
            "table_name": {"type": "string", "description": "表名（可选）"},
            "column": {"type": "string", "description": "列名（与 table_name 配合查概念，可选）"},
            "keyword": {"type": "string", "description": "搜索关键词（可选）"},
            "query_type": {"type": "string", "enum": ["docs", "tags", "concept", "all"], "description": "查询类型（默认 docs）"},
        },
        [],
        _kb_read,
        trust="readonly",
    )
