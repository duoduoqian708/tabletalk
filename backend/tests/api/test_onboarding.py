"""接入流程状态机测试：kb_status 卡死、任务化构建、确认闸、取消、持久化。"""
from __future__ import annotations

import asyncio

from tests.api.test_api import _build_and_wait


async def test_kb_not_built_blocks_query(client, app_state, demo_db):
    """未构建（kb_status=none）→ 手动查询 409 kb_not_built（卡死边界）。"""
    c = app_state.connections.create({"name": "nb", "dialect": "sqlite", "file": str(demo_db)})
    r = await client.post("/api/v1/query", json={"connection_id": c.id, "sql": "SELECT 1"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "kb_not_built"


async def test_kb_not_built_blocks_ai_chat(client, app_state, demo_db):
    """未构建 → AI chat SSE error 事件（保持流式协议）。"""
    c = app_state.connections.create({"name": "nb2", "dialect": "sqlite", "file": str(demo_db)})
    async with client.stream("POST", "/api/v1/ai/chat", json={
        "connection_id": c.id, "messages": [{"role": "user", "content": "查订单"}],
    }) as r:
        body = await r.aread()
    assert "kb_not_built" in body.decode()
    assert r.status_code == 200


async def test_build_job_lifecycle(client, conn_id):
    """任务化构建全流程：build→progress done→pending_review→confirm-all→ready→查询放行。"""
    await _build_and_wait(client, conn_id)
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["kb_status"] == "pending_review"
    assert st["pending"]["draft_docs"] >= 0
    # 确认闸
    r = await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")
    assert r.json()["kb_status"] == "ready"
    assert r.json()["docs"] >= 0
    # 放行
    q = await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT 1"})
    assert q.json()["verdict"] == "allow"


async def test_build_cancel_api(client, conn_id):
    """取消 API：无任务时返回 False；任务已结束时 cancel 幂等不炸。"""
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build/cancel")
    assert r.json()["cancelled"] is False
    # 启动任务后取消（构建可能已极快完成；断言接口形状与状态不炸）
    await client.post(f"/api/v1/knowledge/{conn_id}/build")
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build/cancel")
    assert "cancelled" in r.json()
    # 无论取消是否命中，随后都能重新构建（不残留 building 死锁）
    await _build_and_wait(client, conn_id)
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["kb_status"] in ("pending_review", "none")
    assert st["building"] is False


async def test_confirm_all_confirms_drafts_and_tags(client, conn_id):
    """确认闸：草案文档与 draft 标签批量确认。"""
    await _build_and_wait(client, conn_id)
    # 生产 AI 草案 + 标签
    r = await client.post(f"/api/v1/knowledge/{conn_id}/annotate", json={"include_samples": False})
    assert r.json()["items"] > 0  # items>0 确认 mock 正常；added 可能=0（build 已创建全部 draft）
    r = await client.post(f"/api/v1/knowledge/{conn_id}/annotate-tags")
    assert r.json()["tables"] > 0
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["pending"]["draft_docs"] > 0
    assert st["pending"]["draft_tags"] > 0
    # 确认闸
    r = await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")
    assert r.json()["docs"] > 0
    assert r.json()["tags"] > 0
    # 全部 confirmed（确认语义：AI 草案确认后视为用户认可，source→user）
    tags = (await client.get(f"/api/v1/knowledge/{conn_id}/tags")).json()
    assert all(t["status"] == "confirmed" for t in tags["library"])
    docs = (await client.get(f"/api/v1/knowledge/{conn_id}/docs")).json()
    userish = [d for d in docs["docs"] if d["source"] == "user"]
    assert userish and all(d["status"] == "confirmed" for d in userish)


async def test_test_draft_sqlite_ok(client, demo_db):
    """SQLite：文件可读 + 能列表 → 通过（不落盘）。"""
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "draft", "dialect": "sqlite", "file": str(demo_db),
    })
    body = r.json()
    assert body["ok"] is True
    assert body["error"] is None
    # 不落盘：连接列表没有新增
    lst = (await client.get("/api/v1/connections")).json()
    assert not any(c["name"] == "draft" for c in lst)


async def test_test_draft_sqlite_bad_file(client):
    """SQLite：文件不存在 → 失败。"""
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "bad", "dialect": "sqlite", "file": "/nonexistent/x.db",
    })
    assert r.json()["ok"] is False
    assert r.json()["error"]


async def test_test_draft_sqlite_empty_path(client):
    """SQLite：文件路径为空 → 明确失败提示。"""
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "empty", "dialect": "sqlite", "file": "",
    })
    body = r.json()
    assert body["ok"] is False
    assert "file" in (body["error"] or "").lower() or "路径" in (body["error"] or "")


async def test_test_draft_bad_host(client):
    """PG/MySQL：真实连库失败（不可达地址）。"""
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "pg", "dialect": "postgres", "host": "127.0.0.1", "port": 1,
        "user": "x", "password": "y", "database": "z", "timeout": 2,
    })
    body = r.json()
    assert body["ok"] is False
    assert body["error"]


async def test_kb_status_migration_artifact_exists_becomes_ready(client, app_state, conn_id, tmp_path):
    """迁移：已有知识库 artifact 的老连接（kb_status 缺失=none）→ 自动置 ready，不卡死。"""
    # 构建出 artifact，然后手动把状态改回 none（模拟升级前的旧连接）
    await _build_and_wait(client, conn_id)
    await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")
    app_state.connections.set_kb_status(conn_id, "none")
    # 重启（同数据目录）→ 迁移应置 ready
    from app.state import reset_state
    st2 = reset_state(tmp_path / "data")
    assert st2.connections.get(conn_id).kb_status == "ready"
    # 查询放行
    q = await client.post("/api/v1/query", json={"connection_id": conn_id, "sql": "SELECT 1"})
    assert q.json()["verdict"] == "allow"


async def test_kb_status_persists_and_overview_not_built(client, app_state, demo_db, tmp_path):
    """kb_status 持久化到连接配置；未构建连接 overview 返回 built=False（不隐式构建）。"""
    c = app_state.connections.create({"name": "p", "dialect": "sqlite", "file": str(demo_db)})
    app_state.connections.set_kb_status(c.id, "pending_review")
    # 状态落盘：新建实例（同数据目录）仍可读
    from app.state import reset_state
    st2 = reset_state(tmp_path / "data")
    cfg = st2.connections.get(c.id)
    assert cfg.kb_status == "pending_review"
    # overview 未构建 → built=False + kb_status
    ov = (await client.get(f"/api/v1/knowledge/{c.id}/overview")).json()
    assert ov["built"] is False
    assert ov["kb_status"] == "pending_review"
    assert ov["tables"] == []


async def test_sync_endpoint_lifecycle(client, conn_id, demo_db):
    """手动同步端点：构建+确认后 → 无变化 changed=False → 结构变化（建新表）→ 增量同步。"""
    import sqlite3

    from tests.api.test_api import _build_and_wait
    await _build_and_wait(client, conn_id)
    await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")

    # 无变化
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    body = r.json()
    assert body["changed"] is False
    assert body["message"] == "结构无变化"

    # 结构变化：新建一张表
    conn = sqlite3.connect(str(demo_db))
    conn.execute("CREATE TABLE sync_test (id INTEGER PRIMARY KEY, note TEXT)")
    conn.commit()
    conn.close()
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    body = r.json()
    assert body["changed"] is True
    assert body["tables_added"] == 1
    # 知识库已含新表文档（确认闸不重置：仍 ready）
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["kb_status"] == "ready"
    assert st["synced_at"]
    docs = (await client.get(f"/api/v1/knowledge/{conn_id}/docs", params={"table": "sync_test"})).json()
    assert len(docs["docs"]) >= 1
    # 幂等：再同步无变化
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    assert r.json()["changed"] is False


async def test_sync_requires_ready(client, app_state, demo_db):
    """未构建连接 sync → 409。"""
    c = app_state.connections.create({"name": "nrs", "dialect": "sqlite", "file": str(demo_db)})
    r = await client.post(f"/api/v1/knowledge/{c.id}/sync")
    assert r.status_code == 409
