"""安全闸门解析层测试。"""
from __future__ import annotations

import pytest

from app.safety.parser import parse_sql


@pytest.mark.parametrize(
    "sql,kind,stmt",
    [
        ("SELECT * FROM orders", "read", "select"),
        ("select id from orders limit 10", "read", "select"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "read", "select"),
        ("SHOW TABLES", "read", "show"),
        ("EXPLAIN SELECT 1", "read", "explain"),
        ("DESCRIBE orders", "read", "describe"),
        ("PRAGMA table_info(orders)", "read", "pragma"),
        ("UPDATE orders SET status='paid' WHERE id=1", "dml", "update"),
        ("DELETE FROM orders WHERE id=1", "dml", "delete"),
        ("INSERT INTO orders (id) VALUES (1)", "dml", "insert"),
        ("CREATE TABLE t (x int)", "ddl", "create"),
        ("DROP TABLE orders", "ddl", "drop"),
        ("ALTER TABLE orders ADD COLUMN x int", "ddl", "alter"),
        ("TRUNCATE TABLE orders", "ddl", "truncate"),
        ("BEGIN", "tcl", "begin"),
        ("COMMIT", "tcl", "commit"),
        ("ROLLBACK", "tcl", "rollback"),
    ],
)
def test_statement_kind(sql, kind, stmt):
    infos = parse_sql(sql, "sqlite")
    assert infos[0].kind == kind
    assert infos[0].stmt_type == stmt


def test_parse_failure_defaults_unknown():
    infos = parse_sql("SELECT * FROM WHERE", "sqlite")
    assert infos[0].kind == "unknown"
    assert infos[0].parse_error is not None


def test_multi_statement_split():
    infos = parse_sql("SELECT 1; SELECT 2", "sqlite")
    assert len(infos) == 2


def test_where_and_limit_flags():
    i1 = parse_sql("UPDATE orders SET x=1 WHERE id=1", "sqlite")[0]
    assert i1.has_where is True
    i2 = parse_sql("UPDATE orders SET x=1", "sqlite")[0]
    assert i2.has_where is False
    i3 = parse_sql("SELECT * FROM orders LIMIT 5", "sqlite")[0]
    assert i3.has_limit is True


def test_table_extraction():
    i = parse_sql("SELECT * FROM orders o JOIN customers c ON o.customer_id=c.id", "sqlite")[0]
    assert "orders" in i.tables
    assert "customers" in i.tables


def test_dialect_variants():
    # postgres 双引号标识符 + 类型转换
    infos = parse_sql('UPDATE "orders" SET status = \'paid\' WHERE id = 1', "postgres")
    assert infos[0].kind == "dml"
    # mysql 反引号
    infos2 = parse_sql("UPDATE `orders` SET status='paid' WHERE id=1", "mysql")
    assert infos2[0].kind == "dml"
