"""WS4 T4.1 验收：confirm_token 状态机。
合法确认放行（置 consumed）；二次消费拒绝；过期拒绝；SQL 哈希不符（TOCTOU）拒绝；token 不匹配拒绝。
"""
from __future__ import annotations

import pytest

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