"""结构类原子工具：get_schema（合并旧 get_schema + describe_table）。

可选 table 参数：有表→返回列详情；无表→返回全部表列表+知识库摘要。
"""
from __future__ import annotations

from app.ai.tools.registry import ToolOutcome, register_tool
from app.core.schema import describe_table as core_describe
from app.core.schema import get_schema as core_schema


async def _get_schema(state, args, conn_id, include_data=False):
    table = (args or {}).get("table", "").strip()
    if table:
        # 有表名：返回该表的列定义详情（原 describe_table 功能）
        try:
            info = await core_describe(state, conn_id, table)
        except KeyError as e:
            return ToolOutcome(result={"ok": False, "error": str(e)})
        lines = [f"表 {info['name']}（{info['kind']}）"]
        if info.get("comment"):
            lines.append(f"注释: {info['comment']}")
        for c in info.get("columns", []):
            marks = ""
            if c.get("pk"):
                marks += " PK"
            if c.get("fk"):
                marks += " FK"
            lines.append(f"- {c['name']}: {c['type']}{marks}" + (f"  // {c['comment']}" if c.get("comment") else ""))
        return ToolOutcome(result={"ok": True, "text": "\n".join(lines)})
    # 无表名：返回全部表列表 + 知识库摘要
    schema = await core_schema(state, conn_id)
    text = await _summarize_with_kb(state, schema, conn_id)
    return ToolOutcome(result={"ok": True, "summary": text})


async def _summarize_with_kb(state, schema, conn_id):
    from app.core.schema import summarize

    text = summarize(schema)
    try:
        kb = await state.knowledge.to_context(conn_id, "", None, 20)
        if kb:
            text += "\n" + kb
    except Exception:
        pass
    return text


def register() -> None:
    register_tool(
        "get_schema",
        "查看数据库结构：传表名返回该表列定义，不传表名返回全部表列表+知识库摘要。",
        {"table": {"type": "string", "description": "表名（可选，不传则返回全部表）"}},
        [],
        _get_schema,
        trust="readonly",
    )
