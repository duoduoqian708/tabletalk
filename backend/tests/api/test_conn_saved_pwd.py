"""WS4 顺带修复：编辑连接测试用已存密码（test-draft + saved_conn_id）。

旧密码永不出网：前端只见 *** 占位；测试时密码留空 + saved_conn_id → 后端用已存密码填充，
改过其它字段也能直接测；填了新密码则用新密码。
"""
from __future__ import annotations

import pytest


async def test_test_draft_uses_saved_password(client, app_state, conn_id, monkeypatch):
    """编辑模式：密码留空 + saved_conn_id → 用已存密码填充后再测。"""
    import app.api.connections as mod

    captured = {}

    async def fake_test_draft(data):
        captured["data"] = data
        return {"ok": True, "latency_ms": 3}

    monkeypatch.setattr(app_state.pools, "test_draft", fake_test_draft)
    # 建一个带密码的连接
    cfg = app_state.connections.create({
        "name": "secret-db", "dialect": "sqlite", "file": "/tmp/x.db", "password": "s3cret-value",
    })
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "secret-db", "dialect": "sqlite", "file": "/tmp/x.db",
        "password": "", "saved_conn_id": cfg.id,
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert captured["data"]["password"] == "s3cret-value", "空密码应被已存密码填充"


async def test_test_draft_new_password_wins(client, app_state, conn_id, monkeypatch):
    """编辑模式：填了新密码 → 用新密码（不覆盖已存值）。"""
    import app.api.connections as mod  # noqa: F401

    captured = {}

    async def fake_test_draft(data):
        captured["data"] = data
        return {"ok": True, "latency_ms": 3}

    monkeypatch.setattr(app_state.pools, "test_draft", fake_test_draft)
    cfg = app_state.connections.create({
        "name": "secret-db", "dialect": "sqlite", "file": "/tmp/x.db", "password": "old-value",
    })
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "secret-db", "dialect": "sqlite", "file": "/tmp/x.db",
        "password": "new-value", "saved_conn_id": cfg.id,
    })
    assert r.status_code == 200
    assert captured["data"]["password"] == "new-value"


async def test_test_draft_unknown_saved_id_no_leak(client, app_state, monkeypatch):
    """未知 saved_conn_id：按无密码测，不报错（不泄露连接存在性）。"""
    captured = {}

    async def fake_test_draft(data):
        captured["data"] = data
        return {"ok": True, "latency_ms": 3}

    monkeypatch.setattr(app_state.pools, "test_draft", fake_test_draft)
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "x", "dialect": "sqlite", "file": "/tmp/x.db",
        "password": "", "saved_conn_id": "s_no_such_conn",
    })
    assert r.status_code == 200
    assert captured["data"]["password"] == ""
    assert "saved_conn_id" not in captured["data"], "saved_conn_id 不应进入测试数据"


async def test_test_draft_without_saved_id_plain(client, app_state, monkeypatch):
    """新建模式（无 saved_conn_id）：行为不变。"""
    captured = {}

    async def fake_test_draft(data):
        captured["data"] = data
        return {"ok": True, "latency_ms": 3}

    monkeypatch.setattr(app_state.pools, "test_draft", fake_test_draft)
    r = await client.post("/api/v1/connections/test-draft", json={
        "name": "x", "dialect": "sqlite", "file": "/tmp/x.db", "password": "abc",
    })
    assert r.status_code == 200
    assert captured["data"]["password"] == "abc"
    assert "saved_conn_id" not in captured["data"]