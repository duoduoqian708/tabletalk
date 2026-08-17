"""安全闸门规则引擎测试。"""
from __future__ import annotations

import pytest

from app.safety.models import Origin, Verdict
from app.safety.rules import aggregate, run_rules
from app.safety.parser import parse_sql


def assess(sql, origin=Origin.MANUAL, dialect="sqlite"):
    infos = parse_sql(sql, dialect)
    rules = run_rules(infos, origin)
    return aggregate(infos, rules), infos


@pytest.mark.parametrize(
    "sql,origin,expected",
    [
        # 读
        ("SELECT * FROM orders", Origin.MANUAL, Verdict.ALLOW),
        ("SELECT * FROM orders", Origin.AI, Verdict.ALLOW),
        # 写：无 WHERE 拦截
        ("UPDATE orders SET status='paid'", Origin.MANUAL, Verdict.BLOCK),
        ("DELETE FROM orders", Origin.MANUAL, Verdict.BLOCK),
        # 写：有 WHERE 需确认
        ("UPDATE orders SET status='paid' WHERE id=1", Origin.MANUAL, Verdict.REVIEW),
        ("DELETE FROM orders WHERE id=1", Origin.AI, Verdict.REVIEW),
        # INSERT 需确认
        ("INSERT INTO orders (id) VALUES (1)", Origin.MANUAL, Verdict.REVIEW),
        # DDL
        ("DROP TABLE orders", Origin.MANUAL, Verdict.REVIEW),
        ("DROP TABLE orders", Origin.AI, Verdict.BLOCK),
        ("CREATE TABLE t (x int)", Origin.AI, Verdict.BLOCK),
        ("ALTER TABLE orders ADD COLUMN x int", Origin.MANUAL, Verdict.REVIEW),
        ("TRUNCATE TABLE orders", Origin.AI, Verdict.BLOCK),
        # 事务控制
        ("BEGIN", Origin.MANUAL, Verdict.REVIEW),
        # 解析失败 → 按写处理（需确认，绝不 ALLOW）
        ("SELECT * FROM WHERE", Origin.MANUAL, Verdict.REVIEW),
    ],
)
def test_verdicts(sql, origin, expected):
    agg, _ = assess(sql, origin)
    assert agg["verdict"] == expected, agg


def test_multi_statement_all_read_allowed():
    agg, _ = assess("SELECT 1; SELECT 2", Origin.MANUAL)
    assert agg["verdict"] == Verdict.ALLOW


def test_multi_statement_with_write_blocked():
    agg, _ = assess("SELECT 1; DROP TABLE orders", Origin.MANUAL)
    assert agg["verdict"] == Verdict.BLOCK


def test_tier_classification():
    agg, _ = assess("UPDATE orders SET x=1 WHERE id=1")
    assert agg["tier"].value == "dml"
    agg2, _ = assess("SELECT * FROM orders")
    assert agg2["tier"].value == "read"
    agg3, _ = assess("DROP TABLE orders", Origin.MANUAL)
    assert agg3["tier"].value == "ddl"


def test_reasons_non_empty_on_block():
    agg, _ = assess("UPDATE orders SET status='paid'")
    assert agg["reasons"]
    assert any("WHERE" in r for r in agg["reasons"])
