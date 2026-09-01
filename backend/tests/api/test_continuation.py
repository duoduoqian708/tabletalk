"""§19.5 continuation gate 路由测试：四 type 分发到既有处理器。"""
from __future__ import annotations

import json


async def test_continuation_unknown_type_rejected(client, conn_id):
    r = await client.post("/api/v1/ai/continuation", json={"type": "hax", "connection_id": conn_id})
    assert r.status_code == 422


async def test_continuation_new_question_sse(client, conn_id):
    """new_question → 走完整 /ai/chat SSE 链（mock 降级单任务 query）。"""
    r = await client.post("/api/v1/ai/continuation", json={
        "type": "new_question", "connection_id": conn_id,
        "payload": {"question": "查一下退货率"},
    })
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = []
    async for line in r.aiter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    assert events[-1]["type"] == "done"


async def test_continuation_sql_option_routes(client, conn_id):
    """sql_option → 复用 /ai/sql-option（合法 SQL+选项 → 200，走了轻量改写链路而非类型错）。"""
    r = await client.post("/api/v1/ai/continuation", json={
        "type": "sql_option", "connection_id": conn_id,
        "payload": {
            "sql": "SELECT COUNT(*) FROM orders",
            "option": {"label": "只看近 30 天", "hint": "created_at >= date('now','-30 day')"},
        },
    })
    assert r.status_code == 200
    body = r.json()
    assert "sql" in body or "error" in body


async def test_continuation_confirm_write_routes(client, conn_id):
    """confirm_write → 复用 /api/v1/query + confirm_token（坏 token 走 query 校验 4xx，不是 422 类型错）。"""
    r = await client.post("/api/v1/ai/continuation", json={
        "type": "confirm_write", "connection_id": conn_id,
        "payload": {"sql": "UPDATE orders SET status='paid' WHERE id=1", "confirm_token": "tok-nonexist"},
    })
    assert r.status_code != 422  # 已到 query 层（token 校验失败 4xx）
    assert r.status_code in (400, 401, 403, 406, 409, 410)
