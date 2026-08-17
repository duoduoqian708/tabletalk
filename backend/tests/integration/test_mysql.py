"""MySQL 适配器集成测试（docker-gated，见同目录 conftest）。"""
from __future__ import annotations

import pytest

from app.core.dialects.base import DialectConfig
from app.core.dialects.mysql import MySQLAdapter


@pytest.fixture
async def conn(mysql_cfg):
    adapter = MySQLAdapter()
    c = await adapter.connect(DialectConfig(**mysql_cfg))
    yield adapter, c
    await adapter.close(c)


async def test_connect_health_and_roundtrip(conn):
    adapter, c = conn
    assert await adapter.is_healthy(c)
    await adapter.execute(c, "DROP TABLE IF EXISTS it_orders")
    await adapter.execute(c, "CREATE TABLE it_orders (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, amount DECIMAL(10,2))")
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
    await adapter.execute(c, "DROP TABLE IF EXISTS it_orders")
    await adapter.execute(c, "DROP TABLE IF EXISTS it_customers")
    await adapter.execute(c, "CREATE TABLE it_customers (id INT PRIMARY KEY, name VARCHAR(100))")
    await adapter.execute(c, "CREATE TABLE it_orders (id INT PRIMARY KEY, customer_id INT, note TEXT, FOREIGN KEY (customer_id) REFERENCES it_customers(id))")
    tables = {t.name for t in await adapter.list_tables(c)}
    assert {"it_orders", "it_customers"} <= tables
    cols = {col.name for col in await adapter.list_columns(c, "it_orders")}
    assert cols == {"id", "customer_id", "note"}
    fks = {f.table: f for f in await adapter.list_foreign_keys(c)}
    assert fks["it_orders"].column == "customer_id"
    assert fks["it_orders"].ref_table == "it_customers"
    await adapter.execute(c, "DROP TABLE it_orders")
    await adapter.execute(c, "DROP TABLE it_customers")


async def test_read_only_blocks_writes(mysql_cfg):
    adapter = MySQLAdapter()
    # 先用普通连接建表，再用只读连接尝试写入（失败原因必须是"只读"，而非"表不存在"）
    setup = await adapter.connect(DialectConfig(**mysql_cfg))
    try:
        await adapter.execute(setup, "DROP TABLE IF EXISTS it_ro")
        await adapter.execute(setup, "CREATE TABLE it_ro (id INT PRIMARY KEY, name VARCHAR(100))")
    finally:
        await adapter.close(setup)

    c = await adapter.connect(DialectConfig(**mysql_cfg, read_only=True))
    try:
        with pytest.raises(Exception):
            await adapter.execute(c, "INSERT INTO it_ro (id, name) VALUES (1, 'ro')")
    finally:
        await adapter.close(c)

    cleanup = await adapter.connect(DialectConfig(**mysql_cfg))
    try:
        await adapter.execute(cleanup, "DROP TABLE it_ro")
    finally:
        await adapter.close(cleanup)


def test_quote_ident():
    assert MySQLAdapter().quote_ident("select") == "`select`"
    assert MySQLAdapter().quote_ident("a`b") == "`a``b`"
