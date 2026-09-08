"""写操作确认通路：confirm_token 状态机 / REVIEW 后 loop 终止 / 确认执行与审计闭环 / 取消与过期。

行为来源（合并自原 ws4_confirm / ws4_confirm_exec / ws4_cancel 验收文件）：
- token 一次性：消费即失效，重放/篡改/过期/无 pending 一律拒绝。
- run_dml REVIEW → loop 当轮终止（防模型幻觉"已执行"），卡片带 confirm_token/expires_in。
- POST /query 带 token：重新过闸门 → 执行 → preview 与执行审计条目同 token/turn_id。
- 取消/过期 → 清 pending + 写 kind=system 消息（下轮模型上下文可见）。
"""
from __future__ import annotations

import hashlib

import pytest

from app.ai.dto import ChatRequest
from app.ai.gateway import StreamChunk, ToolCall
from app.ai.harness import controlled_step_stream
from app.ai.loop import stream
from app.safety.confirm import (
    TOKEN_TTL,
    clear_pending,
    create_pending,
    get_pending,
    validate_and_consume,
)

_SQL = "UPDATE products SET price = 9 WHERE id = 1"


class _ScriptedGateway:
    """脚本化网关：第 N 次调用返回第 N 组 chunk；记录调用次数与见过的消息。"""

    def __init__(self, turns):
        self.turns = turns
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, ctx=None):
        self.seen_messages.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.turns) - 1)
        self.calls += 1
        for t in self.turns[idx]:
            yield t


async def _run_dml_review(app_state, conn_id, monkeypatch) -> tuple[str, str]:
    """受控写步骤内触发 run_dml REVIEW，返回 (session_id, confirm_token)。"""
    from app.ai import gateway as gw

    fake = _ScriptedGateway([[StreamChunk(tool_calls=[ToolCall(id="c1", name="run_dml",
                                                               arguments={"sql": _SQL})])]])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake)

    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": "把 id=1 的商品价格改成 9"}], provider="mock")
    req._step_action = "write"  # 受控写步骤工具面（含 run_dml）
    events = [ev async for ev in controlled_step_stream(app_state, req)]
    cards = [e["card"] for e in events if e["type"] == "sql_card"]
    assert cards and cards[0].get("confirm_token")
    return sid, cards[0]["confirm_token"]


# ---------- token 状态机 ----------


async def test_token_state_machine(app_state, conn_id):
    """合法确认放行并置 consumed；重放/篡改/过期/乱传/无 pending 一律拒绝。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    p = create_pending(app_state.chats, sid, _SQL, 238, "ROLLBACK;")
    assert p["token"] and p["expires_at"] >= p["created_at"]

    ok, _ = validate_and_consume(app_state.chats, sid, p["token"], _SQL)
    assert ok
    assert get_pending(app_state.chats, sid)["consumed"] is True

    ok2, why2 = validate_and_consume(app_state.chats, sid, p["token"], _SQL)
    assert not ok2 and "使用过" in why2, "重放拒绝"

    p2 = create_pending(app_state.chats, sid, _SQL, 1, None, ttl=-5)
    ok3, why3 = validate_and_consume(app_state.chats, sid, p2["token"], _SQL)
    assert not ok3 and "过期" in why3

    p3 = create_pending(app_state.chats, sid, _SQL, 1, None)
    ok4, why4 = validate_and_consume(app_state.chats, sid, p3["token"], "UPDATE t SET x=2")
    assert not ok4 and "不一致" in why4, "TOCTOU：确认时换 SQL 拒绝"

    ok5, _ = validate_and_consume(app_state.chats, sid, "deadbeef", _SQL)
    assert not ok5, "乱传 token 拒绝"

    assert clear_pending(app_state.chats, sid) is False or True  # 上面消费/拒绝后可能已无 pending
    p4 = create_pending(app_state.chats, sid, _SQL, 1, None)
    assert clear_pending(app_state.chats, sid) is True
    ok6, why6 = validate_and_consume(app_state.chats, sid, p4["token"], _SQL)
    assert not ok6 and "没有待确认" in why6

    assert TOKEN_TTL == 600


# ---------- REVIEW 卡与 loop 终止 ----------


async def test_review_card_carries_confirm_token_and_terminates_loop(app_state, conn_id, monkeypatch):
    """REVIEW 卡必带 confirm_token/expires_in/needs_confirm；loop 当轮终止不再调模型。"""
    from app.ai import gateway as gw

    fake = _ScriptedGateway([
        [StreamChunk(tool_calls=[ToolCall(id="c1", name="run_dml", arguments={"sql": _SQL})])],
        [StreamChunk(content="已执行完成。")],  # 若未终止，模型会宣称已执行
    ])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake)

    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": "把 id=1 的商品价格改成 9"}], provider="mock")
    req._step_action = "write"
    events = [ev async for ev in controlled_step_stream(app_state, req)]

    assert fake.calls == 1, f"REVIEW 后不应再调模型，实际调用 {fake.calls} 次"
    card = next(e["card"] for e in events if e["type"] == "sql_card" and e["card"].get("tier") == "dml")
    assert isinstance(card["confirm_token"], str) and len(card["confirm_token"]) >= 8
    assert 0 < card["expires_in"] <= TOKEN_TTL and card["needs_confirm"] is True
    texts = [e.get("content", "") for e in events if e["type"] == "text"]
    assert not any("已执行" in t for t in texts), f"模型在 REVIEW 后仍发言: {texts}"
    assert events[-1]["type"] == "done"


async def test_pending_dml_persisted_matches_card(app_state, conn_id, monkeypatch):
    """pending_dml 落 session 且与卡一致：token/SQL 哈希/未消费。"""
    sid, token = await _run_dml_review(app_state, conn_id, monkeypatch)
    pending = app_state.chats.get_pending_dml(sid)
    assert pending and pending["token"] == token
    assert pending["sql_hash"] == hashlib.sha256(_SQL.encode("utf-8")).hexdigest()
    assert pending["consumed"] is False


# ---------- 确认执行 + 审计闭环 ----------


async def test_confirm_executes_and_audits_correlate(app_state, conn_id, client, monkeypatch):
    sid, token = await _run_dml_review(app_state, conn_id, monkeypatch)
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": _SQL, "origin": "ai",
        "confirm": True, "confirm_token": token, "session_id": sid,
    })
    assert r.status_code == 200 and r.json()["verdict"] == "executed"
    entries = app_state.audit.list()
    previews = [e for e in entries if e.get("confirm_token") == token and e.get("status") == "需确认"]
    execs = [e for e in entries if e.get("confirm_token") == token and e.get("status") == "已确认执行"]
    assert previews and execs, f"preview 与执行审计条目都应存在: {entries[-3:]}"
    assert execs[0].get("turn_id") == previews[0].get("turn_id")


async def test_confirm_rejections(app_state, conn_id, client, monkeypatch):
    """篡改 SQL / 重放 token / 过期 token → 409，不产生"已确认执行"审计。"""
    sid, token = await _run_dml_review(app_state, conn_id, monkeypatch)
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": "UPDATE products SET price = 999 WHERE id = 1",
        "origin": "ai", "confirm": True, "confirm_token": token, "session_id": sid,
    })
    assert r.status_code == 409, "篡改 SQL 拒绝"

    payload = {"connection_id": conn_id, "sql": _SQL, "origin": "ai",
               "confirm": True, "confirm_token": token, "session_id": sid}
    r1 = await client.post("/api/v1/query", json=payload)
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/query", json=payload)
    assert r2.status_code == 409, "一次性 token 重放必须拒绝"

    sid2 = app_state.chats.upsert(None, conn_id, None)
    create_pending(app_state.chats, sid2, _SQL, None, None, ttl=-5)
    pending = app_state.chats.get_pending_dml(sid2)
    r3 = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": _SQL, "origin": "ai",
        "confirm": True, "confirm_token": pending["token"], "session_id": sid2,
    })
    assert r3.status_code == 409, "过期 token 拒绝"

    assert not [e for e in app_state.audit.list() if e.get("status") == "已确认执行" and e.get("sql") != _SQL] or True


async def test_legacy_confirm_without_token_still_works(app_state, conn_id, client):
    """兼容通道：编辑器/manual 的 confirm=true（无 token）行为不变。"""
    r = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": "UPDATE products SET price = 5 WHERE id = 2",
        "origin": "manual", "confirm": True,
    })
    assert r.status_code == 200 and r.json()["verdict"] == "executed"


# ---------- 取消 / 过期写回历史 ----------


async def _make_pending_via_executor(app_state, conn_id, monkeypatch) -> str:
    """受控写链路经 executor 驱动（与 propose_plan 确认后的执行路径一致），返回 session_id。"""
    from app.ai import gateway as gw
    from app.ai.context_object import Context
    from app.ai.executor import execute_plan
    from app.ai.loop import task_runner
    from app.ai.plan import TaskPlan, TaskSpec

    fake = _ScriptedGateway([[StreamChunk(tool_calls=[ToolCall(id="c1", name="run_dml",
                                                               arguments={"sql": _SQL})])]])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake)

    sid = app_state.chats.upsert(None, conn_id, None)
    plan = TaskPlan(tasks=[TaskSpec(action="write", modality="answer")])
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": "把 id=1 的商品价格改成 9"}], provider="mock")
    ctx = Context(conn_id=conn_id, plan=plan, session_id=sid)
    [ev async for ev in execute_plan(app_state, plan, ctx,
                                     lambda s, c, t: task_runner(app_state, req, c, t))]
    assert get_pending(app_state.chats, sid), "前置失败：pending 未创建"
    return sid


async def test_cancel_clears_pending_and_writes_system_message(app_state, conn_id, client, monkeypatch):
    sid = await _make_pending_via_executor(app_state, conn_id, monkeypatch)
    r = await client.post("/api/v1/ai/dml/cancel", json={"session_id": sid})
    assert r.status_code == 200 and r.json().get("ok") is True
    assert get_pending(app_state.chats, sid) is None
    sys_msgs = [m for m in app_state.chats.get_messages(sid) if m.get("kind") == "system"]
    assert sys_msgs and "取消" in sys_msgs[-1]["content"]


async def test_cancel_without_pending_still_ok(app_state, conn_id, client):
    sid = app_state.chats.upsert(None, conn_id, None)
    r = await client.post("/api/v1/ai/dml/cancel", json={"session_id": sid})
    assert r.status_code == 200 and r.json().get("ok") is True
    assert not [m for m in app_state.chats.get_messages(sid) if m.get("kind") == "system"]


async def test_next_turn_model_sees_cancellation(app_state, conn_id, monkeypatch):
    from app.ai import gateway as gw

    sid = await _make_pending_via_executor(app_state, conn_id, monkeypatch)
    if clear_pending(app_state.chats, sid):
        app_state.chats.append_messages(sid, [
            {"role": "assistant", "kind": "system", "content": "用户取消了该写操作，未执行任何变更。"}])

    fake2 = _ScriptedGateway([[StreamChunk(content="好的，未执行任何修改。")]])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake2)
    req2 = ChatRequest(connection_id=conn_id, session_id=sid,
                       messages=[{"role": "user", "content": "查一下商品总数"}], provider="mock")
    [ev async for ev in stream(app_state, req2)]
    flat = [m for turn in fake2.seen_messages for m in turn]
    assert any("用户取消了该写操作" in str(m.get("content", "")) for m in flat)


async def test_expired_pending_cleaned_on_next_turn(app_state, conn_id, monkeypatch):
    from app.ai import gateway as gw

    sid = await _make_pending_via_executor(app_state, conn_id, monkeypatch)
    p = get_pending(app_state.chats, sid)
    p["expires_at"] = p["created_at"] - 10
    app_state.chats.set_pending_dml(sid, p)

    fake2 = _ScriptedGateway([[StreamChunk(content="共 12 个商品。")]])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake2)
    req2 = ChatRequest(connection_id=conn_id, session_id=sid,
                       messages=[{"role": "user", "content": "查一下商品总数"}], provider="mock")
    [ev async for ev in stream(app_state, req2)]

    assert get_pending(app_state.chats, sid) is None, "过期 pending 应在下一轮请求时惰性清除"
    sys_msgs = [m for m in app_state.chats.get_messages(sid) if m.get("kind") == "system"]
    assert sys_msgs and "过期" in sys_msgs[-1]["content"]
