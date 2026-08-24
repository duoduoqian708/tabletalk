"""graph_read 工具：查询表间关联关系。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _graph_read(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    from app.ai.graph.store import GraphStore
    from app.config import get_env

    store = GraphStore(get_env().data_dir)
    table_name = (args or {}).get("table_name", "").strip()
    hops = int((args or {}).get("hops") or 2)

    if table_name:
        neighbors = store.get_neighbors(conn_id, table_name, hops=hops)
        return ToolOutcome(
            result={"ok": True, "table": table_name, "relations": neighbors, "count": len(neighbors)},
            think=f"查询 {table_name} 的关联表：{len(neighbors)} 条边。",
        )
    else:
        edges = store.list_edges(conn_id)
        return ToolOutcome(
            result={"ok": True, "edges": edges, "count": len(edges)},
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
