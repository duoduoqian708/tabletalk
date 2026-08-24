"""graph_write 工具：维护图谱边（增/删）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _graph_write(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    from app.ai.graph.store import GraphStore
    from app.config import get_env

    store = GraphStore(get_env().data_dir)
    action = (args or {}).get("action", "").strip()
    source = (args or {}).get("source", "").strip()
    target = (args or {}).get("target", "").strip()

    if not action or not source or not target:
        return ToolOutcome(result={"ok": False, "error": "需要 action, source, target 参数"})

    if action == "add":
        relation = (args or {}).get("relation", "related")
        source_col = (args or {}).get("source_col")
        target_col = (args or {}).get("target_col")
        edge = store.add_edge(conn_id, source, target, relation, source_col, target_col)
        return ToolOutcome(
            result={"ok": True, "edge": edge, "message": f"边 {source}→{target}（{relation}）已添加。"},
            think=f"已添加图谱边：{source} → {target}。",
        )

    elif action == "remove":
        relation = (args or {}).get("relation")
        ok = store.remove_edge(conn_id, source, target, relation)
        if ok:
            return ToolOutcome(result={"ok": True, "message": f"边 {source}→{target} 已删除。"})
        return ToolOutcome(result={"ok": False, "error": f"边 {source}→{target} 不存在。"})

    return ToolOutcome(result={"ok": False, "error": f"未知操作: {action}"})


def register() -> None:
    register_tool(
        "graph_write",
        "维护图谱边：add 添加关联边，remove 删除关联边。",
        {
            "action": {"type": "string", "enum": ["add", "remove"], "description": "操作类型"},
            "source": {"type": "string", "description": "源表名"},
            "target": {"type": "string", "description": "目标表名"},
            "relation": {"type": "string", "description": "关系类型（默认 related，如 fk/values_overlap）"},
            "source_col": {"type": "string", "description": "源列名（可选）"},
            "target_col": {"type": "string", "description": "目标列名（可选）"},
        },
        ["action", "source", "target"],
        _graph_write,
        trust="mutating",
        confirm="card",
    )
