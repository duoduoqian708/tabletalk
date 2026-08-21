"""sqlglot 解析 → StatementInfo。解析失败按 unknown 处理，永不静默放行。"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

READ_TYPES = {"select", "show", "describe", "explain", "pragma", "desc"}
DML_TYPES = {"update", "delete", "insert"}
DDL_TYPES = {"create", "drop", "alter", "truncate"}
TCL_TYPES = {"begin", "commit", "rollback", "set"}
UNKNOWN_TYPES = {"other", "command", "empty"}


@dataclass
class StatementInfo:
    kind: str                     # read | dml | ddl | tcl | unknown
    stmt_type: str                # select | update | delete | ...
    table: str | None = None      # 主要目标表
    tables: list[str] = field(default_factory=list)
    has_where: bool | None = None
    has_limit: bool | None = None
    parse_error: str | None = None


def _stmt_type(node: exp.Expression) -> str:
    if isinstance(node, exp.Select) or isinstance(node, exp.Union):
        return "select"
    if isinstance(node, exp.Update):
        return "update"
    if isinstance(node, exp.Delete):
        return "delete"
    if isinstance(node, exp.Insert):
        return "insert"
    if isinstance(node, exp.Create):
        return "create"
    if isinstance(node, exp.Drop):
        return "drop"
    if isinstance(node, exp.Alter):
        return "alter"
    if isinstance(node, exp.TruncateTable):
        return "truncate"
    if isinstance(node, exp.Describe):
        return "describe"
    if isinstance(node, exp.Show):
        return "show"
    # 注意：此版本 sqlglot 无 exp.Explain——EXPLAIN 由 Command 分支按 name 识别
    if isinstance(node, exp.Pragma):
        return "pragma"
    if isinstance(node, exp.Transaction):
        return "begin"
    if isinstance(node, exp.Commit):
        return "commit"
    if isinstance(node, exp.Rollback):
        return "rollback"
    if isinstance(node, exp.Set):
        return "set"
    if isinstance(node, exp.Command):
        return str(node.name or "").lower() or "command"
    return "other"


def _kind(stmt_type: str) -> str:
    if stmt_type in READ_TYPES:
        return "read"
    if stmt_type in DML_TYPES:
        return "dml"
    if stmt_type in DDL_TYPES:
        return "ddl"
    if stmt_type in TCL_TYPES:
        return "tcl"
    return "unknown"


def _extract_tables(node: exp.Expression) -> tuple[str | None, list[str]]:
    primary: str | None = None
    if isinstance(node, (exp.Update, exp.Delete)):
        tgt = node.args.get("this")
        if tgt is not None:
            primary = getattr(tgt, "name", None) or tgt.sql()
    elif isinstance(node, exp.Insert):
        tgt = node.args.get("this")
        if tgt is not None:
            primary = getattr(tgt, "name", None) or tgt.sql()
    all_tables: list[str] = []
    for t in node.find_all(exp.Table):
        n = getattr(t, "name", None)
        if n and n not in all_tables:
            all_tables.append(n)
    return primary, all_tables


def classify(node: exp.Expression) -> StatementInfo:
    stmt_type = _stmt_type(node)
    primary, tables = _extract_tables(node)
    has_where = None
    has_limit = None
    if isinstance(node, (exp.Select, exp.Update, exp.Delete)):
        has_where = node.args.get("where") is not None
    if isinstance(node, exp.Select):
        has_limit = node.args.get("limit") is not None
    return StatementInfo(
        kind=_kind(stmt_type),
        stmt_type=stmt_type,
        table=primary,
        tables=tables,
        has_where=has_where,
        has_limit=has_limit,
    )


def parse_sql(sql: str, sqlglot_dialect: str) -> list[StatementInfo]:
    try:
        parsed = sqlglot.parse(sql, read=sqlglot_dialect)
    except Exception as e:  # noqa: BLE001
        return [StatementInfo(kind="unknown", stmt_type="unknown", parse_error=f"{type(e).__name__}: {e}")]
    if not parsed or all(p is None for p in parsed):
        return [StatementInfo(kind="unknown", stmt_type="empty", parse_error="空语句")]
    infos: list[StatementInfo] = []
    for node in parsed:
        if node is None:
            continue
        infos.append(classify(node))
    return infos
