"""WS5 T5.1-T5.6 验收。"""
import pytest

from app.ai.context import assemble_context_full
from app.ai.dto import ChatRequest
from app.ai.loop import chat_stream
from app.ai.tools.sql import _run_query
from app.core.questions import QuestionStore


async def test_t51_capped_20_and_priority(app_state, conn_id, monkeypatch):
    # 构造大候选：通过 patch knowledge 的路由方法返回大量表
    from app.knowledge.store import KnowledgeBase
    # 准备 30 张虚拟表名
    all_tables = [f"t{i}" for i in range(30)]
    # mock route_tables 返回前 10 个 tag 表，vector 10 个，expand 返回 30
    def fake_route(conn, tags, hops=2):
        return {"tables": all_tables[:10]}
    async def fake_vec(conn, q, top_k=6):
        return [(f"t{i}", 0.9) for i in range(10, 20)]
    def fake_expand(conn, seeds, hops=2):
        return all_tables

    monkeypatch.setattr(app_state.knowledge, "route_tables", fake_route)
    monkeypatch.setattr(app_state.knowledge, "vector_route_tables", fake_vec)
    monkeypatch.setattr(app_state.knowledge, "expand_tables", fake_expand)
    # 需要有已确认标签以触发 tag_tables
    # 通过直接给 tags 参数避免 classify_tags
    text, meta = await assemble_context_full(app_state, conn_id, query="test", tags=["dummy"], followup_tables=None)
    assert len(meta["candidate_tables"]) == 20
    assert meta["capped"] is True
    # 优先级：前10应为 tag 表
    assert set(meta["candidate_tables"][:10]) == set(all_tables[:10])
    # 不封顶时 capped False
    def fake_expand_small(conn, seeds, hops=2):
        return all_tables[:5]
    monkeypatch.setattr(app_state.knowledge, "expand_tables", fake_expand_small)
    text2, meta2 = await assemble_context_full(app_state, conn_id, query="test", tags=["dummy"])
    assert meta2["capped"] is False
    assert len(meta2["candidate_tables"]) == 5


async def test_t52_bounded_correction_injects_tables(app_state, conn_id):
    # 用不存在的表触发纠错
    res = await _run_query(app_state, {"sql": "SELECT * FROM not_exist_table_xyz"}, conn_id, include_data=False)
    # 工具应返回 available_tables 而不是直接抛异常
    assert res.result.get("available_tables") is not None
    assert isinstance(res.result["available_tables"], list)
    assert len(res.result["available_tables"]) > 0
    assert "hint" in res.result or res.card.get("reason")


async def test_t53_raw_match_before_redact(app_state, conn_id):
    # 保存一条含手机号的问题
    store = app_state.questions
    # 清理旧数据
    for e in store.list(conn_id):
        store.delete(conn_id, e["id"])
    q_raw = "查一下手机号 13800138000 的订单"
    sql = "SELECT * FROM orders WHERE phone='13800138000' LIMIT 10"
    entry = store.save(conn_id, q_raw, sql, tables=["orders"])
    # 用同一手机号提问，脱敏前应命中，脱敏后原文含 token 会不命中；我们已移到脱敏前，所以应命中
    matched = store.match(conn_id, q_raw)
    assert matched is not None
    assert matched["id"] == entry["id"]
    # 清理
    store.delete(conn_id, entry["id"])


async def test_t54_hit_is_confirm_card_not_execute(app_state, conn_id):
    store = app_state.questions
    for e in store.list(conn_id):
        store.delete(conn_id, e["id"])
    q = "查一下订单总数确认卡测试"
    sql = "SELECT COUNT(*) FROM orders"
    entry = store.save(conn_id, q, sql, tables=["orders"])
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": q}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    # 应命中且产生 question_library 卡，但不附 result（需确认）
    cards = [ev for ev in events if ev["type"] == "sql_card"]
    assert len(cards) == 1
    card = cards[0]["card"]
    assert card.get("question_library") is True
    assert card.get("needs_confirm") is True
    assert "result" not in card or card.get("result") is None
    assert card.get("sql") == sql
    # 应有 provider local manifest
    manifests = [ev for ev in events if ev["type"] == "manifest"]
    assert manifests[0]["manifest"]["provider"] == "local"
    # 清理
    store.delete(conn_id, entry["id"])


async def test_t55_save_only_allow(app_state, conn_id):
    store = app_state.questions
    # 只读应成功
    e1 = store.save(conn_id, "查订单", "SELECT * FROM orders LIMIT 10")
    assert e1["sql"].startswith("SELECT")
    store.delete(conn_id, e1["id"])
    # DML 应被拒
    with pytest.raises(ValueError, match="仅收只读"):
        store.save(conn_id, "删订单", "DELETE FROM orders WHERE id=1")


async def test_t56_threshold_configurable(app_state, conn_id, monkeypatch):
    store = app_state.questions
    for e in store.list(conn_id):
        store.delete(conn_id, e["id"])
    # 保存一条
    q = "查一下月度退款率统计分析"
    sql = "SELECT * FROM orders LIMIT 10"
    entry = store.save(conn_id, q, sql, tables=["orders"])
    # 默认阈值 60 时，相似但不完全相同的短句应命中（score 80 或 60）
    # 构造一个相似度 60 的对手：首6字符相同
    similar = q[:6] + "XYZ"
    # 默认阈值下应命中（score 60）
    assert store.match(conn_id, similar) is not None
    # 提高阈值到 80，60分的应不命中
    monkeypatch.setenv("TABLETALK_QUESTION_THRESHOLD", "80")
    # 需让 _threshold 重新读取 env：直接调用时已读 env，无需重启
    assert store.match(conn_id, similar) is None
    # 完全相等仍命中 100
    assert store.match(conn_id, q) is not None
    monkeypatch.delenv("TABLETALK_QUESTION_THRESHOLD", raising=False)
    store.delete(conn_id, entry["id"])


async def test_t53_t54_integration_via_loop(app_state, conn_id):
    # 综合：含敏感串的问题在 loop 中应走确认卡（原文匹配）而非直接执行
    store = app_state.questions
    for e in store.list(conn_id):
        store.delete(conn_id, e["id"])
    q = "13800138000 订单查询"
    sql = "SELECT * FROM orders WHERE phone='13800138000' LIMIT 5"
    entry = store.save(conn_id, q, sql, tables=["orders"])
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": q}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    # 应为确认卡
    card = next(ev["card"] for ev in events if ev["type"] == "sql_card")
    assert card["question_library"] is True
    store.delete(conn_id, entry["id"])
