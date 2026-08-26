"""WS3 T3.4 验收：上下文压缩 v1（机械优先，LLM 兜底走单管道）。

D7 原则断言：
- 卡片骨架（result_id）压缩后原样保留，原始卡（含 result.rows）不被压缩/拼接。
- 当前轮（尾部最近一次 user 及其响应）不动。
- 机械路径零模型调用（resolve_provider_cfg / build_provider 一律不许碰）。
- LLM 兜底摘要是一次出网：必须先写 egress 审计（source=egress-compress）再调模型。
"""
from __future__ import annotations

import json

import pytest

from app.ai.compress import (
    estimate_tokens,
    llm_compress_oldest,
    mechanical_compress,
    rows_tokens,
)
import app.ai.gateway as gw_mod


def _big_history(n_old: int = 150, big: int = 400):
    """n 条冗长叙事 + at 张卡 + 尾部当前轮（user+answer）。"""
    rows = []
    for i in range(n_old):
        rows.append({"role": "assistant", "kind": "text",
                     "content": f"叙事行 {i}: " + "x" * big, "sql": None, "verdict": None})
    rows.append({"role": "assistant", "kind": "sql_card",
                 "content": json.dumps({"result_id": "rKEEP", "verdict": "allow", "sql": "SELECT 1",
                                        "result": {"row_count": 9, "rows": [["hush"]]}}),
                 "sql": "SELECT 1", "verdict": "allow"})
    rows.append({"role": "user", "kind": "text", "content": "当前问题", "sql": None, "verdict": None})
    rows.append({"role": "assistant", "kind": "text", "content": "当前回答内容未压缩", "sql": None, "verdict": None})
    return rows


def test_estimate_and_rows_tokens():
    assert estimate_tokens("中文") == 3          # CJK 每字 1.5
    assert estimate_tokens("abcd") == 1          # ASCII 每字 0.35 → 1.4 → 1
    rows = [{"role": "assistant", "kind": "text", "content": "中文", "sql": "SELECT 1"},
            {"role": "user", "kind": "text", "content": "a" * 100, "sql": None}]
    assert rows_tokens(rows) == 3 + 2 + 35      # 中文3 + SELECT 1的2 + 100×ASCII 0.35=35

def test_mechanical_below_threshold_is_noop():
    rows = [{"role": "user", "kind": "text", "content": "你好"}]
    r = mechanical_compress(rows)
    assert r["over"] is False and r["compressed"] == 0
    assert r["rows"] is rows  # 原位，不复制


def test_mechanical_compresses_old_narrative_keeps_cards_and_current_round():
    rows = _big_history(150)
    r = mechanical_compress(rows)
    assert r["compressed"] >= 100, "旧叙事应被大量压缩"
    assert r["tokens_after"] < r["tokens_before"]
    assert r["over"] is False, "机械应把 15k+ 叙事压到阈值内（无需 LLM）"
    # 卡片骨架保留：原始 JSON（含 result_id 与原始嵌套）原样
    cards = [m for m in r["rows"] if m.get("kind") == "sql_card"]
    assert len(cards) == 1
    parsed = json.loads(cards[0]["content"])
    assert parsed["result_id"] == "rKEEP"
    assert parsed["result"]["rows"] == [["hush"]], "卡片内容不得被压缩/拼接"
    # 当前轮 user+answer 未动
    tail = r["rows"][-2:]
    assert tail[0]["content"] == "当前问题"
    assert tail[1]["content"] == "当前回答内容未压缩"
    # 旧叙事被 [压缩] 标记
    old_texts = [m["content"] for m in r["rows"] if m.get("kind") == "text"
                 and m.get("content", "").startswith("[压缩]")]
    assert len(old_texts) >= 100 and all(t.startswith("[压缩]") for t in old_texts)


def test_mechanical_drops_think_metadata():
    rows = [{"role": "assistant", "kind": "think", "content": "思考过程，不展示"},
            {"role": "assistant", "kind": "text", "content": "x" * 400, "sql": None, "verdict": None},
            {"role": "user", "kind": "text", "content": "q"}]
    r = mechanical_compress(rows, threshold=1)
    kinds = [m.get("kind") for m in r["rows"]]
    assert "think" not in kinds, "think 是元数据，压缩时丢弃"


async def test_compress_hist_mechanical_path_makes_zero_model_calls(app_state, conn_id, monkeypatch):
    """机械把超大叙事压回阈值内 → LLM 兜底绝不触发，全程零模型调用。"""
    import app.ai.compress as comp

    def boom(*a, **k):
        raise AssertionError("机械路径不应调用 LLM")

    monkeypatch.setattr(gw_mod, "build_provider", boom)
    monkeypatch.setattr(gw_mod, "is_effective_mock", boom)
    out = await comp.compress_hist(app_state, conn_id, _big_history(150))
    assert out["stats"]["mechanical_compressed"] >= 100
    assert out["stats"].get("llm_summarized", 0) == 0
    assert any(m.get("kind") == "sql_card" for m in out["rows"])


async def test_llm_fallback_writes_egress_audit_before_model(app_state, conn_id, monkeypatch):
    """机械压不完（海量叙事）→ LLM 摘要：经真实 gateway（mock）驱动，中央拦截器在模型调用前记
    egress（source=egress-compress），再调模型。"""
    import app.ai.compress as comp
    import app.ai.gateway as gw_mod
    from app.ai.gateway import ChatResponse, LLMGateway

    app_state.runtime.update({"privacy_mode": "standard"})
    order = []

    # 补丁 MockProvider.chat 返回确定性摘要，但 gateway 仍走真实拦截器（egress 先于返回写）
    async def _mock_chat(cls, messages, tools=None):
        order.append("chat")
        return ChatResponse(content="历史要点：订单、客户、库存三块", usage={"prompt_tokens": 1, "completion_tokens": 1})
    monkeypatch.setattr(gw_mod.MockProvider, "chat", classmethod(_mock_chat))

    def fake_build(*a, **k):
        order.append("build")
        return LLMGateway({"provider": "mock", "model": "mock"})

    monkeypatch.setattr(gw_mod, "build_provider", fake_build)
    monkeypatch.setattr(gw_mod, "is_effective_mock", lambda cfg: False)

    # 400 条叙事 → 即便 each 被压成一句 (~44 token) 也超阈值 → 走 LLM 兜底
    out = await comp.compress_hist(app_state, conn_id, _big_history(400))
    assert order == ["build", "chat"], f"应恰好一次构建+调用模型，got {order}"
    assert out["stats"].get("llm_summarized", 0) > 0
    # egress 审计先于模型调用落库（source=egress-compress）—— 由中央拦截器写
    entries = [e for e in app_state.audit.list() if e.get("status") == "egress-compress"]
    assert entries, "LLM 摘要必须记 egress 审计"
    assert entries[0]["verdict"] == "egress"
    # LLM 摘要 marker 进上下文，且卡片骨架仍在
    assert any("[早期对话 LLM 摘要]" in (m.get("content") or "") for m in out["rows"])
    assert any(m.get("kind") == "sql_card" for m in out["rows"])


async def test_llm_fallback_degrades_to_mech_on_mock(app_state, conn_id, monkeypatch):
    """mock 提供器不回退摘要（离线保护）：维持机械结果，over=True，degraded=True，不调模型。"""
    import app.ai.compress as comp

    monkeypatch.setattr(gw_mod, "is_effective_mock", lambda cfg: True)

    def boom(*a, **k):
        raise AssertionError("mock 不应调真实 provider")

    monkeypatch.setattr(gw_mod, "build_provider", boom)
    out = await comp.compress_hist(app_state, conn_id, _big_history(400))
    assert out["stats"].get("degraded") is True
    assert out["stats"].get("llm_summarized", 0) == 0


async def test_stream_wires_compression_of_large_server_history(app_state, conn_id, monkeypatch):
    """T3.4 装配：session 历史超大时，stream() 先压缩再喂模型（卡片骨架保留、美叙事变 [压缩]）。"""
    import app.ai.loop as loop
    from app.ai.dto import ChatRequest

    sid = app_state.chats.upsert(None, conn_id, None)
    app_state.chats.append_messages(sid, _big_history(150))

    recorder = {"messages": None}
    class _RecProv:
        async def chat_stream(self, messages, tools, ctx=None):
            recorder["messages"] = messages
            if False:
                yield

    monkeypatch.setattr(loop.gw, "build_provider", lambda *a, **k: _RecProv())
    req = ChatRequest(connection_id=conn_id, session_id=sid,
                      messages=[{"role": "user", "content": "那按周呢"}], provider="mock")
    [ev async for ev in loop.stream(app_state, req)]
    stats = getattr(req, "_compress_stats", None)
    assert stats and stats.get("mechanical_compressed", 0) >= 100, f"应压缩，got {stats}"
    contents = [m.get("content") or "" for m in (recorder["messages"] or [])]
    joined = " ".join(contents)
    assert "[压缩]" in joined, "被压缩的旧叙事应进入上下文"
    assert "result_id=rKEEP" in joined or "rKEEP" in joined, "卡片骨架应保留"
    assert "x" * 400 not in joined, "未压缩的冗长叙事不得整体进入上下文"
    assert any("那按周呢" in c for c in contents), "当前追问应在"