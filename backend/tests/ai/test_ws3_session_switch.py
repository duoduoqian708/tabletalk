"""WS3 T3.5 验收：切库换 session。
后端双保险——POST /ai/chat 校验 session 归属 connection 与请求一致，不一致返回 409 明确错误；
同连接连续提问放行；ChatStore.get_connection 提供归属查询。
"""
from __future__ import annotations

import json

import pytest


async def test_get_connection_roundtrip(app_state, conn_id):
    """get_connection：已存在会话返回归属连接，未知 id 返回 None。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    assert app_state.chats.get_connection(sid) == conn_id
    assert app_state.chats.get_connection("s_does_not_exist") is None


async def test_cross_connection_session_rejected_409(client, app_state, conn_id, demo_db):
    """跨连接质疑：session 属于 A，但请求 connection_id=B → 409 明确拒绝（不发混合上下文）。"""
    # 构造第二个连接 B（同一文件，仅作身份区分足够）
    conn_b = app_state.connections.create({"name": "b", "dialect": "sqlite", "file": str(demo_db)}).id
    # 建立属于 A（conn_id）的会话
    sid = app_state.chats.upsert(None, conn_id, None)

    r = await client.post("/api/v1/ai/chat", json={
        "connection_id": conn_b,
        "messages": [{"role": "user", "content": "查一下订单"}],
        "session_id": sid,
        "provider": "mock",
    })
    assert r.status_code == 409, f"跨源会话应 409，got {r.status_code}: {r.text[:200]}"
    assert "数据源" in r.json()["detail"] or "会话" in r.json()["detail"]


async def test_same_connection_session_passes(client, app_state, conn_id):
    """同连接同一会话：放行（不 409），正常起流。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    r = await client.post("/api/v1/ai/chat", json={
        "connection_id": conn_id,
        "messages": [{"role": "user", "content": "查一下订单总数"}],
        "session_id": sid,
        "provider": "mock",
    })
    assert r.status_code == 200, f"同源会话应放行，got {r.status_code}: {r.text[:200]}"
    # 首个 SSE 帧应是 data: 事件（turn_start / intent），而非 error
    first = r.text.strip().splitlines()[0]
    assert first.startswith("data:")
    payload = json.loads(first[len("data:"):])
    assert payload["type"] != "error"


async def test_unknown_session_created_under_current_conn(client, app_state, conn_id):
    """未知/过期 session id：视为新会话，落当前连接，不 409。"""
    r = await client.post("/api/v1/ai/chat", json={
        "connection_id": conn_id,
        "messages": [{"role": "user", "content": "查产品"}],
        "session_id": "s_stale_unknown_id",
        "provider": "mock",
    })
    assert r.status_code == 200, f"未知 id 应放行，got {r.status_code}"
    # 新会话应落在当前连接下
    found = app_state.chats.get_connection("s_stale_unknown_id")
    assert found == conn_id