"""sample_values 分列抽样测试（T5）：度量列主键倒序整行，枚举列 DISTINCT。"""
from __future__ import annotations

import sqlite3

from app.core.schema import sample_values

ORDER_COLUMNS = {"id", "customer_id", "address_id", "campaign_id", "status", "created_at", "total_amount"}
# 度量/时间列：走整行主键倒序（最近样本，与 id 对齐）
METRIC_COLUMNS = {"customer_id", "address_id", "campaign_id", "created_at", "total_amount"}


async def test_sample_values_pk_desc_whole_row(app_state, conn_id):
    """R4：demo orders（40 行 ≤ 50）小表全表 DISTINCT——全量去重，不按时间偏置。"""
    samples = await sample_values(app_state, conn_id, "orders", per_column=5)
    assert set(samples.keys()) == ORDER_COLUMNS
    ids = samples["id"]
    # 小表信号：全量去重 = 40 个 id 全覆盖（不受 per_column=5 限制）
    assert len(ids) == 40 and set(ids) == set(range(1, 41))
    # 各列独立 DISTINCT：样本数 ≤ 40 且与自身去重一致
    for col in METRIC_COLUMNS:
        vals = samples[col]
        assert len(vals) <= 40 and len(vals) == len(set(vals)), f"{col} 应为去重后样本"
    # 枚举列（status）：同样全量去重，无重复
    assert len(samples["status"]) == len(set(samples["status"]))


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
    # R4：7 行小表 → 全量 DISTINCT（无主键回退同样生效）
    assert sorted(samples["seq"]) == [1, 2, 3, 4, 5, 6, 7]
    assert len(samples["msg"]) == 7  # 全量去重（m1..m7 各一）


async def test_sample_values_large_table_recent_path(app_state, tmp_path):
    """R4 对照：大表（>50 行）保持整行主键倒序（最近样本路径）。"""
    import sqlite3
    from app.core.schema import sample_values

    p = tmp_path / "big.db"
    con = sqlite3.connect(str(p))
    con.executescript("CREATE TABLE big (id INTEGER PRIMARY KEY, val REAL)")
    con.executemany("INSERT INTO big VALUES (?,?)", [(i, float(i)) for i in range(1, 81)])
    con.commit()
    con.close()
    c = app_state.connections.create({"name": "big", "dialect": "sqlite", "file": str(p)})
    samples = await sample_values(app_state, c.id, "big", per_column=5)
    assert samples["id"] == [80, 79, 78, 77, 76]  # 最近 5 行（非 DISTINCT）
