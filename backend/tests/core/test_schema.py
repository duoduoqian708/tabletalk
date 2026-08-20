"""schema 发现测试（真实 SQLite demo 库）。"""
from __future__ import annotations

from app.core.schema import describe_table, export_ddl, get_schema, preview_table, summarize


async def test_get_schema(app_state, conn_id):
    schema = await get_schema(app_state, conn_id, refresh=True)
    names = {t["name"] for t in schema["tables"]}
    assert "orders" in names
    assert "products" in names
    assert "customers" in names
    assert schema["dialect"] == "sqlite"


async def test_schema_has_row_count(app_state, conn_id):
    schema = await get_schema(app_state, conn_id, refresh=True)
    by_name = {t["name"]: t for t in schema["tables"]}
    assert "row_count" in by_name["orders"]
    assert isinstance(by_name["orders"]["row_count"], int)
    assert by_name["orders"]["row_count"] >= 1



async def test_columns_with_pk_fk(app_state, conn_id):
    schema = await get_schema(app_state, conn_id, refresh=True)
    cols = {(c["table"], c["name"]): c for c in schema["columns"]}
    assert cols[("orders", "id")]["pk"] is True
    assert cols[("order_items", "order_id")]["fk"] is True
    fks = {(f["table"], f["column"]) for f in schema["foreign_keys"]}
    assert ("order_items", "order_id") in fks
    assert ("orders", "customer_id") in fks


async def test_describe_table(app_state, conn_id):
    info = await describe_table(app_state, conn_id, "orders")
    assert info["name"] == "orders"
    col_names = {c["name"] for c in info["columns"]}
    assert "id" in col_names and "created_at" in col_names


async def test_preview_table(app_state, conn_id):
    res = await preview_table(app_state, conn_id, "orders", limit=5)
    assert res["total"] >= 1
    assert len(res["rows"]) <= 5
    assert "id" in res["columns"]


async def test_export_ddl(app_state, conn_id):
    res = await export_ddl(app_state, conn_id, "orders")
    assert "CREATE TABLE" in res["ddl"].upper()
    assert "PRIMARY KEY" in res["ddl"].upper()
    assert "FOREIGN KEY" in res["ddl"].upper()


async def test_summarize_structure_only(app_state, conn_id):
    schema = await get_schema(app_state, conn_id, refresh=True)
    text = summarize(schema, "orders")
    assert "orders" in text
    assert "created_at" in text
    assert "orders" in text and "id" in text
