"""结构类原子工具：get_schema / describe_table（只读结构，不含数据）。"""
from __future__ import annotations

from app.ai.tools.registry import ToolOutcome, _get_ctx, register_tool
from app.core.schema import describe_table as core_describe
from app.core.schema import get_schema as core_schema


async def _get_schema(state, args, conn_id, include_data=False):
    schema = await core_schema(state, conn_id)
    text = await _summarize_with_kb(state, schema, conn_id)
    return ToolOutcome(result={"ok": True, "summary": text})


async def _describe_table(state, args, conn_id, include_data=False):
    table = args.get("table", "")
    try:
        info = await core_describe(state, conn_id, table)
    except KeyError as e:
        return ToolOutcome(result={"ok": False, "error": str(e)})
    lines = [f"表 {info['name']}（{info['kind']}）"]
    if info["comment"]:
        lines.append(f"注释: {info['comment']}")
    for c in info["columns"]:
        marks = ""
        if c.get("pk"):
            marks += " PK"
        if c.get("fk"):
            marks += " FK"
        lines.append(f"- {c['name']}: {c['type']}{marks}" + (f"  // {c['comment']}" if c["comment"] else ""))
    return ToolOutcome(result={"ok": True, "text": "\n".join(lines)})


async def _summarize_with_kb(state, schema, conn_id):
    from app.core.schema import summarize

    text = summarize(schema)
    kb = await state.knowledge.to_context(conn_id, "", None, 20)
    if kb:
        text += "\n" + kb
    return text


def register() -> None:
    register_tool(
        "get_schema",
        "获取当前连接的数据库结构摘要（表/列/类型/外键/注释）。只读结构，不含数据。",
        {"scope": {"type": "string", "enum": ["all"], "description": "范围，默认 all"}},
        [],
        _get_schema,
    )
    register_tool(
        "describe_table",
        "查看某张表的列定义、约束与注释。只读结构。",
        {"table": {"type": "string", "description": "表名"}},
        ["table"],
        _describe_table,
    )
