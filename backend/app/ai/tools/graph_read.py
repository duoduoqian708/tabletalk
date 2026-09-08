"""graph_read 工具：查询表间关联关系（统一图谱，T2 后指向知识库唯一图）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


def _to_relation_list(edges: list[dict]) -> list[dict[str, Any]]:
    """知识库图边 → 工具兼容结构 {source, target, relation, source_col, target_col}。

    relation 字段承载边的来源（fk/naming/llm/query_log/user），供 AI 判断可信度。
    """
    out = []
    for e in edges:
        out.append({
            "source": e.get("from", ""),
            "target": e.get("to", ""),
            "relation": e.get("source", "related"),
            "source_col": e.get("from_col"),
            "target_col": e.get("to_col"),
            "cardinality": e.get("cardinality", "n:1"),
            "reason": e.get("reason", ""),
        })
    return out


async def _graph_read(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    kb = state.knowledge
    table_name = (args or {}).get("table_name", "").strip()
    hops = int((args or {}).get("hops") or 2)

    edges = kb.graph(conn_id).get("edges", [])
    if table_name:
        # 种子表可达范围（BFS，按图扩展）内的边，且至少一端是种子表可达节点
        reachable = kb.expand_tables(conn_id, {table_name}, hops=hops)
        related = [e for e in edges
                   if e.get("from") in reachable or e.get("to") in reachable]
        return ToolOutcome(
            result={"ok": True, "table": table_name, "relations": _to_relation_list(related),
                    "count": len(related)},
            think=f"查询 {table_name} 的关联表：{len(related)} 条边。",
        )
    else:
        return ToolOutcome(
            result={"ok": True, "edges": _to_relation_list(edges), "count": len(edges)},
            think=f"图谱共 {len(edges)} 条边。",
        )


def register() -> None:
    register_tool(
        "graph_read",
        "查询表间关联关系：传表名返回关联表列表（默认2跳BFS），不传返回全图概览。",
        {
            "table_name": {"type": "string", "description": "表名（可选）"},
            "hops": {"type": "integer", "description": "跳数（默认2）"},
        },
        [],
        _graph_read,
        trust="readonly",
    )
