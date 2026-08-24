"""sample_values 主键倒序整行抽样测试（真实 SQLite demo 库）。"""
from __future__ import annotations

import sqlite3

from app.core.schema import sample_values

ORDER_COLUMNS = {"id", "customer_id", "address_id", "campaign_id", "status", "created_at", "total_amount"}


async def test_sample_values_pk_desc_whole_row(app_state, conn_id):
    samples = await sample_values(app_state, conn_id, "orders", per_column=5)
    assert set(samples.keys()) == ORDER_COLUMNS
    ids = samples["id"]
    assert ids == sorted(ids, reverse=True)
    assert ids == [40, 39, 38, 37, 36]
    # 整行抽样：各列样本数一致（同一批行按列拆分）
    assert all(len(v) == 5 for v in samples.values())
    assert len(set(ids)) == 5


async def test_sample_values_no_pk_fallback(app_state, demo_db):
    with sqlite3.connect(demo_db) as cx:
        cx.execute("CREATE TABLE no_pk_log (msg TEXT, seq INTEGER)")
        cx.executemany(
            "INSERT INTO no_pk_log VALUES (?,?)",
            [(f"m{i}", i) for i in range(1, 8)],
        )
    c = app_state.connections.create({"name": "demo-nopk", "dialect": "sqlite", "file": str(demo_db)})
    samples = await sample_values(app_state, c.id, "no_pk_log", per_column=3)
    assert set(samples.keys()) == {"msg", "seq"}
    assert sorted(samples["seq"]) == [1, 2, 3]
