"""R6/S1 集成：POST /query 会话变量填充；表级过滤器已从执行层移除（纯知识形态）。"""
from __future__ import annotations

import sqlite3

from app.knowledge.filters import FilterStore


async def test_filters_api_confirm_keeps_knowledge_form(client, app_state, tmp_path):
    """S1：filters API 三端点可用（知识形态）；confirm 后执行层不再注入（引擎不深入改写 SQL）。"""
    p = tmp_path / "t8api2.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, is_deleted INTEGER DEFAULT 0);"
        "INSERT INTO orders VALUES (1, 0), (2, 0);"
    )
    con.commit()
    con.close()
    c = app_state.connections.create(
        {"name": "t8api2", "dialect": "sqlite", "file": str(p)}
    )
    app_state.connections.set_kb_status(c.id, "ready")
    kb = app_state.knowledge
    try:
        # 结构检测 → draft 候选落库（模拟 build 接入）
        schema = {
            "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 1}],
            "columns": [{"table": "orders", "name": "is_deleted", "type": "int", "pk": False, "fk": False}],
            "foreign_keys": [],
        }
        kb.build_service.ingest_filter_candidates(kb, c.id, schema)
        # GET 列表可见（draft）
        r = await client.get(f"/api/v1/knowledge/{c.id}/filters")
        flt = r.json()["filters"]
        assert len(flt) == 1 and flt[0]["table"] == "orders" and flt[0]["status"] == "draft"
        # confirm → 知识形态（context 提示 LLM 参考，执行层不注入）
        r2 = await client.post(f"/api/v1/knowledge/{c.id}/filters/confirm",
                               json={"table": "orders"})
        assert r2.json()["confirmed"] is True
        r3 = await client.post("/api/v1/query", json={
            "connection_id": c.id, "sql": "SELECT * FROM orders",
        })
        assert r3.json()["verdict"] == "allow", r3.json()
        name = app_state.connections.get(c.id).name
        ar = await client.get("/api/v1/audit", params={"connection": name, "limit": 5})
        sqls = [e["sql"] for e in ar.json()["entries"]]
        assert not any("is_deleted = 0" in s for s in sqls), sqls  # S1：执行层不再改写
        # reject → 移除且不再注入
        r4 = await client.post(f"/api/v1/knowledge/{c.id}/filters/reject",
                               json={"table": "orders"})
        assert r4.json()["rejected"] is True
        r5 = await client.get(f"/api/v1/knowledge/{c.id}/filters")
        assert r5.json()["filters"] == []
    finally:
        kb.filter_store._filters.pop(c.id, None)
        app_state.connections.delete(c.id)


async def test_query_with_session_vars_and_filters(client, app_state, tmp_path):
    p = tmp_path / "t8api.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, is_deleted INTEGER DEFAULT 0, tenant_id INTEGER);"
        "INSERT INTO orders VALUES (1, 0, 7), (2, 0, 9);"
    )
    con.commit()
    con.close()
    c = app_state.connections.create(
        {"name": "t8api", "dialect": "sqlite", "file": str(p),
         "session_vars": {"current_tenant": 7}}
    )
    app_state.connections.set_kb_status(c.id, "ready")
    try:
        fs: FilterStore = app_state.knowledge.filter_store
        fs.add(c.id, "orders", "is_deleted = 0")
        fs.confirm(c.id, "orders")
        # LLM 风格：SQL 直接引用会话变量（此前会 binding 报错）
        r = await client.post("/api/v1/query", json={
            "connection_id": c.id,
            "sql": "SELECT * FROM orders WHERE tenant_id = :current_tenant",
        })
        body = r.json()
        assert body["verdict"] == "allow", body
        # 审计：真实执行 SQL（变量已填充；过滤器不注入——S1 执行层纯横切面）
        name = app_state.connections.get(c.id).name
        ar = await client.get("/api/v1/audit", params={"connection": name, "limit": 5})
        sqls = [e["sql"] for e in ar.json()["entries"]]
        assert any("tenant_id = '7'" in s for s in sqls), sqls
        assert not any("is_deleted = 0" in s for s in sqls), sqls
    finally:
        app_state.knowledge.filter_store._filters.pop(c.id, None)
        app_state.connections.delete(c.id)
