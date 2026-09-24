"""AI 会话主流程：服务端历史组装 / 工件持久化 / 跨连接校验 / 上下文压缩。

行为来源（合并自原 ws3_session / ws3_session_switch / ws3_compress 验收文件）：
- 服务端历史优先，前端 req.messages 只带新问题；无服务端历史时走全量兼容通道。
- 铁律1：sql_card 只喂 D7 骨架（result_id/verdict/row_count），原始行数据永不出网。
- 工件按 result_id 存取，超限截断。
- 跨连接复用 session 一律 409。
- 压缩：机械优先零出网，压不完才 LLM 兜底（先写 egress 审计）。
"""
from __future__ import annotations

import json

import pytest

from app.ai.dto import ChatRequest
from app.ai.gateway import StreamChunk
from app.ai.loop import _server_history_for_model, stream
from app.ai.tools.sql import _run_query


# ---------- 服务端历史 + 骨架（铁律1） ----------


async def test_server_history_assembles_second_turn(app_state, conn_id, monkeypatch):
    """同 session 连续两问：第二问 messages 来自服务端历史；卡片只喂骨架，原始行不回模型。"""
    import app.ai.loop as loop

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
        async def chat_stream(self, messages, tools, ctx=None):
            recorder["messages"] = messages
            yield StreamChunk(content="好")

    from app.ai import gateway as _gwl
    monkeypatch.setattr(_gwl, "build_provider", lambda *a, **k: _RecProv())

    req2 = ChatRequest(connection_id=conn_id, session_id=sid,
                       messages=[{"role": "user", "content": "那按周统计呢"}], provider="mock")
    events = [ev async for ev in stream(app_state, req2)]
    assert events[-1]["type"] == "done"

    hist = recorder["messages"]
    assert hist is not None, "第二轮应调用 provider"
    contents = [m["content"] for m in hist if m["role"] in ("user", "assistant")]
    assert any("查一下订单总数" in c for c in contents), "第一问 user 应从服务端历史还原"
    assert any("row_count" in c for c in contents), "应含 rowcount 骨架"
    assert not any("3]]" in c for c in contents), "原始行数据不得 feed 回模型"
    assert any("那按周统计呢" in c for c in contents)


async def test_frontend_history_compat_when_no_server_history(app_state, conn_id, monkeypatch):
    """兼容通道：session 无历史（session_id=None / 测试直连）时用 req.messages 全量。"""
    import app.ai.loop as loop

    recorder = {"messages": None}

    class _RecProv:
        async def chat_stream(self, messages, tools, ctx=None):
            recorder["messages"] = messages
            yield StreamChunk(content="好")

    from app.ai import gateway as _gwl
    monkeypatch.setattr(_gwl, "build_provider", lambda *a, **k: _RecProv())

    full_history = [
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": "已查出 3 条"},
        {"role": "user", "content": "那看产品呢"},
    ]
    req = ChatRequest(connection_id=conn_id, session_id=None, messages=full_history, provider="mock")
    events = [ev async for ev in stream(app_state, req)]
    assert events[-1]["type"] == "done"

    contents = [m["content"] for m in (recorder["messages"] or []) if m.get("role") in ("user", "assistant")]
    assert any("查订单" in c for c in contents)
    assert any("已查出 3 条" in c for c in contents)  # 无服务端历史 → assistant 原文直通


def test_skeleton_never_embeds_rows():
    """铁律1 单元级：_server_history_for_model 对含原始行的卡只出骨架，绝不出 rows。"""
    rows = [
        {"role": "assistant", "kind": "sql_card",
         "content": json.dumps({"verdict": "block", "sql": "UPDATE t SET x=1",
                                "result": {"columns": ["id", "phone"], "rows": [["1", "13800001111"]], "row_count": 7}}),
         "sql": "UPDATE t SET x=1", "verdict": "block"},
    ]
    out = _server_history_for_model(rows)
    c = out[0]["content"]
    assert "13800001111" not in c and "columns" not in c
    assert "row_count=7" in c and "verdict=block" in c


def test_kind_metadata_roundtrip(app_state, conn_id):
    """kind 元数据（text/stage/think/sql_card）落库还原完整，时间线可重建。"""
    sid = app_state.chats.upsert(None, conn_id, None)
    app_state.chats.append_messages(sid, [
        {"role": "user", "kind": "text", "content": "查一下订单总数"},
        {"role": "assistant", "kind": "think", "content": "调用 run_query"},
        {"role": "assistant", "kind": "sql_card", "content": json.dumps({"verdict": "allow"}),
         "sql": "SELECT 1", "verdict": "allow"},
    ])
    kinds = [m["kind"] for m in app_state.chats.get_messages(sid)]
    assert kinds == ["text", "think", "sql_card"]


# ---------- 工件（result_id 引用寻址） ----------


async def test_artifact_roundtrip_and_isolation(app_state, conn_id):
    """按 result_id 取回工件；跨 session 拒绝；未知 id 返回 None；超限行截断。"""
    from app.api.ai import _persist_artifacts

    sid = app_state.chats.upsert(None, conn_id, None)
    many = [[str(i)] for i in range(1050)]
    _persist_artifacts(app_state, sid, [
        {"type": "sql_card", "card": {"result_id": "rAAA", "verdict": "allow", "sql": "SELECT 1",
                                      "result": {"columns": ["a"], "row_count": 2, "rows": [["1"]], "truncated": False}}},
        {"type": "sql_card", "card": {"result_id": "rBIG", "verdict": "allow", "sql": "SELECT 2",
                                      "result": {"columns": ["n"], "row_count": 1050, "rows": many, "truncated": True}}},
    ])
    a1 = app_state.chats.get_artifact(sid, "rAAA")
    assert a1 is not None and a1["columns"] == ["a"] and a1["rows"] == [["1"]]
    big = app_state.chats.get_artifact(sid, "rBIG")
    assert big is not None and big["truncated"] is True and len(big["rows"]) <= 1001, "超限行应被截断"
    assert app_state.chats.get_artifact("s_other_session", "rAAA") is None, "跨 session 拒绝"
    assert app_state.chats.get_artifact(sid, "rNOPE") is None


# ---------- 跨连接校验 ----------


async def test_cross_connection_session_rejected_409(client, app_state, conn_id, demo_db):
    """session 属于 A、请求 connection_id=B → 409；同连接放行；未知 session 视为新会话。"""
    conn_b = app_state.connections.create({"name": "b", "dialect": "sqlite", "file": str(demo_db)}).id
    sid = app_state.chats.upsert(None, conn_id, None)

    r = await client.post("/api/v1/ai/chat", json={
        "connection_id": conn_b,
        "messages": [{"role": "user", "content": "查一下订单"}],
        "session_id": sid, "provider": "mock",
    })
    assert r.status_code == 409, f"跨源会话应 409，got {r.status_code}"

    r2 = await client.post("/api/v1/ai/chat", json={
        "connection_id": conn_id,
        "messages": [{"role": "user", "content": "查一下订单总数"}],
        "session_id": "s_stale_unknown_id", "provider": "mock",
    })
    assert r2.status_code == 200, "未知 session 应视为新会话放行"
    assert app_state.chats.get_connection("s_stale_unknown_id") == conn_id


# ---------- 上下文压缩（机械优先，LLM 兜底） ----------


def _big_history(n_old: int = 150):
    rows = [{"role": "assistant", "kind": "text", "content": f"叙事行 {i}: " + "x" * 400,
             "sql": None, "verdict": None} for i in range(n_old)]
    rows.append({"role": "assistant", "kind": "sql_card",
                 "content": json.dumps({"result_id": "rKEEP", "verdict": "allow", "sql": "SELECT 1",
                                        "result": {"row_count": 9, "rows": [["hush"]]}}),
                 "sql": "SELECT 1", "verdict": "allow"})
    rows.append({"role": "user", "kind": "text", "content": "当前问题", "sql": None, "verdict": None})
    rows.append({"role": "assistant", "kind": "text", "content": "当前回答内容未压缩", "sql": None, "verdict": None})
    return rows


async def test_mechanical_compress_keeps_cards_and_current_round(app_state, conn_id, monkeypatch):
    """机械压缩：旧叙事 [压缩]，卡片骨架与当前轮不动；压回阈值内 → 零模型调用。"""
    import app.ai.compress as comp

    def boom(*a, **k):
        raise AssertionError("机械路径不应调用 LLM")

    monkeypatch.setattr("app.ai.gateway.build_provider", boom)
    monkeypatch.setattr("app.ai.gateway.is_effective_mock", boom)

    out = await comp.compress_hist(app_state, conn_id, _big_history(150))
    assert out["stats"]["mechanical_compressed"] >= 100
    assert out["stats"].get("llm_summarized", 0) == 0
    cards = [m for m in out["rows"] if m.get("kind") == "sql_card"]
    assert json.loads(cards[0]["content"])["result"]["rows"] == [["hush"]], "卡片内容不得被压缩"
    assert out["rows"][-2]["content"] == "当前问题" and out["rows"][-1]["content"] == "当前回答内容未压缩"


async def test_llm_fallback_writes_egress_audit_before_model(app_state, conn_id, monkeypatch):
    """机械压不完 → LLM 摘要兜底：模型调用前先写 egress 审计（source=egress-compress）。"""
    import app.ai.compress as comp
    from app.ai.gateway import ChatResponse, LLMGateway

    app_state.runtime.update({"privacy_mode": "standard"})
    order = []

    async def _mock_chat(cls, messages, tools=None):
        order.append("chat")
        return ChatResponse(content="历史要点：订单、客户、库存三块", usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr("app.ai.gateway.MockProvider.chat", classmethod(_mock_chat))

    def fake_build(*a, **k):
        order.append("build")
        return LLMGateway({"provider": "mock", "model": "mock"})

    monkeypatch.setattr("app.ai.gateway.build_provider", fake_build)
    monkeypatch.setattr("app.ai.gateway.is_effective_mock", lambda cfg: False)

    out = await comp.compress_hist(app_state, conn_id, _big_history(400))
    assert order == ["build", "chat"], f"应恰好一次构建+调用模型，got {order}"
    assert out["stats"].get("llm_summarized", 0) > 0
    entries = [e for e in app_state.audit.list() if e.get("status") == "egress-compress"]
    assert entries, "LLM 摘要必须记 egress 审计"
    assert any("[早期对话 LLM 摘要]" in (m.get("content") or "") for m in out["rows"])
    assert any(m.get("kind") == "sql_card" for m in out["rows"])


async def test_stream_compresses_large_server_history(app_state, conn_id, monkeypatch):
    """装配：session 历史超大时 stream() 先压缩再喂模型。"""
    import app.ai.loop as loop

    sid = app_state.chats.upsert(None, conn_id, None)
    app_state.chats.append_messages(sid, _big_history(150))

    recorder = {"messages": None}

    class _RecProv:
        async def chat_stream(self, messages, tools, ctx=None):
            recorder["messages"] = messages
            if False:
                yield

    from app.ai import gateway as _gwl
    monkeypatch.setattr(_gwl, "build_provider", lambda *a, **k: _RecProv())
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": "那按周呢"}], provider="mock")
    [ev async for ev in loop.stream(app_state, req)]
    stats = getattr(req, "_compress_stats", None)
    assert stats and stats.get("mechanical_compressed", 0) >= 100, f"应压缩，got {stats}"
    joined = " ".join(m.get("content") or "" for m in (recorder["messages"] or []))
    assert "[压缩]" in joined and "rKEEP" in joined and "x" * 400 not in joined
