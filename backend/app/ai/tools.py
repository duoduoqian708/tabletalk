"""AI 工具集：4 个执行工具 + draft_ddl（仅草稿）。

执行工具全部过安全闸门；AI 无 DDL 执行工具。tool 结果默认只回列名+行数，
仅当 include_data opt-in 才回传明细。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.schema import describe_table as core_describe
from app.core.schema import get_schema as core_schema
from app.safety import gate as safety_gate
from app.safety import parser as safety_parser
from app.safety.models import Origin, Verdict

if TYPE_CHECKING:
    from app.state import AppState


@dataclass
class ToolOutcome:
    result: dict[str, Any]
    card: dict[str, Any] | None = None
    think: str | None = None


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _tool(
        "get_schema",
        "获取当前连接的数据库结构摘要（表/列/类型/外键/注释）。只读结构，不含数据。",
        {"scope": {"type": "string", "enum": ["all"], "description": "范围，默认 all"}},
        [],
    ),
    _tool(
        "describe_table",
        "查看某张表的列定义、约束与注释。只读结构。",
        {"table": {"type": "string", "description": "表名"}},
        ["table"],
    ),
    _tool(
        "run_query",
        "执行只读查询。结果只会返回列名与行数；如需明细需用户 opt-in。",
        {"sql": {"type": "string", "description": "只读 SELECT SQL"}},
        ["sql"],
    ),
    _tool(
        "run_dml",
        "执行写操作（INSERT/UPDATE/DELETE）。必须先评估影响行数并需用户确认，绝不自动执行。",
        {"sql": {"type": "string", "description": "DML SQL"}},
        ["sql"],
    ),
    _tool(
        "draft_ddl",
        "生成 DDL 脚本（CREATE/ALTER/DROP 等）草稿，发送到编辑器由用户手动执行。绝不执行。",
        {"sql": {"type": "string", "description": "DDL SQL 脚本"}},
        ["sql"],
    ),
]


# 报告模式只读工具集：只结构发现 + 只读查询，物理上无法写/改结构。
# 与"DDL 不进 AI 工具集"同构——报告天然只读，连 run_dml/draft_ddl 都不挂。
TOOL_SCHEMAS_READONLY: list[dict] = [
    t for t in TOOL_SCHEMAS if t["function"]["name"] in ("get_schema", "describe_table", "run_query")
]


def _sub(sql: str, sqlglot_dialect: str) -> str:
    infos = safety_parser.parse_sql(sql, sqlglot_dialect)
    if not infos:
        return ""
    i = infos[0]
    extra = f" · {len(i.tables)} tables" if i.tables else ""
    return f"{i.stmt_type.upper()}{extra}"


async def execute_tool(
    state: "AppState",
    name: str,
    args: dict[str, Any],
    conn_id: str,
    include_data: bool = False,
) -> ToolOutcome:
    cfg = state.connections.get(conn_id)
    dialect = safety_gate.sqlglot_dialect_for(cfg.dialect)

    if name == "get_schema":
        schema = await core_schema(state, conn_id)
        text = await _summarize_with_kb(state, schema, conn_id)
        return ToolOutcome(result={"ok": True, "summary": text})

    if name == "describe_table":
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

    if name == "run_query":
        sql = args.get("sql", "")
        assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
        if assessment.verdict == Verdict.ALLOW:
            from app.core.query import execute as run_query

            res = await run_query(state, conn_id, sql)
            result: dict[str, Any] = {
                "ok": True,
                "columns": res["columns"],
                "row_count": res["row_count"],
                "truncated": res["truncated"],
                "elapsed_ms": res["elapsed_ms"],
            }
            if include_data:
                result["rows"] = res["rows"]
            card = {"tier": "read", "verdict": "allow", "sql": sql, "sub": _sub(sql, dialect)}
            return ToolOutcome(
                result=result,
                card=card,
                think=f"只读查询，安全闸门放行（{res['row_count']} 行）。",
            )
        return ToolOutcome(
            result={"ok": False, "verdict": assessment.verdict.value, "reason": "; ".join(assessment.reasons)},
            card={"tier": assessment.tier.value, "verdict": assessment.verdict.value, "sql": sql,
                  "sub": _sub(sql, dialect), "reason": "; ".join(assessment.reasons)},
            think="安全闸门未放行。",
        )

    if name == "run_dml":
        sql = args.get("sql", "")
        assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
        if assessment.verdict == Verdict.BLOCK:
            return ToolOutcome(
                result={"ok": False, "verdict": "block", "reason": "; ".join(assessment.reasons)},
                card={"tier": "dml", "verdict": "block", "sql": sql, "sub": _sub(sql, dialect),
                      "reason": "; ".join(assessment.reasons)},
                think="写操作被拦截。",
            )
        preview = await safety_gate.preview_rows(state, conn_id, sql, dialect)
        return ToolOutcome(
            result={"ok": True, "verdict": "review", "preview_rows": preview, "needs_confirm": True,
                    "reason": "; ".join(assessment.reasons)},
            card={"tier": "dml", "verdict": "review", "sql": sql, "sub": _sub(sql, dialect),
                  "preview_rows": preview, "reason": "需确认后执行"},
            think=f"写操作已评估：预估影响 {preview if preview is not None else '未知'} 行，需确认。",
        )

    if name == "draft_ddl":
        sql = args.get("sql", "")
        return ToolOutcome(
            result={"ok": True, "note": "DDL 脚本已生成，发送到编辑器手动执行。AI 不执行 DDL。"},
            card={"tier": "ddl", "verdict": "manual", "sql": sql, "sub": "DDL · manual only"},
            think="生成 DDL 脚本（仅草稿，不执行）。",
        )

    return ToolOutcome(result={"ok": False, "error": f"未知工具: {name}"})


async def _summarize_with_kb(state: "AppState", schema: dict, conn_id: str) -> str:
    from app.core.schema import summarize

    text = summarize(schema)
    kb = await state.knowledge.to_context(conn_id, "", None, 20)
    if kb:
        text += "\n" + kb
    return text
