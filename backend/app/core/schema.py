"""方言感知的 schema 发现、单表描述、DDL 导出、以及给 AI 的 summarize()。

知识库与 AI 上下文都从这里拿"结构"——本模块永不接触行数据。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.core.dialects.base import ColumnRef, FKRef, TableRef

if TYPE_CHECKING:
    from app.state import AppState

_CACHE_TTL = 30.0
_schema_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _discover(state: "AppState", conn_id: str):
    def _work(adapter, conn):
        async def inner():
            tables = await adapter.list_tables(conn)
            columns: list[ColumnRef] = []
            for t in tables:
                columns += await adapter.list_columns(conn, t.name)
            fks = await adapter.list_foreign_keys(conn)
            return tables, columns, fks

        return inner()

    return await state.pools.run(conn_id, _work)


async def get_schema(state: "AppState", conn_id: str, refresh: bool = False) -> dict[str, Any]:
    cached = _schema_cache.get(conn_id)
    if not refresh and cached and time.monotonic() - cached[0] < _CACHE_TTL:
        return cached[1]

    cfg = state.connections.get(conn_id)
    tables, columns, fks = await _discover(state, conn_id)
    fk_set = {(fk.table, fk.column) for fk in fks}
    for c in columns:
        c.is_fk = (c.table, c.name) in fk_set

    result: dict[str, Any] = {
        "connection": cfg.name,
        "dialect": cfg.dialect,
        "databases": [cfg.database] if cfg.database else [],
        "tables": [
            {"name": t.name, "kind": t.kind, "comment": t.comment,
             "column_count": sum(1 for c in columns if c.table == t.name)}
            for t in tables
        ],
        "columns": [
            {"table": c.table, "name": c.name, "type": c.data_type, "nullable": c.nullable,
             "pk": c.is_pk, "fk": c.is_fk, "default": c.default, "comment": c.comment}
            for c in columns
        ],
        "foreign_keys": [
            {"table": fk.table, "column": fk.column, "ref_table": fk.ref_table, "ref_column": fk.ref_column}
            for fk in fks
        ],
    }
    _schema_cache[conn_id] = (time.monotonic(), result)
    return result


def invalidate_schema(conn_id: str) -> None:
    _schema_cache.pop(conn_id, None)


async def describe_table(state: "AppState", conn_id: str, table: str) -> dict[str, Any]:
    schema = await get_schema(state, conn_id)
    tables = {t["name"]: t for t in schema["tables"]}
    tinfo = tables.get(table)
    if tinfo is None:
        raise KeyError(f"表不存在: {table}")
    columns = [c for c in schema["columns"] if c["table"] == table]
    return {"name": table, "kind": tinfo["kind"], "comment": tinfo["comment"], "columns": columns}


async def preview_table(state: "AppState", conn_id: str, table: str, limit: int = 100) -> dict[str, Any]:
    def _work(adapter_obj, conn):
        async def inner():
            quote = adapter_obj.quote_ident
            sel = await adapter_obj.execute(conn, f"SELECT * FROM {quote(table)} LIMIT {limit}")
            cnt = await adapter_obj.execute(conn, f"SELECT COUNT(*) AS _n FROM {quote(table)}")
            total = cnt.rows[0][0] if cnt.rows else 0
            return sel, total

        return inner()

    from app.core.query import serialize_rows

    raw, total = await state.pools.run(conn_id, _work)
    columns, rows, types = serialize_rows(raw, max_rows=limit)
    return {"columns": columns, "types": types, "rows": rows, "total": total}


async def sample_values(state: "AppState", conn_id: str, table: str, per_column: int = 10) -> dict[str, list[Any]]:
    """每列取前 K 个不同的样本值（只读，本地）。供图谱值重叠边与 AI 注释使用。"""
    from app.core.query import serialize_value

    def _work(adapter, conn):
        async def inner():
            quote = adapter.quote_ident
            cols = await adapter.list_columns(conn, table)
            out: dict[str, list[Any]] = {}
            for c in cols:
                try:
                    raw = await adapter.execute(
                        conn, f"SELECT DISTINCT {quote(c.name)} FROM {quote(table)} LIMIT {per_column}"
                    )
                    out[c.name] = [serialize_value(r[0]) for r in raw.rows] if raw.rows else []
                except Exception:
                    out[c.name] = []
            return out

        return inner()

    return await state.pools.run(conn_id, _work)


async def export_ddl(state: "AppState", conn_id: str, table: str) -> dict[str, Any]:
    cfg = state.connections.get(conn_id)

    def _work(adapter, conn):
        async def inner():
            quote = adapter.quote_ident
            literal = adapter.quote_literal
            tables = await adapter.list_tables(conn)
            tab = next((t for t in tables if t.name == table), None)
            if tab is None:
                raise KeyError(f"表不存在: {table}")
            cols = await adapter.list_columns(conn, table)
            fks = [f for f in await adapter.list_foreign_keys(conn) if f.table == table]
            lines = [f"CREATE TABLE {quote(table)} ("]
            defs = []
            for c in cols:
                parts = [quote(c.name), c.data_type]
                if c.is_pk:
                    parts.append("PRIMARY KEY")
                elif c.nullable is False:
                    parts.append("NOT NULL")
                if c.default is not None:
                    parts.append(f"DEFAULT {literal(c.default)}")
                defs.append("  " + " ".join(parts))
            for fk in fks:
                defs.append(
                    f"  CONSTRAINT fk_{table}_{fk.column} FOREIGN KEY ({quote(fk.column)}) "
                    f"REFERENCES {quote(fk.ref_table)} ({quote(fk.ref_column)})"
                )
            lines.append(",\n".join(defs))
            lines.append(");")
            return {"table": table, "ddl": "\n".join(lines)}

        return inner()

    return await state.pools.run(conn_id, _work)


def summarize(schema: dict[str, Any], table: str | None = None, table_names: list[str] | None = None) -> str:
    """给 AI 的 schema 摘要：只发结构，不含行数据。

    table 过滤单表；table_names 过滤一组表（领域路由后的候选子图）。
    """
    parts: list[str] = []
    tables = {t["name"]: t for t in schema["tables"]}
    columns = {t: [] for t in tables}
    for c in schema["columns"]:
        if c["table"] in columns:
            columns[c["table"]].append(c)

    for tname, tinfo in tables.items():
        if table and tname != table:
            continue
        if table_names and tname not in table_names:
            continue
        tag = "view" if tinfo["kind"] == "view" else "table"
        head = f"- {tname} ({tag}"
        if tinfo.get("column_count"):
            head += f", {tinfo['column_count']} 列"
        if tinfo.get("comment"):
            head += f", 注释: {tinfo['comment']}"
        parts.append(head + ")")
        for c in columns.get(tname, []):
            marks = []
            if c["pk"]:
                marks.append("PK")
            if c["fk"]:
                marks.append("FK")
            mark = (" " + "/".join(marks)) if marks else ""
            extra = f" 默认 {c['default']}" if c.get("default") else ""
            cmt = f"  // {c['comment']}" if c.get("comment") else ""
            parts.append(f"  - {c['name']}: {c['type']}{mark}{extra}{cmt}")

    for fk in schema.get("foreign_keys", []):
        if table and fk["table"] != table:
            continue
        if table_names and (fk["table"] not in table_names or fk["ref_table"] not in table_names):
            continue
        parts.append(f"- FK {fk['table']}.{fk['column']} → {fk['ref_table']}.{fk['ref_column']}")

    return "\n".join(parts)
