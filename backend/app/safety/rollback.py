"""回滚剧本 — A4 本地生成（不承诺时光机，仅给手段）。

UPDATE/DELETE：生成“受影响行导出” SELECT + 逆向语句模板；
INSERT：提示主键回放或无法自动回滚；
剧本自身过闸门校验（防剧本越权）。
"""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

def build_rollback(sql: str, dialect: str) -> dict[str, Any] | None:
    sql = (sql or "").strip()
    if not sql:
        return None
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except Exception:
        return None
    if not parsed or parsed[0] is None:
        return None
    node = parsed[0]
    # 仅处理 UPDATE/DELETE
    if isinstance(node, exp.Update):
        tbl = node.args.get("this")
        where = node.args.get("where")
        if tbl is None:
            return None
        table_sql = tbl.sql(dialect=dialect)
        # 备份导出
        if where is not None:
            cond = where.this.sql(dialect=dialect)
            backup_sql = f"SELECT * FROM {table_sql} WHERE {cond}"
            # 逆向：用备份的行数据还原（模板，需用户核对）
            rollback_sql = f"-- 回滚：用备份数据还原（示例，需核对）\n-- UPDATE {table_sql} SET ... WHERE {cond} -- 需用备份的列值回填"
        else:
            backup_sql = f"SELECT * FROM {table_sql}"
            rollback_sql = f"-- 回滚：全表备份 {table_sql}，请核对"
        return {
            "kind": "update",
            "backup_sql": backup_sql,
            "rollback_sql": rollback_sql,
            "note": "回滚剧本仅供参考，执行前请核对备份数据与 WHERE 范围",
        }
    if isinstance(node, exp.Delete):
        tbl = node.args.get("this")
        where = node.args.get("where")
        if tbl is None:
            return None
        # DELETE 的 this 可能是 table 或 from
        # 兼容：DELETE FROM tbl WHERE ...
        try:
            table_sql = tbl.sql(dialect=dialect)
            # 去掉可能的 "FROM" 前缀
            table_sql = re.sub(r"^\s*FROM\s+", "", table_sql, flags=re.I)
        except Exception:
            table_sql = str(tbl)
        if where is not None:
            cond = where.this.sql(dialect=dialect)
            backup_sql = f"SELECT * FROM {table_sql} WHERE {cond}"
            rollback_sql = f"-- 回滚：用备份数据 INSERT 回放\n-- INSERT INTO {table_sql} SELECT * FROM backup WHERE {cond}"
        else:
            backup_sql = f"SELECT * FROM {table_sql}"
            rollback_sql = f"-- 回滚：全表备份 {table_sql}"
        return {
            "kind": "delete",
            "backup_sql": backup_sql,
            "rollback_sql": rollback_sql,
            "note": "回滚剧本仅供参考，执行前请核对",
        }
    if isinstance(node, exp.Insert):
        # INSERT：无法自动回滚，给出主键删除建议
        tbl = node.args.get("this")
        try:
            table_sql = tbl.sql(dialect=dialect) if tbl else "unknown_table"
        except Exception:
            table_sql = "unknown_table"
        return {
            "kind": "insert",
            "backup_sql": None,
            "rollback_sql": f"-- 回滚：DELETE FROM {table_sql} WHERE <主键>=<插入值> -- 需填主键",
            "note": "INSERT 无法自动回滚，需按主键回放删除",
        }
    return None
