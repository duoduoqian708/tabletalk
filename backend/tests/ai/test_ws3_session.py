"""WS3 T3.1 验收：服务端历史组装 / req.messages 降级兼容通道 / kind 元数据补齐。

- 服务端历史优先：同 session 连续两问，第二问的 messages 来自 state.chats（断言不含前端重复传的历史）。
- 兼容通道：session 无历史时（旧会话/测试直连）仍用 req.messages 全量。
- 铁律1：sql_card 只喂 D7 骨架（result_id/表名/verdict/rowcount），原始卡含 result.rows，feed 回模型即行数据出网绕过脱敏。
"""
from __future__ import annotations

import json

import pytest

from app.ai.dto import ChatRequest
from app.ai.gateway import StreamChunk, ToolCall
from app.ai.loop import _server_history_for_model, stream
from app.api.ai import _events_to_messages, _persist_artifacts


@pytest.mark.asyncio
async def test_get_messages_roundtrip_with_kinds(app_state, conn_id):
    """T3.1：upersert+append 后 get_messages 还原含 kind 元数据（stage/think 已补），只读不刷新。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    app_state.chats.append_messages(sid, [
        {"role": "user", "kind": "text", "content": "查一下订单总数"},
        {"role": "assistant", "kind": "stage", "content": json.dumps({"type": "stage", "stage": "intent", "value": ["query"]})},
        {"role": "assistant", "kind": "think", "content": "调用 run_query"},
        {"role": "assistant", "kind": "sql_card", "content": json.dumps({"verdict": "allow", "result": {"row_count": 5}}), "sql": "SELECT COUNT(*) t FROM orders", "verdict": "allow"},
    ])
    msgs = app_state.chats.get_messages(sid)
    kinds = [m["kind"] for m in msgs]
    assert kinds == ["text", "stage", "think", "sql_card"], f"kind 元数据应完整(含 stage/think)，got {kinds}"
    assert msgs[-1]["sql"] == "SELECT COUNT(*) t FROM orders"


async def test_events_to_messages_records_stage_and_think(app_state):
    """落库侧补齐：_events_to_messages 补 think/stage 进 kind（gate 无独立事件，verdict 在 sql_card 上）。"""
    events = [
        {"type": "turn_start", "connection": "c"},
        {"type": "stage", "stage": "intent", "value": ["query"]},
        {"type": "think", "text": "调用 run_query"},
        {"type": "sql_card", "card": {"verdict": "allow", "sql": "SELECT 1", "result": {"row_count": 1}}},
        {"type": "text", "content": "结果如上"},
        {"type": "done"},
    ]
    req_msgs = [{"role": "user", "content": "查订单"}]
    out = _events_to_messages(events, req_msgs)
    kinds = [m["kind"] for m in out]
    assert "stage" in kinds, "stage 应落库进 kind"
    assert "think" in kinds, "think 应落库进 kind（时间线可重建）"
    assert "sql_card" in kinds
    assert "text" in kinds
    assert any(m.get("role") == "user" and m["kind"] == "text" for m in out)


async def test_server_history_assembles_second_turn(app_state, conn_id, monkeypatch):
    """集成验收：同 session 连续两问，第二问 messages 来自服务端（req.messages 只带新问题），
    sql_card 只喂 D7 骨架不进原始行。"""
    import app.ai.loop as loop

    # 第一问：直接落一条用户 + 一条 sql_card 进服务端会话（绕过真实 SQL）
    sid = app_state.chats.upsert(None, conn_id, None)
    app_state.chats.append_messages(sid, [
        {"role": "user", "kind": "text", "content": "查一下订单总数"},
        {"role": "assistant", "kind": "sql_card",
         "content": json.dumps({"verdict": "allow", "sql": "SELECT COUNT(*) FROM orders",
                                "result": {"columns": ["c"], "rows": [["3"]], "row_count": 3}}),
         "sql": "SELECT COUNT(*) FROM orders", "verdict": "allow"},
    ])

    recorder = {"messages": None}

    class _RecProv:
        async def chat_stream(self, messages, tools):
            recorder["messages"] = messages  # 记录第二轮喂给模型的完整消息
            yield StreamChunk(content="好")

    monkeypatch.setattr(loop.gw, "build_provider", lambda *a, **k: _RecProv())

    # 第二问（追问）：req.messages 只带新问题（WS3 后的前端行为），session 复用第一问
    req2 = ChatRequest(connection_id=conn_id, session_id=sid,
                       messages=[{"role": "user", "content": "那按周统计呢"}], provider="mock")
    events = [ev async for ev in stream(app_state, req2)]
    assert events[-1]["type"] == "done"

    hist = recorder["messages"]
    assert hist is not None, "第二轮应调用 provider"
    text_roles = [(m["role"], m["content"]) for m in hist if m["role"] in ("user", "assistant")]
    contents = [c for _, c in text_roles]

    # 第一问的用户消息来自服务端（req.messages 里没有它）
    assert any("查一下订单总数" in c for c in contents), "第一问 user 应从服务端历史还原"
    # sql_card 骨架（verdict/rowcount）进上下文，但原始行数据不得进（铁律1）
    assert any("row_count=3" in c or "row_count" in c for c in contents), "应含 rowcount 骨架"
    assert not any("3]]" in c or str(["3"]) in c for c in contents), "原始行数据不得 feed 回模型"
    # 新问题在
    assert any("那按周统计呢" in c for c in contents)
    # 前端未重复传历史 → 用户消息除第一问外仅此一条新问题（无重复）
    user_msgs = [c for m, c in text_roles if m == "user"]
    assert user_msgs.count("那按周统计呢") == 1


async def test_frontend_history_compat_when_no_server_history(app_state, conn_id, monkeypatch):
    """兼容通道：session 无历史（空 store / session_id=None / 测试直连）时用 req.messages 全量。"""
    import app.ai.loop as loop

    recorder = {"messages": None}

    class _RecProv:
        async def chat_stream(self, messages, tools):
            recorder["messages"] = messages
            yield StreamChunk(content="好")

    monkeypatch.setattr(loop.gw, "build_provider", lambda *a, **k: _RecProv())

    full_history = [
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": "已查出 3 条"},
        {"role": "user", "content": "那看产品呢"},
    ]
    req = ChatRequest(connection_id=conn_id, session_id=None, messages=full_history, provider="mock")
    events = [ev async for ev in stream(app_state, req)]
    assert events[-1]["type"] == "done"

    hist = recorder["messages"] or []
    contents = [m["content"] for m in hist if m.get("role") in ("user", "assistant")]
    assert any("查订单" in c for c in contents)
    assert any("已查出 3 条" in c for c in contents)  # assistant 原文直通（无服务端历史，不过骨架化）
    assert any("那看产品呢" in c for c in contents)


@pytest.mark.asyncio
async def test_server_history_skeleton_never_embeds_rows(app_state, conn_id):
    """铁律1 单元级：_server_history_for_model 对含原始行的卡只出骨架，绝不出 columns/rows。"""
    rows = [
        {"role": "assistant", "kind": "sql_card",
         "content": json.dumps({"verdict": "block", "sql": "UPDATE t SET x=1",
                                "result": {"columns": ["id", "phone"], "rows": [["1", "13800001111"]], "row_count": 7}}),
         "sql": "UPDATE t SET x=1", "verdict": "block"},
    ]
    out = _server_history_for_model(rows)
    assert len(out) == 1
    c = out[0]["content"]
    assert "13800001111" not in c, "原始行数据不得进入模型上下文"
    assert "row_count=7" in c
    assert "verdict=block" in c
    assert "columns" not in c


# ---- WS3 T3.2：result_id 工件 ----

@pytest.mark.asyncio
async def test_artifact_roundtrip_by_id(app_state, conn_id):
    """验收：两轮查询后按 id 可取回两个工件；结果（columns/row_count/rows）持久化。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    events = [
        {"type": "sql_card", "card": {"result_id": "rAAA", "verdict": "allow",
                                      "sql": "SELECT * FROM t1", "result": {"columns": ["a"], "row_count": 2, "rows": [["1"]], "truncated": False}}},
        {"type": "sql_card", "card": {"result_id": "rBBB", "verdict": "allow",
                                      "sql": "SELECT * FROM t2", "result": {"columns": ["b"], "row_count": 1, "rows": [["9"]], "truncated": True}}},
    ]
    _persist_artifacts(app_state, sid, events)
    a1 = app_state.chats.get_artifact(sid, "rAAA")
    a2 = app_state.chats.get_artifact(sid, "rBBB")
    assert a1 is not None and a1["columns"] == ["a"] and a1["row_count"] == 2
    assert a2 is not None and a2["row_count"] == 1 and a2["truncated"] is True
    assert a1["rows"] == [["1"]]


async def test_artifact_cross_session_and_missing_rejected(app_state, conn_id):
    sid1 = app_state.chats.upsert(None, conn_id, None)
    app_state.chats.save_artifact(sid1, "rX1", {"row_count": 3, "rows": [["1"], ["2"], ["3"]]})
    # 跨 session 拒绝
    assert app_state.chats.get_artifact("s_other_session", "rX1") is None
    # 不存在的 id 友好返回 None
    assert app_state.chats.get_artifact(sid1, "rNOPE") is None
    # 同 session 可取回
    assert app_state.chats.get_artifact(sid1, "rX1") is not None


async def test_artifact_rows_truncated_on_overflow(app_state, conn_id):
    """验收：行数超过上限时截断存储（防御性截断，行上限与 run_query 一致）。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    # 构造超过 query_max_rows（默认 1000）的行
    many = [[str(i)] for i in range(1050)]
    events = [{"type": "sql_card", "card": {"result_id": "rBIG", "verdict": "allow", "sql": "SELECT 1",
                                            "result": {"columns": ["n"], "row_count": 1050, "rows": many, "truncated": True}}}]
    _persist_artifacts(app_state, sid, events)
    art = app_state.chats.get_artifact(sid, "rBIG")
    assert art is not None
    assert len(art["rows"]) <= 1000 + 1, f"超限行应被截断，got {len(art['rows'])}"
    assert art["truncated"] is True


@pytest.mark.asyncio
async def test_sql_card_gets_result_id_in_loop(app_state, conn_id, monkeypatch):
    """loop 内每张 sql_card 自行落 result_id（引用寻址）。"""
    import app.ai.loop as loop
    from app.ai.dto import ChatRequest
    from app.ai.preflight import PreflightResult
    from app.ai.tools.registry import ToolOutcome

    async def fake_execute(state, name, args, conn_id, include_data=False):
        return ToolOutcome(
            result={"ok": True, "columns": ["a"], "row_count": 1},
            card={"tier": "read", "verdict": "allow", "sql": "SELECT 1",
                  "sqlglot": None, "result": {"columns": ["a"], "row_count": 1, "rows": [["1"]], "truncated": False}},
            think="ok",
        )

    monkeypatch.setattr(loop, "execute_tool", fake_execute)

    emitted = {"n": 0}

    class _FakeProv:
        async def chat_stream(self, messages, tools):
            if emitted["n"] < 1:
                emitted["n"] += 1
                yield StreamChunk(tool_calls=[ToolCall(
                    id="tc_x", name="run_query", arguments={"sql": "SELECT 1"},
                )])
            else:
                yield StreamChunk(content="完成")

    monkeypatch.setattr(loop.gw, "build_provider", lambda *a, **k: _FakeProv())
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "查订单"}], provider="mock")
    req.skill_id = "query"
    req._preflight = PreflightResult(intent="query", tags=[], degraded=True, is_followup=False, followup_tables=[],)

    cards = []
    async for ev in loop.chat_stream(app_state, req):
        if ev["type"] == "sql_card":
            cards.append(ev["card"])
    assert cards, "应产生 sql_card"
    assert all(c.get("result_id") for c in cards), f"每张卡应有 result_id，got {[c.get('result_id') for c in cards]}"