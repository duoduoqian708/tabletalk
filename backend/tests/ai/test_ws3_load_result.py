"""WS3 T3.3 验收：load_result 工具。
standard 档工件回喂被 redact；strict 拦行数据（只回列名+行数）；不存在/跨 session 的 id 报错友好。
"""
from __future__ import annotations

import pytest

from app.ai.tools.data_tier import apply_data_tier
from app.ai.tools.load_result import _load_result
from app.ai.tools.registry import set_active_session


@pytest.fixture
def session_store(app_state, conn_id):
    """真实 ChatStore + 预置工件（含名义敏感手机号行）。"""
    store = app_state.chats
    sid = store.upsert(None, conn_id, None)
    store.save_artifact(sid, "r1", {
        "columns": ["id", "name", "phone"],
        "row_count": 3,
        "rows": [["1", "张三", "13800001111"], ["2", "李四", "18800002222"], ["3", "王五", "13900003333"]],
        "truncated": False,
    })
    return {"store": store, "sid": sid, "conn": conn_id}


async def test_load_result_standard_redacts_sensitive_rows(app_state, session_store):
    """standard 档：敏感（手机号）被 token；列名/行数/样例行回显。"""
    app_state.runtime.update({"privacy_mode": "standard"})
    set_active_session(session_store["sid"])
    out = await _load_result(app_state, {"result_id": "r1"}, session_store["conn"])
    r = out.result
    assert r["ok"] is True
    assert r["row_count"] == 3
    assert r["columns"] == ["id", "name", "phone"]
    flat = [str(c) for row in (r["sample_rows"] or []) for c in row]
    assert not any(("138" in c or "188" in c or "139" in c) for c in flat), f"敏感值应被脱敏:{flat}"
    assert "13800001111" not in flat


async def test_load_result_strict_blocks_rows_only_shape(app_state, session_store):
    """strict 档：行数据一律不返回，只回列名+行数。"""
    app_state.runtime.update({"privacy_mode": "strict"})
    set_active_session(session_store["sid"])
    out = await _load_result(app_state, {"result_id": "r1"}, session_store["conn"])
    r = out.result
    assert r["ok"] is True
    assert r["row_count"] == 3
    assert r["columns"] == ["id", "name", "phone"]
    assert r["sample_rows"] == []
    assert r["rows_returned"] == 0
    assert "note" in r


async def test_load_result_open_returns_raw(app_state, session_store):
    """open 档：明文样例行回显（only-trust 由用户自行选择）。"""
    app_state.runtime.update({"privacy_mode": "open"})
    set_active_session(session_store["sid"])
    out = await _load_result(app_state, {"result_id": "r1"}, session_store["conn"])
    assert out.result["ok"] is True and out.result["row_count"] == 3


async def test_load_result_missing_and_cross_session(app_state, session_store):
    """不存在的 id / 跨 session 的 id → 友好报错（不泄露别的会话存在性）。
    result_id 是全球唯一 PK，归属唯一会话；从别的会话取 → 未命中 → 友好报错。"""
    store, conn, sid = session_store["store"], session_store["conn"], session_store["sid"]
    # 当前会话：不存在
    set_active_session(sid)
    out = await _load_result(app_state, {"result_id": "r_nope"}, conn)
    assert out.result["ok"] is False and "不存在" in out.result["error"]
    # 跨 session：r1 归属 sid，从新会话取 → 未命中
    other_sid = store.upsert(None, conn, None)
    set_active_session(other_sid)
    out2 = await _load_result(app_state, {"result_id": "r1"}, conn)
    assert out2.result["ok"] is False
    assert ("跨会话" in out2.result["error"]) or ("不存在" in out2.result["error"])
    # 归属会话仍可取到（对照）
    set_active_session(sid)
    out3 = await _load_result(app_state, {"result_id": "r1"}, conn)
    assert out3.result["ok"] is True and out3.result["row_count"] == 3


async def test_load_result_no_session_friendly(app_state, session_store):
    set_active_session(None)
    out = await _load_result(app_state, {"result_id": "r1"}, session_store["conn"])
    assert out.result["ok"] is False


def test_data_tier_shared_three_modes(app_state, session_store):
    """data_tier 三档复用：strict 无行 / open 明文 / standard redact。"""
    cols = ["id", "phone"]
    rows = [["1", "18800009999"]]
    app_state.runtime.update({"privacy_mode": "strict"})
    assert apply_data_tier(app_state, session_store["conn"], rows, cols)[0] is None
    app_state.runtime.update({"privacy_mode": "open"})
    assert apply_data_tier(app_state, session_store["conn"], rows, cols)[0] == rows
    app_state.runtime.update({"privacy_mode": "standard"})
    r3, _ = apply_data_tier(app_state, session_store["conn"], rows, cols)
    assert r3 is not None and "18800009999" not in str(r3)