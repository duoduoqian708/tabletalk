"""安全闸门 gate 层测试：assess_sql + 影响行数预览查询构造。"""
from __future__ import annotations

import pytest

from app.safety.gate import assess_sql, preview_count_query, sqlglot_dialect_for
from app.safety.models import Origin, Verdict


@pytest.mark.parametrize(
    "sql,dialect,origin,expected",
    [
        ("SELECT * FROM products", "sqlite", Origin.AI, Verdict.ALLOW),
        ("UPDATE products SET price=price*1.1 WHERE stock=0", "sqlite", Origin.AI, Verdict.REVIEW),
        ("UPDATE products SET price=price*1.1", "sqlite", Origin.MANUAL, Verdict.BLOCK),
        ("DELETE FROM returns WHERE id=1", "postgres", Origin.AI, Verdict.REVIEW),
        ('INSERT INTO products VALUES (1,2,3,4,5,6)', "mysql", Origin.MANUAL, Verdict.REVIEW),
        ("CREATE INDEX x ON products(id)", "postgres", Origin.MANUAL, Verdict.REVIEW),
        ("CREATE INDEX x ON products(id)", "postgres", Origin.AI, Verdict.BLOCK),
    ],
)
def test_assess_sql(sql, dialect, origin, expected):
    a = assess_sql(sql, sqlglot_dialect_for(dialect), origin)
    assert a.verdict == expected
    assert isinstance(a.to_dict()["tier"], str)


def test_parse_failure_never_allows():
    a = assess_sql("SELECT * FROM WHERE", "sqlite", Origin.AI)
    assert a.verdict != Verdict.ALLOW
    assert a.parse_error is not None


@pytest.mark.parametrize(
    "sql,dialect,want",
    [
        ("UPDATE products SET price=price*1.1 WHERE stock=0", "sqlite",
         "SELECT COUNT(*) AS _n FROM products WHERE stock = 0"),
        ("DELETE FROM orders WHERE id=5", "sqlite",
         "SELECT COUNT(*) AS _n FROM orders WHERE id = 5"),
        ('UPDATE "orders" SET status=1 WHERE "id"=3', "postgres",
         'SELECT COUNT(*) AS _n FROM "orders" WHERE "id" = 3'),
    ],
)
def test_preview_count_query(sql, dialect, want):
    assert preview_count_query(sql, sqlglot_dialect_for(dialect)) == want


def test_preview_count_query_none_when_no_where():
    assert preview_count_query("UPDATE products SET price=1", "sqlite") is None
    assert preview_count_query("INSERT INTO products VALUES (1)", "sqlite") is None
    assert preview_count_query("SELECT * FROM products", "sqlite") is None


async def test_preview_rows_timeout_returns_none(app_state, conn_id, monkeypatch):
    """亿级表的 COUNT 超时：不抛错，返回 None（UI 显示"无法预估"）。"""
    import asyncio

    from app.safety.gate import preview_rows

    async def slow_execute(_conn_id, _sql):
        await asyncio.sleep(5.0)
        raise AssertionError("不应到达")

    monkeypatch.setattr(app_state.pools, "execute", slow_execute)
    app_state.env.gate_preview_timeout = 0.05
    assert await preview_rows(app_state, conn_id, "UPDATE products SET price=1 WHERE id=1", "sqlite") is None


def test_suggest_safe_gives_alternatives():
    from app.safety.gate import suggest_safe

    suggests = suggest_safe("UPDATE orders SET status='paid'", "sqlite", Origin.MANUAL)
    assert any("WHERE" in s for s in suggests)


def test_ai_ddl_blocked_defense_in_depth():
    a = assess_sql("DROP TABLE orders", "sqlite", Origin.AI)
    assert a.verdict == Verdict.BLOCK
    assert any(r.rule == "ddl-ai" for r in a.rules)
