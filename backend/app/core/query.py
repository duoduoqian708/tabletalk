"""查询执行、结果序列化、行数上限、取消。

`safe` 判定不在本模块——SQL 必须先过安全闸门（app.safety）再进这里执行。
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

if TYPE_CHECKING:
    from app.state import AppState

_running: dict[str, set[asyncio.Task]] = {}


def _apply_page(sql: str, sqlglot_dialect: str, limit: int | None, offset: int | None) -> str:
    if limit is None and offset is None:
        return sql
    try:
        parsed = sqlglot.parse(sql, read=sqlglot_dialect)
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            return sql
        node = parsed[0]
        limit_expr = exp.Literal.number(limit if limit is not None else 10**9)
        offset_expr = exp.Literal.number(offset) if offset else None
        node.set("limit", exp.Limit(expression=limit_expr, offset=offset_expr))
        return node.sql(dialect=sqlglot_dialect)
    except Exception:
        return sql


def _auto_cap(sql: str, sqlglot_dialect: str, cap: int) -> str:
    """无 LIMIT 的单条 SELECT 自动注入 LIMIT cap+1：把数据库工作量截断在行数上限内。

    只截断传输是不够的——驱动会先全量拉回内存。这里在 SQL 层截断 DB 工作量；
    多取 1 行用于判断 truncated。用户显式写了 LIMIT 或调用方给了分页参数则不动。
    """
    try:
        parsed = sqlglot.parse(sql, read=sqlglot_dialect)
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            return sql
        node = parsed[0]
        if node.args.get("limit") is not None:
            return sql
        node.set("limit", exp.Limit(expression=exp.Literal.number(cap + 1)))
        return node.sql(dialect=sqlglot_dialect)
    except Exception:
        return sql


# JS 安全整数上界：超过此值的数字经 JSON 传给浏览器会丢精度（float64 尾数截断）
JS_SAFE_INT_MAX = 9007199254740991


def serialize_value(v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        # 超大整数转字符串，防止浏览器 JSON.parse 精度丢失
        return str(v) if abs(v) > JS_SAFE_INT_MAX else v
    if isinstance(v, float):
        return v
    if isinstance(v, Decimal):
        # 高精度金额：超出安全范围转字符串；范围内转 float 供展示
        return str(v) if abs(v) > JS_SAFE_INT_MAX else float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bytes):
        return f"<blob {len(v)}B>"
    if isinstance(v, (list, dict)):
        return str(v)
    return str(v)


def _type_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "numeric"
    if isinstance(v, Decimal):
        return "numeric"
    if isinstance(v, datetime):
        return "timestamptz"
    if isinstance(v, date):
        return "date"
    return "text"


def serialize_rows(raw, max_rows: int | None):
    columns = raw.columns or []
    raw_rows = (raw.rows or [])[: max_rows or len(raw.rows or [])]
    rows = [[serialize_value(v) for v in r] for r in raw_rows]
    if not columns and rows:
        columns = [f"c{i+1}" for i in range(len(rows[0]))]
    # 每列取第一个非空值的类型（从原始值推断，超大 int 序列化为 str 后仍报 int）
    types: list[str] = []
    if raw_rows:
        for j in range(len(raw_rows[0])):
            tv = next((row[j] for row in raw_rows if row[j] is not None), None)
            types.append(_type_of(tv))
    return columns, rows, types


async def execute(
    state: "AppState",
    conn_id: str,
    sql: str,
    limit: int | None = None,
    offset: int | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    max_rows = max_rows or state.runtime.get().query_max_rows
    cfg = state.connections.get(conn_id)
    dialect = cfg.dialect if cfg.dialect in ("sqlite", "postgres", "mysql") else "sqlite"
    paged_sql = _apply_page(sql, dialect, limit, offset)
    # 调用方没给分页参数时，在 SQL 层截断 DB 工作量（不只截断传输）
    if limit is None and offset is None:
        paged_sql = _auto_cap(paged_sql, dialect, max_rows)

    t0 = time.monotonic()
    task = asyncio.current_task()
    _running.setdefault(conn_id, set()).add(task)
    try:
        raw = await state.pools.execute(conn_id, paged_sql)
    finally:
        _running.get(conn_id, set()).discard(task)

    columns, rows, types = serialize_rows(raw, max_rows=max_rows)
    truncated = len(raw.rows or []) > max_rows
    return {
        "columns": columns,
        "types": types,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "affected_rows": raw.rowcount if raw.is_dml else None,
        "is_dml": raw.is_dml,
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
    }


async def count_total(state: "AppState", conn_id: str, sql: str) -> int | None:
    cfg = state.connections.get(conn_id)
    try:
        parsed = sqlglot.parse(sql, read=cfg.dialect)
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            return None
        wrapped = f"SELECT COUNT(*) AS _n FROM ({sql}) AS _t"
        raw = await state.pools.execute(conn_id, wrapped)
        if raw.rows:
            return int(raw.rows[0][0])
    except Exception:
        pass
    return None


def cancel(conn_id: str) -> int:
    tasks = list(_running.get(conn_id, ()))
    for t in tasks:
        t.cancel()
    return len(tasks)
