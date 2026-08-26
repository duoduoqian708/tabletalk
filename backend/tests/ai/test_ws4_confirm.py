"""WS4 T4.1 验收：confirm_token 状态机。
合法确认放行（置 consumed）；二次消费拒绝；过期拒绝；SQL 哈希不符（TOCTOU）拒绝；token 不匹配拒绝。

WS4 T4.2 验收：loop 终止与确认卡下发——
run_dml REVIEW 后 loop 当轮终止（模型不再发言）；sql_card 附 confirm_token/expires_in；pending_dml 落 session。
"""
from __future__ import annotations

import hashlib

import pytest

from app.ai.dto import ChatRequest
from app.ai.gateway import StreamChunk, ToolCall
from app.ai.loop import chat_stream
from app.safety.confirm import (
    TOKEN_TTL,
    clear_pending,
    create_pending,
    get_pending,
    validate_and_consume,
)


@pytest.fixture
def sess(app_state, conn_id):
    sid = app_state.chats.upsert(None, conn_id, None)
    return {"store": app_state.chats, "sid": sid}


async def test_valid_confirm_consumes(sess):
    """合法确认：未过期/未消费/哈希一致 -> 放行并置 consumed。"""
    p = create_pending(sess["store"], sess["sid"], "UPDATE t SET x=1", 238, "ROLLBACK;")
    assert p["token"]
    assert p["expires_at"] >= p["created_at"]
    ok, why = validate_and_consume(sess["store"], sess["sid"], p["token"], "UPDATE t SET x=1")
    assert ok, why
    stored = get_pending(sess["store"], sess["sid"])
    assert stored["consumed"] is True, "消费后应置 consumed 并持久化"


async def test_double_consume_rejected(sess):
    """一次性：同 token 二次消费拒绝（用户点两次 / 重放）。"""
    p = create_pending(sess["store"], sess["sid"], "UPDATE t SET x=1", 1, None)
    ok1, _ = validate_and_consume(sess["store"], sess["sid"], p["token"], "UPDATE t SET x=1")
    assert ok1
    ok2, why2 = validate_and_consume(sess["store"], sess["sid"], p["token"], "UPDATE t SET x=1")
    assert not ok2 and "使用过" in why2


async def test_expired_rejected(sess):
    """过期拒绝：手动把 expires_at 改到过去（或用负 ttl）。"""
    p = create_pending(sess["store"], sess["sid"], "UPDATE t SET x=1", 1, None, ttl=-5)
    ok, why = validate_and_consume(sess["store"], sess["sid"], p["token"], "UPDATE t SET x=1")
    assert not ok and "过期" in why


async def test_sql_tampered_rejected_toctou(sess):
    """TOCTOU：确认时换了 SQL（哈希不符）-> 拒绝，要求重走 preview。"""
    p = create_pending(sess["store"], sess["sid"], "UPDATE t SET x=1", 1, None)
    ok, why = validate_and_consume(sess["store"], sess["sid"], p["token"], "UPDATE t SET x=2")
    assert not ok and "不一致" in why


async def test_token_mismatch_rejected(sess):
    """token 不匹配：乱传 token -> 拒绝。"""
    create_pending(sess["store"], sess["sid"], "UPDATE t SET x=1", 1, None)
    ok, why = validate_and_consume(sess["store"], sess["sid"], "deadbeef", "UPDATE t SET x=1")
    assert not ok and ("不匹配" in why or "没有" in why)


async def test_no_pending_rejected(sess):
    """无待确认（已处理/取消）-> 拒绝且给出明确原因。"""
    ok, why = validate_and_consume(sess["store"], sess["sid"], "whatever", "UPDATE t SET x=1")
    assert not ok and "没有待确认" in why


async def test_clear_pending(sess):
    """取消/过期清除：存在时返回 True，清后可查。"""
    create_pending(sess["store"], sess["sid"], "UPDATE t SET x=1", 1, None)
    assert get_pending(sess["store"], sess["sid"]) is not None
    assert clear_pending(sess["store"], sess["sid"]) is True
    assert get_pending(sess["store"], sess["sid"]) is None
    # 二次清除：不存在返回 False
    assert clear_pending(sess["store"], sess["sid"]) is False


async def test_constant_ttl_ten_minutes():
    assert TOKEN_TTL == 600


# ---- T4.2：loop 终止与确认卡下发 ----

_WRITE_Q = "把库存为 0 的商品提价 10%"


def _dml_review_cards(events) -> list[dict]:
    return [e["card"] for e in events
            if e["type"] == "sql_card"
            and e["card"].get("tier") == "dml" and e["card"].get("verdict") == "review"]


class _ScriptedGateway:
    """脚本化网关：第 N 次调用返回第 N 组 chunk；记录调用次数供断言。"""

    def __init__(self, turns):
        self.turns = turns
        self.calls = 0

    async def chat_stream(self, messages, tools=None, ctx=None):
        idx = min(self.calls, len(self.turns) - 1)
        self.calls += 1
        for t in self.turns[idx]:
            yield t


async def test_review_card_carries_confirm_token_and_expires_in(app_state, conn_id):
    """REVIEW 卡必带 confirm_token / expires_in / needs_confirm，前端三态确认卡的数据源。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": _WRITE_Q}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    cards = _dml_review_cards(events)
    assert cards, events
    card = cards[0]
    tok = card.get("confirm_token")
    assert isinstance(tok, str) and len(tok) >= 8, f"卡片缺有效 confirm_token: {card}"
    ei = card.get("expires_in")
    assert isinstance(ei, int) and 0 < ei <= 600, f"卡片缺有效 expires_in: {card}"
    assert card.get("needs_confirm") is True


async def test_run_dml_review_terminates_loop_no_model_speak(app_state, conn_id, monkeypatch):
    """run_dml 返回 REVIEW 后 loop 当轮终止：不再有第二次模型调用（防幻觉"已执行"）。"""
    from app.ai import gateway as gw

    turn1 = [StreamChunk(tool_calls=[ToolCall(id="c1", name="run_dml",
                                              arguments={"sql": "UPDATE products SET price = 9 WHERE id = 1"})])]
    turn2 = [StreamChunk(content="已执行完成。")]  # 若未终止，模型会宣称已执行
    fake = _ScriptedGateway([turn1, turn2])
    monkeypatch.setattr(gw, "build_provider", lambda cfg: fake)

    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid, skill_id="write",
                      messages=[{"role": "user", "content": "把 id=1 的商品价格改成 9"}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]

    assert fake.calls == 1, f"REVIEW 后不应再调模型，实际调用 {fake.calls} 次"
    texts = [e.get("content", "") for e in events if e["type"] == "text"]
    assert not any("已执行" in t for t in texts), f"模型在 REVIEW 后仍发言: {texts}"
    assert events[-1]["type"] == "done"


async def test_pending_dml_persisted_matches_card(app_state, conn_id):
    """pending_dml 落 session 且与卡一致：token 相同、SQL 哈希一致、未消费、preview 对应。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": _WRITE_Q}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    cards = _dml_review_cards(events)
    assert cards
    card = cards[0]
    pending = app_state.chats.get_pending_dml(sid)
    assert pending, "pending_dml 未写入会话存储"
    assert pending["token"] == card["confirm_token"]
    assert pending["sql_hash"] == hashlib.sha256(card["sql"].encode("utf-8")).hexdigest()
    assert pending["consumed"] is False
    assert pending["preview"] == card.get("preview_rows")