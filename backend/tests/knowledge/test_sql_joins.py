"""R15/R1 共享 SQL join 解析：图校验器（plausibility gate）与日志挖掘共用。

extract_join_pairs：sqlglot 解析 SQL 的 JOIN 等值列对 → [(t1,c1,t2,c2)]。
"""
from __future__ import annotations

from app.knowledge.graph.sql_joins import extract_join_pairs


def test_basic_join():
    pairs = extract_join_pairs("SELECT * FROM orders JOIN users ON orders.user_id = users.id")
    assert pairs == [("orders", "user_id", "users", "id")]


def test_aliased_join():
    pairs = extract_join_pairs(
        "SELECT * FROM orders o JOIN users u ON o.user_id = u.id")
    assert pairs == [("orders", "user_id", "users", "id")]


def test_reversed_condition():
    """ON 两侧颠倒：users.id = orders.user_id。"""
    pairs = extract_join_pairs(
        "SELECT * FROM orders JOIN users ON users.id = orders.user_id")
    assert pairs == [("orders", "user_id", "users", "id")]


def test_composite_join():
    """复合键：两条等值条件 → 两对列。"""
    pairs = extract_join_pairs(
        "SELECT * FROM order_lines JOIN orders "
        "ON order_lines.order_id = orders.id AND order_lines.line_no = orders.line_no")
    assert pairs == [
        ("order_lines", "order_id", "orders", "id"),
        ("order_lines", "line_no", "orders", "line_no"),
    ]


def test_three_table_join():
    pairs = extract_join_pairs(
        "SELECT * FROM a JOIN b ON a.b_id = b.id JOIN c ON b.c_id = c.id")
    assert pairs == [("a", "b_id", "b", "id"), ("b", "c_id", "c", "id")]


def test_single_table_no_join():
    assert extract_join_pairs("SELECT * FROM orders") == []


def test_unqualified_columns_skipped():
    """ON id = id（无表限定）→ 无法归属 → 跳过。"""
    assert extract_join_pairs("SELECT * FROM a JOIN b ON id = id") == []


def test_non_equi_join_skipped():
    """范围 join（非等值）→ 跳过（图校验只管等值）。"""
    assert extract_join_pairs(
        "SELECT * FROM scd JOIN dim ON scd.eff_start <= dim.date AND scd.eff_end >= dim.date") == []


def test_unparseable_returns_empty():
    assert extract_join_pairs("NOT VALID SQL {{{") == []