"""查询执行与序列化测试（真实 SQLite）。"""
from __future__ import annotations

from app.core import query as core_query


async def test_execute_select(app_state, conn_id):
    res = await core_query.execute(app_state, conn_id, "SELECT * FROM orders LIMIT 5")
    assert res["columns"]
    assert len(res["rows"]) <= 5
    assert res["row_count"] <= 5
    assert not res["is_dml"]


async def test_execute_dml_affected_rows(app_state, conn_id):
    res = await core_query.execute(app_state, conn_id, "UPDATE products SET stock=stock WHERE id=1")
    assert res["is_dml"] is True
    assert res["affected_rows"] == 1


async def test_row_cap_truncation(app_state, conn_id):
    res = await core_query.execute(app_state, conn_id, "SELECT * FROM orders", max_rows=3)
    assert len(res["rows"]) == 3
    assert res["truncated"] is True


async def test_serialization_types(app_state, conn_id):
    res = await core_query.execute(app_state, conn_id, "SELECT id, total_amount FROM orders LIMIT 2")
    assert res["types"][0] == "int"
    assert res["types"][1] == "numeric"


async def test_big_int_serialized_as_string(app_state, conn_id):
    """超出 JS 安全整数的 INTEGER 序列化为字符串，防止浏览器精度丢失。"""
    res = await core_query.execute(
        app_state, conn_id,
        "SELECT big_int, label FROM big_values WHERE label='超大整数'"
    )
    # 9223372036854775807 (2^63-1) 超过 2^53，必须转 str 且原样保留
    assert res["rows"][0][0] == "9223372036854775807"
    assert res["types"][0] == "int"
    # 小整数不受影响
    res2 = await core_query.execute(app_state, conn_id, "SELECT id FROM orders LIMIT 1")
    assert isinstance(res2["rows"][0][0], int)


async def test_count_total(app_state, conn_id):
    total = await core_query.count_total(app_state, conn_id, "SELECT * FROM orders")
    assert total >= 1


async def test_limit_offset_paging(app_state, conn_id):
    p1 = await core_query.execute(app_state, conn_id, "SELECT id FROM orders ORDER BY id", limit=2, offset=0)
    p2 = await core_query.execute(app_state, conn_id, "SELECT id FROM orders ORDER BY id", limit=2, offset=2)
    assert len(p1["rows"]) == 2
    assert len(p2["rows"]) == 2
    assert p1["rows"][0][0] != p2["rows"][0][0]


def test_auto_cap_injects_limit():
    sql = core_query._auto_cap("SELECT * FROM orders", "sqlite", 100)
    assert "LIMIT 101" in sql.upper()


def test_auto_cap_skips_explicit_limit():
    sql = core_query._auto_cap("SELECT * FROM orders LIMIT 5", "sqlite", 100)
    assert "LIMIT 5" in sql.upper()
    assert "LIMIT 101" not in sql.upper()


def test_auto_cap_skips_non_select():
    assert core_query._auto_cap("UPDATE orders SET x=1", "sqlite", 100) == "UPDATE orders SET x=1"


async def test_auto_cap_bounds_no_limit_select(app_state, conn_id):
    res = await core_query.execute(app_state, conn_id, "SELECT * FROM orders", max_rows=3)
    assert len(res["rows"]) == 3
    assert res["truncated"] is True


async def test_auto_cap_leaves_explicit_limit_alone(app_state, conn_id):
    res = await core_query.execute(app_state, conn_id, "SELECT * FROM orders LIMIT 2", max_rows=100)
    assert len(res["rows"]) == 2
    assert res["truncated"] is False
