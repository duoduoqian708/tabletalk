"""SQLite 方言适配器。端到端可测的演示路径。"""
from __future__ import annotations

from typing import Any

import aiosqlite

from app.core.dialects.base import (
    ColumnRef,
    DialectAdapter,
    DialectConfig,
    FKRef,
    RawResult,
    TableRef,
)
from app.core.dialects.registry import register_dialect


@register_dialect
class SQLiteAdapter(DialectAdapter):
    name = "sqlite"
    sqlglot_name = "sqlite"

    async def connect(self, cfg: DialectConfig) -> aiosqlite.Connection:
        if not cfg.file:
            raise ValueError("SQLite 连接需要 file 路径")
        conn = await aiosqlite.connect(cfg.file, timeout=cfg.timeout)
        await conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def close(self, conn: aiosqlite.Connection) -> None:
        try:
            await conn.close()
        except Exception:
            pass

    async def is_healthy(self, conn: aiosqlite.Connection) -> bool:
        try:
            await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def execute(self, conn: aiosqlite.Connection, sql: str) -> RawResult:
        sql = sql.strip().rstrip(";")
        if not sql:
            return RawResult()
        cursor = await conn.execute(sql)
        if cursor.description:
            columns = [d[0] for d in cursor.description]
            rows = await cursor.fetchall()
            rows = [list(r) for r in rows]
            return RawResult(columns=columns, types=[""] * len(columns), rows=rows)
        await conn.commit()
        return RawResult(rowcount=cursor.rowcount, is_dml=True)

    async def list_tables(self, conn: aiosqlite.Connection) -> list[TableRef]:
        cur = await conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        rows = await cur.fetchall()
        return [TableRef(name=r[0], kind=r[1]) for r in rows]

    async def list_columns(self, conn: aiosqlite.Connection, table: str) -> list[ColumnRef]:
        cur = await conn.execute(f'PRAGMA table_info("{table}")')
        rows = await cur.fetchall()
        cols: list[ColumnRef] = []
        for cid, name, ctype, notnull, dflt, pk in rows:
            cols.append(
                ColumnRef(
                    table=table,
                    name=name,
                    data_type=ctype or "text",
                    nullable=not bool(notnull),
                    is_pk=bool(pk),
                    default=dflt,
                    comment="",
                )
            )
        return cols

    async def list_foreign_keys(self, conn: aiosqlite.Connection) -> list[FKRef]:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = [r[0] for r in await cur.fetchall()]
        fks: list[FKRef] = []
        for t in tables:
            cur = await conn.execute(f'PRAGMA foreign_key_list("{t}")')
            for _id, _seq, ref_table, from_col, to_col, *_rest in await cur.fetchall():
                fks.append(
                    FKRef(table=t, column=from_col, ref_table=ref_table,
                          ref_column=to_col or "id", constraint_id=_id)
                )
        return fks

    async def count_rows(self, conn: aiosqlite.Connection, table: str) -> int:
        cur = await conn.execute(f'SELECT COUNT(*) FROM "{table}"')
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    def quote_ident(self, name: str) -> str:
        return f'"{name}"'

    def quote_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        return "'" + str(value).replace("'", "''") + "'"

    async def explain(self, conn: aiosqlite.Connection, sql: str) -> dict[str, Any]:
        # SQLite EXPLAIN QUERY PLAN 没有行数，需用 SCAN/SEARCH 判别 + 已知 row_count 估算
        try:
            cur = await conn.execute(f"EXPLAIN QUERY PLAN {sql}")
            rows = await cur.fetchall()
            detail = " | ".join(str(r[3]) for r in rows) if rows else ""
            is_scan = any("SCAN" in str(r[3]).upper() for r in rows)
            # 估算：若为 SCAN，则上界为表行数（需调用方提供）；此处返回 is_scan 标记，由调用方结合 row_count 估算
            return {"estimated_rows": None, "is_scan": is_scan, "detail": detail}
        except Exception as e:
            return {"estimated_rows": None, "is_scan": False, "detail": f"explain failed: {e}"}
