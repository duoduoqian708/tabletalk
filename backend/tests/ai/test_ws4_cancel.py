"""WS4 T4.4 验收：取消/过期写回历史。
取消 -> 清 pending_dml + session 追加 kind=system 消息（下轮模型上下文可见）；
过期 -> 下一轮请求开始时惰性清除并写回系统消息。
"""
from __future__ import annotations

import pytest

from app.ai.dto import ChatRequest
from app.ai.gateway import StreamChunk, ToolCall
from app.ai.loop import stream
from app.safety.confirm import create_pending, get_pending

_SQL = "UPDATE products SET price = 9 WHERE id = 1"


class _ScriptedGateway:
    def __init__(self, turns):
        self.turns = turns
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, ctx=None):
        self.calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.turns) - 1)
        for t in self.turns[idx]:
            yield t


async def _make_pending(app_state, conn_id, monkeypatch, ttl=600) -> str:
    """chat 内触发 run_dml REVIEW，返回 session_id。"""
    from app.ai import gateway as gw
    import app.ai.decompose as _dec
    from app.ai.plan import TaskPlan, TaskSpec

    fake = _ScriptedGateway([[StreamChunk(tool_calls=[ToolCall(id="c1", name="run_dml",
                                                                arguments={"sql": _SQL})])]])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake)
    # 2026-09：意图归 LLM（无关键词层）→ 测试直接指定 write 计划，驱动完整写链路
    async def _write_plan(*a, **k):
        return TaskPlan(tasks=[TaskSpec(action="write", modality="answer")], degraded=True)
    monkeypatch.setattr(_dec, "decompose", _write_plan)
    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid, skill_id="write",
                      messages=[{"role": "user", "content": "把 id=1 的商品价格改成 9"}], provider="mock")
    [ev async for ev in stream(app_state, req)]
    assert get_pending(app_state.chats, sid), "前置失败：pending 未创建"
    return sid


# ---- 取消 ----


async def test_cancel_clears_pending_and_writes_system_message(app_state, conn_id, client, monkeypatch):
    sid = await _make_pending(app_state, conn_id, monkeypatch)
    r = await client.post("/api/v1/ai/dml/cancel", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    assert get_pending(app_state.chats, sid) is None, "取消后 pending 应被清除"
    msgs = app_state.chats.get_messages(sid)
    sys_msgs = [m for m in msgs if m.get("kind") == "system"]
    assert sys_msgs and "取消" in sys_msgs[-1]["content"], f"缺取消系统消息: {msgs[-3:]}"


async def test_cancel_without_pending_still_ok(app_state, conn_id, client):
    sid = app_state.chats.upsert(None, conn_id, None)
    r = await client.post("/api/v1/ai/dml/cancel", json={"session_id": sid})
    assert r.status_code == 200
    assert r.json().get("ok") is True
    # 无 pending 时不应留垃圾系统消息
    msgs = app_state.chats.get_messages(sid)
    assert not [m for m in msgs if m.get("kind") == "system"]


async def test_next_turn_model_sees_cancellation(app_state, conn_id, monkeypatch):
    from app.ai import gateway as gw

    sid = await _make_pending(app_state, conn_id, monkeypatch)
    # 取消（直接走服务层语义：清 + 写消息）
    from app.safety.confirm import clear_pending
    if clear_pending(app_state.chats, sid):
        app_state.chats.append_messages(sid, [
            {"role": "assistant", "kind": "system", "content": "用户取消了该写操作，未执行任何变更。"}])

    turn2 = [[StreamChunk(content="好的，未执行任何修改。")]]
    fake2 = _ScriptedGateway(turn2)
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake2)
    req2 = ChatRequest(connection_id=conn_id, session_id=sid,
                       messages=[{"role": "user", "content": "查一下商品总数"}], provider="mock")
    [ev async for ev in stream(app_state, req2)]
    flat = [m for turn in fake2.seen_messages for m in turn]
    assert any("用户取消了该写操作" in str(m.get("content", "")) for m in flat), \
        "下轮模型上下文应包含取消信号"


# ---- 过期（惰性清理） ----


async def test_expired_pending_cleaned_on_next_turn(app_state, conn_id, monkeypatch):
    from app.ai import gateway as gw

    sid = await _make_pending(app_state, conn_id, monkeypatch)
    # 手动把 expires_at 改到过去
    p = get_pending(app_state.chats, sid)
    p["expires_at"] = p["created_at"] - 10
    app_state.chats.set_pending_dml(sid, p)

    fake2 = _ScriptedGateway([[StreamChunk(content="共 12 个商品。")]])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake2)
    req2 = ChatRequest(connection_id=conn_id, session_id=sid,
                       messages=[{"role": "user", "content": "查一下商品总数"}], provider="mock")
    [ev async for ev in stream(app_state, req2)]

    assert get_pending(app_state.chats, sid) is None, "过期 pending 应在下一轮请求时惰性清除"
    msgs = app_state.chats.get_messages(sid)
    sys_msgs = [m for m in msgs if m.get("kind") == "system"]
    assert sys_msgs and "过期" in sys_msgs[-1]["content"], f"缺过期系统消息: {msgs[-3:]}"
