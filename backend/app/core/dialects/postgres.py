"""PostgreSQL 方言适配器（psycopg3 async）。本机无 PG 服务，集成测试靠 Docker。"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import tuple_row

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
class PostgresAdapter(DialectAdapter):
    name = "postgres"
    sqlglot_name = "postgres"

    async def connect(self, cfg: DialectConfig) -> Any:
        conn = await psycopg.AsyncConnection.connect(
            host=cfg.host or None,
            port=cfg.port or None,
            user=cfg.user or None,
            password=cfg.password or None,
            dbname=cfg.database or None,
            connect_timeout=cfg.timeout,
            row_factory=tuple_row,
        )
        return conn

    async def set_read_only(self, conn: Any, flag: bool) -> None:
        # psycopg3 async 连接的 read_only 是只读属性，必须用 set_read_only()
        await conn.set_read_only(flag)

    async def close(self, conn: Any) -> None:
        try:
            await conn.close()
        except Exception:
            pass

    async def is_healthy(self, conn: Any) -> bool:
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                return True
        except Exception:
            return False

    async def execute(self, conn: Any, sql: str) -> RawResult:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            if cur.description:
                columns = [d.name for d in cur.description]
                rows = list(await cur.fetchall())
                rows = [list(r) for r in rows]
                return RawResult(columns=columns, types=[""] * len(columns), rows=rows)
            return RawResult(rowcount=cur.rowcount or 0, is_dml=True)

    async def list_tables(self, conn: Any) -> list[TableRef]:
        sql = """
            SELECT c.relname AS name,
                   CASE WHEN c.relkind = 'v' THEN 'view' ELSE 'table' END AS kind,
                   COALESCE(obj_description(c.oid), '') AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r','v','p','m')
            ORDER BY c.relname
        """
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return [TableRef(name=r[0], kind=r[1], comment=r[2]) for r in await cur.fetchall()]

    async def list_columns(self, conn: Any, table: str) -> list[ColumnRef]:
        sql = """
            SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                   col_description(a.attrelid, a.attnum),
                   EXISTS (SELECT 1 FROM information_schema.table_constraints tc
                           JOIN information_schema.key_column_usage kcu
                             ON tc.constraint_name = kcu.constraint_name
                           WHERE tc.table_name = c.table_name AND tc.constraint_type='PRIMARY KEY'
                             AND kcu.column_name = c.column_name) AS is_pk
            FROM information_schema.columns c
            JOIN pg_attribute a ON a.attrelid = quote_ident(c.table_name)::regclass
                               AND a.attname = c.column_name
            WHERE c.table_schema = 'public' AND c.table_name = %s
            ORDER BY c.ordinal_position
        """
        async with conn.cursor() as cur:
            await cur.execute(sql, (table,))
            rows = await cur.fetchall()
        cols: list[ColumnRef] = []
        for name, dtype, nullable, default, comment, is_pk in rows:
            cols.append(
                ColumnRef(
                    table=table,
                    name=name,
                    data_type=dtype,
                    nullable=nullable == "YES",
                    is_pk=bool(is_pk),
                    default=default,
                    comment=comment or "",
                )
            )
        return cols

    async def list_foreign_keys(self, conn: Any) -> list[FKRef]:
        sql = """
            SELECT conrelid::regclass::text AS tbl,
                   a.attname AS col,
                   confrelid::regclass::text AS ref_tbl,
                   af.attname AS ref_col
            FROM pg_constraint con
            JOIN pg_attribute a  ON a.attnum = ANY(con.conkey) AND a.attrelid = con.conrelid
            JOIN pg_attribute af ON af.attnum = ANY(con.confkey) AND af.attrelid = con.confrelid
            WHERE con.contype = 'f'
        """
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return [FKRef(table=r[0], column=r[1], ref_table=r[2], ref_column=r[3]) for r in await cur.fetchall()]

    async def count_rows(self, conn: Any, table: str) -> int:
        from psycopg.rows import dict_row

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT COUNT(*) AS n FROM {self.quote_ident(table)}')
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    def quote_ident(self, name: str) -> str:
        return f'"{name.replace(chr(34), chr(34)*2)}"'

    def quote_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        return "'" + str(value).replace("'", "''") + "'"

    async def explain(self, conn: Any, sql: str) -> dict[str, Any]:
        try:
            import json
            async with conn.cursor() as cur:
                await cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                row = await cur.fetchone()
                data = row[0] if row else ""
                if isinstance(data, str):
                    data = json.loads(data)
                # data is list with one plan
                plan = data[0]["Plan"] if isinstance(data, list) else data.get("Plan", {})
                rows = plan.get("Plan Rows", 0)
                return {"estimated_rows": int(rows), "is_scan": "Seq Scan" in str(data), "detail": str(data)[:600]}
        except Exception as e:
            return {"estimated_rows": None, "is_scan": False, "detail": f"explain failed: {e}"}
