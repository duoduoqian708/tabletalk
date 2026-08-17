"""PostgreSQL 适配器集成测试（docker-gated，见同目录 conftest）。"""
from __future__ import annotations

import pytest

from app.core.dialects.base import DialectConfig
from app.core.dialects.postgres import PostgresAdapter


@pytest.fixture
async def conn(pg_cfg):
    adapter = PostgresAdapter()
    c = await adapter.connect(DialectConfig(**pg_cfg))
    yield adapter, c
    await adapter.close(c)


async def test_connect_health_and_roundtrip(conn):
    adapter, c = conn
    assert await adapter.is_healthy(c)
    await adapter.execute(c, "DROP TABLE IF EXISTS it_orders")
    await adapter.execute(c, "CREATE TABLE it_orders (id serial PRIMARY KEY, name text NOT NULL, amount numeric(10,2))")
    await adapter.execute(c, "INSERT INTO it_orders (name, amount) VALUES ('a', 1.5), ('b', 2.5)")
    raw = await adapter.execute(c, "SELECT name, amount FROM it_orders ORDER BY name")
    assert raw.columns == ["name", "amount"]
    assert raw.rows == [["a", 1.5], ["b", 2.5]]
    dml = await adapter.execute(c, "UPDATE it_orders SET amount = 9.9 WHERE name = 'a'")
    assert dml.is_dml is True
    assert dml.rowcount == 1
    await adapter.execute(c, "DROP TABLE it_orders")


async def test_schema_discovery_with_fk(conn):
    adapter, c = conn
    await adapter.execute(c, "DROP TABLE IF EXISTS it_orders CASCADE")
    await adapter.execute(c, "DROP TABLE IF EXISTS it_customers CASCADE")
    await adapter.execute(c, "CREATE TABLE it_customers (id serial PRIMARY KEY, name text)")
    await adapter.execute(c, "CREATE TABLE it_orders (id serial PRIMARY KEY, customer_id int REFERENCES it_customers(id), note text)")
    tables = {t.name for t in await adapter.list_tables(c)}
    assert {"it_orders", "it_customers"} <= tables
    cols = {col.name for col in await adapter.list_columns(c, "it_orders")}
    assert cols == {"id", "customer_id", "note"}
    fks = {f.table: f for f in await adapter.list_foreign_keys(c)}
    assert fks["it_orders"].column == "customer_id"
    assert fks["it_orders"].ref_table == "it_customers"
    await adapter.execute(c, "DROP TABLE it_orders")
    await adapter.execute(c, "DROP TABLE it_customers")


async def test_read_only_blocks_writes(pg_cfg):
    adapter = PostgresAdapter()
    c = await adapter.connect(DialectConfig(**pg_cfg, read_only=True))
    try:
        with pytest.raises(Exception):
            await adapter.execute(c, "CREATE TABLE it_ro (id int)")
    finally:
        await adapter.close(c)


def test_quote_ident():
    assert PostgresAdapter().quote_ident("select") == '"select"'
    assert PostgresAdapter().quote_ident('a"b') == '"a""b"'
