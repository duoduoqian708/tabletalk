from __future__ import annotations


async def test_audit_offset_pagination(client, conn_id):
    for i in range(5):
        await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": f"SELECT {i}"})
    r = await client.get("/api/v1/audit", params={"limit": 2, "offset": 0})
    body = r.json()
    assert body["count"] >= 5                      # count = 全量过滤总数
    assert len(body["entries"]) == 2               # entries = 窗口
    # 最新在前：第一条应是 SELECT 4
    assert "SELECT 4" in body["entries"][0]["sql"]


async def test_audit_offset_second_page(client, conn_id):
    for i in range(5):
        await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": f"SELECT {i}"})
    r = await client.get("/api/v1/audit", params={"limit": 2, "offset": 2})
    body = r.json()
    assert len(body["entries"]) == 2
    assert "SELECT 2" in body["entries"][0]["sql"]


async def test_audit_summary(client, app_state, conn_id):
    name = app_state.connections.get(conn_id).name
    await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT 1"})
    r = await client.get("/api/v1/audit/summary", params={"connection": name})
    body = r.json()
    assert body["total"] >= 1
    assert "by_verdict" in body and "by_origin" in body and "by_tier" in body
    assert "blocked_rate" in body and "ai_ddl_count" in body and "review_count" in body
    assert body["ai_ddl_count"] >= 0


async def test_audit_summary_filter_by_verdict(client, app_state, conn_id):
    name = app_state.connections.get(conn_id).name
    await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT 1"})
    r = await client.get("/api/v1/audit/summary", params={"connection": name, "verdict": "block"})
    body = r.json()
    assert body["by_verdict"].get("block", 0) == 0   # SELECT 是 allow，不应计入 block
