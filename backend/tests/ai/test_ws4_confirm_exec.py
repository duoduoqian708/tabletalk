"""WS4 T4.3 验收：确认执行通路。
POST /query 接 confirm_token（与 confirm=true 同传）：校验 token -> 重新过闸门 -> 执行 ->
审计带 confirm_token/turn_id 与 preview 审计闭环；SQL 被篡改/重放/过期则拒绝。
"""
from __future__ import annotations

import pytest

from app.ai.dto import ChatRequest
from app.ai.gateway import StreamChunk, ToolCall
from app.ai.loop import chat_stream
from app.safety.confirm import create_pending

_SQL = "UPDATE products SET price = 9 WHERE id = 1"


class _ScriptedGateway:
    def __init__(self, turns):
        self.turns = turns
        self.calls = 0

    async def chat_stream(self, messages, tools=None, ctx=None):
        idx = min(self.calls, len(self.turns) - 1)
        self.calls += 1
        for t in self.turns[idx]:
            yield t


async def _run_dml_review(app_state, conn_id, monkeypatch) -> tuple[str, str]:
    """chat 内触发 run_dml REVIEW，返回 (session_id, confirm_token)。"""
    from app.ai import gateway as gw

    turn1 = [StreamChunk(tool_calls=[ToolCall(id="c1", name="run_dml", arguments={"sql": _SQL})])]
    fake = _ScriptedGateway([turn1])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake)

    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid, skill_id="write",
                      messages=[{"role": "user", "content": "把 id=1 的商品价格改成 9"}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    cards = [e["card"] for e in events if e["type"] == "sql_card"]
    assert cards and cards[0].get("confirm_token")
    return sid, cards[0]["confirm_token"]


# ---- 确认执行 + 审计闭环 ----


async def test_confirm_executes_and_audits_correlate(app_state, conn_id, client, monkeypatch):
    sid, token = await _run_dml_review(app_state, conn_id, monkeypatch)
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": _SQL, "origin": "ai",
        "confirm": True, "confirm_token": token, "session_id": sid,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "executed"
    # 审计闭环：preview 条目与执行条目同 token、同 turn_id
    entries = app_state.audit.list()
    previews = [e for e in entries if e.get("confirm_token") == token and e.get("status") == "需确认"]
    execs = [e for e in entries if e.get("confirm_token") == token and e.get("status") == "已确认执行"]
    assert previews, f"缺 preview 审计条目: {entries[-3:]}"
    assert execs, f"缺确认执行审计条目: {entries[-3:]}"
    assert previews[0].get("turn_id") and execs[0].get("turn_id") == previews[0].get("turn_id")


async def test_tampered_sql_rejected(app_state, conn_id, client, monkeypatch):
    sid, token = await _run_dml_review(app_state, conn_id, monkeypatch)
    tampered = "UPDATE products SET price = 999 WHERE id = 1"
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": tampered, "origin": "ai",
        "confirm": True, "confirm_token": token, "session_id": sid,
    })
    assert r.status_code == 409
    # 无"已确认执行"条目产生
    execs = [e for e in app_state.audit.list() if e.get("status") == "已确认执行"]
    assert not execs


async def test_replayed_token_rejected(app_state, conn_id, client, monkeypatch):
    sid, token = await _run_dml_review(app_state, conn_id, monkeypatch)
    payload = {
        "connection_id": conn_id, "sql": _SQL, "origin": "ai",
        "confirm": True, "confirm_token": token, "session_id": sid,
    }
    r1 = await client.post("/api/v1/query", json=payload)
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/query", json=payload)
    assert r2.status_code == 409, "一次性 token 重放必须拒绝"


async def test_expired_token_rejected(app_state, conn_id, client):
    sid = app_state.chats.upsert(None, conn_id, None)
    create_pending(app_state.chats, sid, _SQL, None, None, ttl=-5)
    pending = app_state.chats.get_pending_dml(sid)
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": _SQL, "origin": "ai",
        "confirm": True, "confirm_token": pending["token"], "session_id": sid,
    })
    assert r.status_code == 409


async def test_legacy_confirm_without_token_still_works(app_state, conn_id, client):
    """兼容通道：编辑器/manual 的 confirm=true（无 token）行为不变。"""
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": "UPDATE products SET price = 5 WHERE id = 2",
        "origin": "manual", "confirm": True,
    })
    assert r.status_code == 200
    assert r.json()["verdict"] == "executed"
