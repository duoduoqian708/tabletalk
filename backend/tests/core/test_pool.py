"""连接池行为测试（SQLite 单连接串行 + 并发正确性）。"""
from __future__ import annotations

import asyncio

from app.core import query as core_query


async def test_concurrent_reads_serialized_sqlite(app_state, conn_id):
    """SQLite 池大小=1：10 个并发读在池内排队，全部正确返回。"""
    async def one(_i: int) -> int:
        res = await core_query.execute(app_state, conn_id, "SELECT id FROM orders LIMIT 1")
        return len(res["rows"])

    results = await asyncio.gather(*[one(i) for i in range(10)])
    assert results == [1] * 10


async def test_concurrent_mixed_read_write(app_state, conn_id):
    """并发读写混合：写走 DML 行数，读走 SELECT，互不串扰。"""
    async def write(i: int) -> int:
        raw = await app_state.pools.execute(
            conn_id, f"UPDATE orders SET status = status WHERE id = {i + 1}"
        )
        return raw.rowcount

    async def read() -> int:
        res = await core_query.execute(app_state, conn_id, "SELECT COUNT(*) AS n FROM orders")
        return res["rows"][0][0]

    w = await asyncio.gather(*[write(i) for i in range(3)])
    r = await asyncio.gather(*[read() for _ in range(5)])
    assert w == [1, 1, 1]
    assert all(x >= 1 for x in r)
