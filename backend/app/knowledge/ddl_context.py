"""DDL 上下文生成：为 AI annotation 提供实时建表 DDL（不持久化）。

复用 export_ddl 的连接池逻辑，但针对批量场景优化：一次连接读取所有表的 DDL，
供 annotator.py 的 prompt 使用。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.state import AppState

# 构建图谱时过滤的噪音列（时间戳/审计人/软删除标记），减少 LLM 干扰
NOISE_COLUMNS: set[str] = {
    # 时间戳
    "created_at", "updated_at", "deleted_at", "modified_at",
    "create_time", "update_time", "delete_time", "modify_time",
    "gmt_create", "gmt_modified", "gmt_deleted",
    # 审计人
    "created_by", "updated_by", "deleted_by", "modified_by",
    "create_user", "update_user", "operator", "creator", "modifier",
    # 软删除标记
    "is_deleted", "deleted", "del_flag", "is_active", "is_removed",
}


def _is_noise_column(col_name: str) -> bool:
    """判断列名是否为噪音列（时间戳/审计人/软删除标记）。"""
    return col_name.lower() in NOISE_COLUMNS


async def generate_ddl(state: "AppState", conn_id: str, table: str) -> str:
    """实时生成单表 DDL（字符串），供逐表 AI 注释 prompt 使用。"""
    result = await _generate_ddls_batch(state, conn_id, [table])
    return result.get(table, "")


async def generate_ddls_all(state: "AppState", conn_id: str) -> dict[str, str]:
    """一次性生成所有表的 DDL，返回 {table_name: ddl_string}。"""
    from app.core.schema import get_schema
    schema = await get_schema(state, conn_id)
    table_names = [t["name"] for t in schema.get("tables", [])]
    if not table_names:
        return {}
    return await _generate_ddls_batch(state, conn_id, table_names)


def build_ddl_overview(schema: dict[str, Any]) -> str:
    """从已有 schema 构建精简版表结构概览（不走连接池），供全局标签 prompt 使用。

    格式：
    - users: id(PK) INTEGER, name VARCHAR, email VARCHAR
      FK: none
      comment: 用户主表
    """
    fk_map: dict[str, list[str]] = {}
    for fk in schema.get("foreign_keys", []):
        fk_map.setdefault(fk["table"], []).append(
            f"{fk['column']} → {fk['ref_table']}.{fk['ref_column']}"
        )

    parts: list[str] = []
    for t in schema.get("tables", []):
        name = t["name"]
        cols = [c for c in schema.get("columns", []) if c["table"] == name]
        col_parts = []
        for c in cols:
            label = c["name"]
            if c.get("pk"):
                label += "(PK)"
            label += f" {c.get('type', '')}"
            col_parts.append(label)
        fks = fk_map.get(name, [])
        comment = t.get("comment", "")
        line = f"- {name}: {', '.join(col_parts)}"
        line += f"\n  FK: {'; '.join(fks) if fks else 'none'}"
        if comment:
            line += f"\n  comment: {comment}"
        parts.append(line)

    return "\n".join(parts)


async def _generate_ddls_batch(state: "AppState", conn_id: str, tables: list[str]) -> dict[str, str]:
    """一次连接生成多表 DDL，返回 {table_name: ddl_string}。"""

    def _work(adapter, conn):
        async def inner():
            quote = adapter.quote_ident
            literal = adapter.quote_literal
            all_tables = await adapter.list_tables(conn)
            all_columns = []
            for t in all_tables:
                all_columns += await adapter.list_columns(conn, t.name)
            all_fks = await adapter.list_foreign_keys(conn)

            table_map = {t.name: t for t in all_tables}
            fks_by_table: dict[str, list] = {}
            for fk in all_fks:
                fks_by_table.setdefault(fk.table, []).append(fk)

            result: dict[str, str] = {}
            for tbl in tables:
                tab = table_map.get(tbl)
                if tab is None:
                    continue
                cols = [c for c in all_columns if c.table == tbl]
                tbl_fks = fks_by_table.get(tbl, [])

                lines = [f"CREATE TABLE {quote(tbl)} ("]
                defs = []
                for c in cols:
                    parts = [quote(c.name), c.data_type]
                    if c.is_pk:
                        parts.append("PRIMARY KEY")
                    elif c.nullable is False:
                        parts.append("NOT NULL")
                    if c.default is not None:
                        parts.append(f"DEFAULT {literal(c.default)}")
                    if c.comment:
                        parts.append(f"/* {c.comment} */")
                    defs.append("  " + " ".join(parts))
                for fk in tbl_fks:
                    defs.append(
                        f"  CONSTRAINT fk_{tbl}_{fk.column} FOREIGN KEY ({quote(fk.column)}) "
                        f"REFERENCES {quote(fk.ref_table)} ({quote(fk.ref_column)})"
                    )
                lines.append(",\n".join(defs))
                lines.append(");")
                result[tbl] = "\n".join(lines)

            return result

        return inner()

    return await state.pools.run(conn_id, _work)


def build_graph_overview(schema: dict[str, Any], filter_noise: bool = True) -> str:
    """为 LLM 图谱识别构建精简表结构概览，可选过滤噪音列。

    与 build_ddl_overview 的区别：过滤时间戳/审计人/软删除列，减少 LLM 干扰。
    """
    fk_map: dict[str, list[str]] = {}
    for fk in schema.get("foreign_keys", []):
        fk_map.setdefault(fk["table"], []).append(
            f"{fk['column']} → {fk['ref_table']}.{fk['ref_column']}"
        )

    parts: list[str] = []
    for t in schema.get("tables", []):
        name = t["name"]
        cols = [c for c in schema.get("columns", []) if c["table"] == name]
        col_parts = []
        for c in cols:
            if filter_noise and _is_noise_column(c["name"]):
                continue
            label = c["name"]
            if c.get("pk"):
                label += "(PK)"
            label += f" {c.get('type', '')}"
            col_parts.append(label)
        fks = fk_map.get(name, [])
        comment = t.get("comment", "")
        line = f"- {name}: {', '.join(col_parts)}"
        if fks:
            line += f"\n  FK: {'; '.join(fks)}"
        if comment:
            line += f"\n  comment: {comment}"
        parts.append(line)

    return "\n".join(parts)
