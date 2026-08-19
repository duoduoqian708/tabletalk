"""端到端 API 测试（httpx AsyncClient + 真实 SQLite demo 库）。"""
from __future__ import annotations

import json


async def test_health(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["gate"] == "armed"
    assert "sqlite" in body["dialects"]


async def test_sidecar_token_guard():
    """无 token / 错 token → 401；health 与 OPTIONS 免鉴权。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/connections")
        assert r.status_code == 401
        r = await c.post("/api/v1/query", json={"connection_id": "x", "sql": "SELECT 1"})
        assert r.status_code == 401
        r = await c.get("/api/v1/health")
        assert r.status_code == 200
        r = await c.options("/api/v1/query")
        assert r.status_code != 401
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-TableTalk-Token": "wrong-token"},
    ) as c:
        r = await c.get("/api/v1/connections")
        assert r.status_code == 401


async def test_connection_crud_and_schema(client):
    r = await client.post("/api/v1/connections", json={"name": "demo", "dialect": "sqlite", "file": "/tmp/x.db"})
    assert r.status_code == 201
    cid = r.json()["id"]
    # 凭据不回显明文
    assert r.json().get("password") in ("", "•••")

    lst = await client.get("/api/v1/connections")
    assert len(lst.json()) >= 1

    # 测试连接（指向不存在的文件也会通过，SQLite 会创建；这里用于验证接口可用）
    t = await client.post(f"/api/v1/connections/{cid}/test")
    assert t.status_code == 200

    d = await client.delete(f"/api/v1/connections/{cid}")
    assert d.status_code == 200


async def test_query_gate_flow(client, conn_id):
    # 只读 → 放行
    r = await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT * FROM orders LIMIT 3"})
    body = r.json()
    assert body["verdict"] == "allow"
    assert body["tier"] == "read"
    assert len(body["rows"]) == 3

    # UPDATE 无 WHERE → 拦截
    r = await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "UPDATE orders SET status='paid'"})
    body = r.json()
    assert body["verdict"] == "block"
    assert body["suggestions"]

    # UPDATE 有 WHERE 未确认 → review + 预览
    r = await client.post("/api/v1/query",
                          json={"connection_id": conn_id, "sql": "UPDATE orders SET status='paid' WHERE id=1"})
    body = r.json()
    assert body["verdict"] == "review"
    assert body["needs_confirm"] is True
    assert body["preview_rows"] == 1

    # 确认后执行
    r = await client.post("/api/v1/query",
                          json={"connection_id": conn_id, "sql": "UPDATE orders SET status='paid' WHERE id=1",
                                "confirm": True})
    body = r.json()
    assert body["verdict"] == "executed"
    assert body["affected_rows"] == 1

    # INSERT 需确认
    r = await client.post("/api/v1/query",
                          json={"connection_id": conn_id, "sql": "INSERT INTO orders (id) VALUES (999)"})
    assert r.json()["verdict"] == "review"


async def test_query_ddl_manual_only(client, conn_id):
    # DDL 手动 → review（红卡确认），确认后执行
    r = await client.post("/api/v1/query", json={"connection_id": conn_id,
                                                 "sql": "DROP TABLE orders"})
    assert r.json()["verdict"] == "review"
    assert r.json()["tier"] == "ddl"
    # DDL 来自 AI → 拦截（防御纵深）
    r = await client.post("/api/v1/query", json={"connection_id": conn_id, "origin": "ai",
                                                 "sql": "DROP TABLE orders"})
    assert r.json()["verdict"] == "block"


async def test_ai_chat_sse(client, conn_id):
    payload = {"connection_id": conn_id, "messages": [{"role": "user", "content": "查退货率"}], "provider": "mock"}
    r = await client.post("/api/v1/ai/chat", json=payload)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = []
    async for line in r.aiter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "sql_card" for e in events)


async def test_ai_selection(client, conn_id):
    r = await client.post("/api/v1/ai/selection",
                          json={"connection_id": conn_id, "sql": "SELECT * FROM orders", "kind": "risk"})
    assert r.status_code == 200
    assert r.json()["kind"] == "risk"


async def test_ai_test_gateway(client):
    # mock 网关连通性测试（显式指定 provider）
    r = await client.post("/api/v1/ai/test", params={"provider": "mock"})
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "mock"
    assert body["latency_ms"] >= 0


async def test_chat_sessions_recorded_on_question(client, conn_id):
    """只有 AI 提问才进会话表；GET 只读不刷新 updated_at。"""
    import time as _time

    payload = {"connection_id": conn_id, "session_id": "test-sess-1",
               "messages": [{"role": "user", "content": "查退货率"}], "provider": "mock"}
    r = await client.post("/api/v1/ai/chat", json=payload)
    assert r.status_code == 200
    async for line in r.aiter_lines():
        if line == "data: [DONE]":
            break
    # 会话已建 + 消息落库（user + assistant text + sql_card）
    detail = await client.get("/api/v1/chat/sessions/test-sess-1")
    body = detail.json()
    assert body["id"] == "test-sess-1"
    assert body["connection_id"] == conn_id
    kinds = {m["kind"] for m in body["messages"]}
    assert "text" in kinds and "sql_card" in kinds
    assert any(m["role"] == "user" for m in body["messages"])

    # GET 不刷新 updated_at
    list1 = (await client.get("/api/v1/chat/sessions", params={"connection": conn_id})).json()
    up1 = next(s["updated_at"] for s in list1["sessions"] if s["id"] == "test-sess-1")
    _time.sleep(0.01)
    list2 = (await client.get("/api/v1/chat/sessions", params={"connection": conn_id})).json()
    up2 = next(s["updated_at"] for s in list2["sessions"] if s["id"] == "test-sess-1")
    assert up1 == up2

    # 再次提问（同 session）刷新 updated_at
    _time.sleep(0.01)
    await client.post("/api/v1/ai/chat", json={**payload, "messages": [{"role": "user", "content": "库存"}]})
    list3 = (await client.get("/api/v1/chat/sessions", params={"connection": conn_id})).json()
    up3 = next(s["updated_at"] for s in list3["sessions"] if s["id"] == "test-sess-1")
    assert up3 > up2

    # 标题回传不刷新 updated_at
    _time.sleep(0.01)
    r = await client.put("/api/v1/chat/sessions/test-sess-1/title", json={"title": "退货率分析"})
    assert r.json()["ok"] is True
    list4 = (await client.get("/api/v1/chat/sessions", params={"connection": conn_id})).json()
    up4 = next(s["updated_at"] for s in list4["sessions"] if s["id"] == "test-sess-1")
    assert up4 == up3


async def test_query_run_does_not_touch_sessions(client, conn_id):
    """点'运行'（POST /query）不进会话表，只进审计。"""
    before = (await client.get("/api/v1/chat/sessions", params={"connection": conn_id})).json()
    n_before = before["count"]
    await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT 1"})
    after = (await client.get("/api/v1/chat/sessions", params={"connection": conn_id})).json()
    assert after["count"] == n_before


async def test_ai_test_gateway_bad_url(client):
    # 无效 base_url（显式 local 走真实网络验证）→ ok=False + error
    r = await client.post("/api/v1/ai/test", params={"provider": "local",
                                                     "base_url": "http://127.0.0.1:1/v1",
                                                     "api_key": "x", "model": "y"})
    body = r.json()
    assert body["ok"] is False
    assert body["error"]


async def test_query_readonly_conn_blocks_writes(client, app_state, demo_db):
    """只读连接：写/DDL 一律 BLOCK，读正常。"""
    ro = app_state.connections.create(
        {"name": "ro-demo", "dialect": "sqlite", "file": str(demo_db), "read_only": True}
    )
    app_state.connections.set_kb_status(ro.id, "ready")  # 放行状态机（本测试只管只读语义）
    # 读 → 放行
    r = await client.post("/api/v1/query", json={"connection_id": ro.id, "sql": "SELECT * FROM orders LIMIT 3"})
    assert r.json()["verdict"] == "allow"
    # 写（有 WHERE 本应 REVIEW）→ 只读连接强制 BLOCK
    r = await client.post("/api/v1/query", json={"connection_id": ro.id,
                                                 "sql": "UPDATE orders SET status='RO-FORBIDDEN' WHERE id=1"})
    body = r.json()
    assert body["verdict"] == "block"
    assert "只读" in body["reason"]
    # DDL → BLOCK
    r = await client.post("/api/v1/query", json={"connection_id": ro.id, "sql": "DROP TABLE orders"})
    assert r.json()["verdict"] == "block"
    # 数据未被改动（标记值不应出现）
    r = await client.post("/api/v1/query", json={"connection_id": ro.id,
                                                 "sql": "SELECT COUNT(*) FROM orders WHERE status='RO-FORBIDDEN'"})
    assert r.json()["rows"][0][0] == 0


async def test_audit_logged(client, conn_id):
    await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT 1"})
    await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "UPDATE orders SET x=1"})
    r = await client.get("/api/v1/audit")
    body = r.json()
    assert body["count"] >= 2
    verdicts = {e["verdict"] for e in body["entries"]}
    assert "block" in verdicts


async def test_settings_update(client, app_state):
    # 默认不再内置模型 → 先塞一条，再测兼容字段代理与旧格式 PUT
    app_state.runtime.update({"ai_models": [{
        "id": "llm_t", "name": "测试模型", "provider": "cloud",
        "base_url": "https://x/v1", "model": "gpt-test", "api_key": "k",
    }], "default_ai_model": "llm_t"})
    r = await client.get("/api/v1/settings")
    body = r.json()
    # 兼容字段 ai_provider 代理到当前默认模型
    assert body["ai_provider"] == next(m["provider"] for m in body["ai_models"] if m["id"] == body["default_ai_model"])
    r = await client.put("/api/v1/settings", json={"ai_provider": "local", "ai_base_url": "http://localhost:11434/v1"})
    body = r.json()
    assert body["ai_provider"] == "local"
    assert "•••" in body["ai_api_key"] or body["ai_api_key"] == ""


async def _build_and_wait(client, conn_id):
    """POST build → 轮询 progress 直到 done（任务化构建）。"""
    import asyncio

    r = await client.post(f"/api/v1/knowledge/{conn_id}/build")
    assert r.status_code == 200
    assert r.json()["kb_status"] == "building"
    for _ in range(200):
        p = (await client.get(f"/api/v1/knowledge/{conn_id}/build/progress")).json()
        if p.get("done"):
            assert not p.get("error"), p
            return p
        await asyncio.sleep(0.02)
    raise AssertionError("build 超时")


async def test_knowledge_build_retrieve_annotate(client, conn_id):
    await _build_and_wait(client, conn_id)
    # 构建完成 → 待确认；确认闸 → ready
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["kb_status"] == "pending_review"
    r = await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")
    assert r.json()["kb_status"] == "ready"
    r = await client.get(f"/api/v1/knowledge/{conn_id}/retrieve", params={"q": "orders"})
    assert r.json()["count"] >= 1
    r = await client.put(f"/api/v1/knowledge/{conn_id}/docs",
                         json={"table": "orders", "column": "status", "note": "状态枚举：pending/paid/shipped/cancelled"})
    assert r.status_code == 201
    r = await client.get(f"/api/v1/knowledge/{conn_id}/docs", params={"table": "orders"})
    assert any(d["source"] == "user" for d in r.json()["docs"])


async def test_knowledge_graph_and_overview(client, conn_id):
    await _build_and_wait(client, conn_id)
    # 图谱：FK 边存在
    g = await client.get(f"/api/v1/knowledge/{conn_id}/graph")
    assert g.json()["built"] is True
    assert any(e["kind"] == "fk" for e in g.json()["edges"])
    # 审查视图：表/列 + 确认状态字段
    ov = await client.get(f"/api/v1/knowledge/{conn_id}/overview")
    o = ov.json()
    assert o["built"] is True
    assert len(o["tables"]) > 0
    assert "comment_status" in o["tables"][0]
    assert "tags" in o["tables"][0]
    assert "type" in o["columns"][0]


async def test_knowledge_tags_flow(client, conn_id):
    await _build_and_wait(client, conn_id)
    # AI 生成领域标签（mock 网关：按表名关键词）
    r = await client.post(f"/api/v1/knowledge/{conn_id}/annotate-tags")
    assert r.json()["tables"] > 0
    # 标签库：全 draft，订单相关表共享"订单"标签
    r = await client.get(f"/api/v1/knowledge/{conn_id}/tags")
    body = r.json()
    assert len(body["library"]) > 0
    assert all(t["status"] == "draft" for t in body["library"])
    order_tables = [t for t, tags in body["tables"].items() if "订单" in tags]
    assert any("order" in t.lower() for t in order_tables)
    # 确认一个标签 → 可路由
    name = body["library"][0]["name"]
    c = await client.post(f"/api/v1/knowledge/{conn_id}/tags/confirm", json={"name": name})
    assert c.json()["confirmed"] is True
    # 路由候选表
    rt = await client.post(f"/api/v1/knowledge/{conn_id}/route", json={"table": "x", "tags": [name]})
    assert len(rt.json()["tables"]) >= 1
    # 拒绝一个标签
    r = await client.get(f"/api/v1/knowledge/{conn_id}/tags")
    other = next(t["name"] for t in r.json()["library"] if t["name"] != name)
    d = await client.post(f"/api/v1/knowledge/{conn_id}/tags/reject", json={"name": other})
    assert d.json()["rejected"] is True
    r = await client.post(f"/api/v1/knowledge/{conn_id}/annotate", json={"include_samples": False})
    assert r.json()["added"] > 0
    # 草案状态 = draft
    docs = await client.get(f"/api/v1/knowledge/{conn_id}/docs")
    drafts = [d for d in docs.json()["docs"] if d["source"] == "ai_draft"]
    assert drafts and all(d["status"] == "draft" for d in drafts)
    # 确认全部 → 变 confirmed
    c = await client.post(f"/api/v1/knowledge/{conn_id}/confirm", json={})
    assert c.json()["confirmed"] >= 1
    docs2 = await client.get(f"/api/v1/knowledge/{conn_id}/docs")
    confirmed = [d for d in docs2.json()["docs"] if d["source"] == "ai_draft"]
    assert all(d["status"] == "confirmed" for d in confirmed)


async def test_sql_format(client):
    r = await client.post("/api/v1/sql/format", json={"sql": "select * from orders where id=1", "dialect": "sqlite"})
    assert r.status_code == 200
    assert "SELECT" in r.json()["formatted"].upper()


async def test_connection_sensitive_roundtrip(client):
    """敏感名单：创建时写入 → 列表可见 → 更新可改（数据边界由用户画）。"""
    r = await client.post("/api/v1/connections", json={
        "name": "sensitive-db", "dialect": "sqlite", "file": "/tmp/sens.db",
        "sensitive": ["payroll_*", "*.email"],
    })
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["sensitive"] == ["payroll_*", "*.email"]
    # 更新
    r = await client.put(f"/api/v1/connections/{cid}", json={"sensitive": ["hr_*"]})
    assert r.status_code == 200
    assert r.json()["sensitive"] == ["hr_*"]
    await client.delete(f"/api/v1/connections/{cid}")
