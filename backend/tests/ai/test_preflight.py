"""Preflight 准备层测试（2026-09 改版）：意图识别已移出 preflight（归 decompose LLM）。

本层只测 plan 级字段（追问轮/skip_retrieval/tags）、脱敏/清单准备、以及
意图回填后的集成（chat_stream 事件/度量）。关键词/LLM 意图断言全部移除。
"""
from __future__ import annotations

from app.ai.preflight import PreflightResult, preflight
from app.ai.dto import ChatRequest
from app.ai.loop import chat_stream


async def test_preflight_no_intent_only_plan_fields(app_state, conn_id):
    """2026-09：preflight 不再产出意图（intent 为空）；tags/skip_retrieval/脱敏/清单就位。"""
    res = await preflight(app_state, conn_id, "查一下订单总数", history_tail=None)
    assert res.intent == ""                     # 意图归 decompose
    assert res.redacted_q == "查一下订单总数"    # 脱敏原文透传
    assert isinstance(res.manifest, dict)


async def test_preflight_skip_retrieval_structure_question(app_state, conn_id):
    """结构问答/审计回顾 → skip_retrieval（跳过向量检索管线；非意图分类）。"""
    for q in ("有哪些表", "审计里刚才被拦了什么", "看看刚才的操作记录"):
        res = await preflight(app_state, conn_id, q, history_tail=None)
        assert res.skip_retrieval is True, q
    res2 = await preflight(app_state, conn_id, "查一下订单总数", history_tail=None)
    assert res2.skip_retrieval is False


async def test_preflight_followup_seeds(app_state, conn_id):
    """追问轮：上轮有 orders 表，当前短句追问 → followup + 种子表。"""
    history = [
        {"role": "user", "content": "查一下订单总数"},
        {"role": "assistant", "content": "已查询", "tables": ["orders"]},
    ]
    res = await preflight(app_state, conn_id, "那按周统计呢", history_tail=history)
    assert res.is_followup is True
    assert "orders" in res.followup_tables


async def test_preflight_followup_not_trigger_when_has_domain(app_state, conn_id):
    """含明确领域动作的句不误判为 followup。"""
    history = [
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": "ok", "tables": ["orders"]},
    ]
    res = await preflight(app_state, conn_id, "查一下产品销量", history_tail=history)
    assert res.is_followup is False


async def test_preflight_tags_matches_confirmed(app_state, conn_id):
    """tags 检索种子：只匹配已确认标签。"""
    kb = app_state.knowledge
    kb.create_tag(conn_id, "订单域", "", "")
    kb.confirm_tag(conn_id, "订单域")
    # 未确认标签不应进 tags
    kb.upsert_tags(conn_id, [{"name": "草稿域", "description": ""}])
    res = await preflight(app_state, conn_id, "查一下订单域的订单总数", history_tail=None)
    assert "订单域" in res.tags
    assert "草稿域" not in res.tags


async def test_dispatcher_disabled_skill_degraded(app_state):
    """skill 关闭时的能力降级（resolve_skill_from_intent，意图源不依赖 preflight）。"""
    from app.ai.agent.dispatcher import resolve_skill_from_intent
    from app.ai.skills.registry import get_skill, update_skill

    skill = get_skill("report")
    if skill is None:
        import pytest
        pytest.skip("report skill not registered")
    prev = skill.enabled
    try:
        update_skill("report", {"enabled": False})
        # 意图字符串直接驱动（preflight 已不再产意图）
        skill_id, degraded, msg = resolve_skill_from_intent("report")
        assert degraded is True
        assert msg and "已关闭" in msg
        assert skill_id == "report"
    finally:
        update_skill("report", {"enabled": prev})


async def test_intent_py_no_inline_audit():
    import pathlib
    p = pathlib.Path("app/ai/intent.py")
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if not text:
        import inspect
        text = inspect.getsource(__import__("app.ai.intent", fromlist=["*"]))
    assert "build_manifest" not in text
    assert "audit.log" not in text
    assert "redact_text" not in text


async def test_preflight_coverage_and_mismatch_integration(app_state, conn_id):
    """chat_stream 集成：意图回填后 coverage / intent_mismatch 仍产出。"""
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "查一下订单总数"}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    done = next((ev for ev in events if ev["type"] == "done"), None)
    assert done is not None
    meta = done.get("context_meta") or {}
    assert "coverage" in meta
    assert "intent_mismatch" in meta
    assert isinstance(meta["coverage"], float)
    assert isinstance(meta["intent_mismatch"], bool)
