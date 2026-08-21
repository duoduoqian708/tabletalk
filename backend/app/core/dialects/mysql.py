"""MySQL 方言适配器（aiomysql）。本机无 MySQL 服务，集成测试靠 Docker。"""
from __future__ import annotations

from typing import Any

import aiomysql

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
class MySQLAdapter(DialectAdapter):
    name = "mysql"
    sqlglot_name = "mysql"

    async def connect(self, cfg: DialectConfig) -> Any:
        conn = await aiomysql.connect(
            host=cfg.host or "127.0.0.1",
            port=cfg.port or 3306,
            user=cfg.user or "",
            password=cfg.password or "",
            db=cfg.database or None,
            connect_timeout=cfg.timeout,
            autocommit=True,
        )
        if cfg.read_only:
            async with conn.cursor() as cur:
                await cur.execute("SET SESSION TRANSACTION READ ONLY")
        return conn

    async def close(self, conn: Any) -> None:
        try:
            conn.close()
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
                columns = [d[0] for d in cur.description]
                rows = list(await cur.fetchall())
                rows = [list(r) for r in rows]
                return RawResult(columns=columns, types=[""] * len(columns), rows=rows)
            return RawResult(rowcount=cur.rowcount or 0, is_dml=True)

    async def list_tables(self, conn: Any) -> list[TableRef]:
        sql = """
            SELECT TABLE_NAME, TABLE_TYPE, COALESCE(TABLE_COMMENT, '')
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME
        """
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
        return [
            TableRef(
                name=r[0],
                kind="view" if r[1] == "VIEW" else "table",
                comment=r[2],
            )
            for r in rows
        ]

    async def list_columns(self, conn: Any, table: str) -> list[ColumnRef]:
        sql = """
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT,
                   (COLUMN_KEY = 'PRI') AS is_pk
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
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
            SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE kcu
            WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
        """
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
        return [FKRef(table=r[0], column=r[1], ref_table=r[2], ref_column=r[3]) for r in rows]

    async def count_rows(self, conn: Any, table: str) -> int:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT COUNT(*) AS n FROM {self.quote_ident(table)}")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    def quote_ident(self, name: str) -> str:
        return f"`{name.replace('`', '``')}`"

    def quote_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        return "'" + str(value).replace("'", "''") + "'"

    async def explain(self, conn: Any, sql: str) -> dict[str, Any]:
        try:
            async with conn.cursor() as cur:
                await cur.execute(f"EXPLAIN {sql}")
                rows = await cur.fetchall()
                # MySQL EXPLAIN: rows_estimate is in column 9 or 10
                detail = " | ".join(str(r) for r in rows[:2])[:600]
                # 估算：取 rows 列的最大值
                est = None
                for r in rows:
                    # MySQL's EXPLAIN output has 'rows' as 9th col (index 9)
                    try:
                        # try to find a numeric rows estimate
                        for v in r:
                            if isinstance(v, int) and v > 0:
                                est = v if est is None else max(est, v)
                    except Exception:
                        pass
                return {"estimated_rows": est, "is_scan": "ALL" in detail, "detail": detail}
        except Exception as e:
            return {"estimated_rows": None, "is_scan": False, "detail": f"explain failed: {e}"}
