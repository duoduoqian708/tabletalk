"""版本制知识库：stash（放弃回滚）/ 历史归档（字段回溯）/ 版本号 / 重建隔离。"""
from __future__ import annotations

from app.knowledge.store import KnowledgeBase


def _schema() -> dict:
    from tests.knowledge.test_discard import _schema as s

    return s()


async def test_first_round_confirm_version_1(app_state):
    """首轮构建+确认：版本号=1，无归档（当前内容为空，过滤不入档）。"""
    st = app_state
    conn = "v-first"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "状态v1", "values": "P=待付款"}])
    r = await kb.confirm_all(conn)
    assert kb.current_version(conn) == 1
    assert r["archived"] == 0


async def test_rebuild_keeps_current_discard_clears_proposals(app_state):
    """重建（2026-09 修订）：当前生效知识保留，放弃 = 清提案（无需回滚）。"""
    st = app_state
    conn = "v-discard"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "状态v1", "values": "P=待付款"}])
    kb.upsert_tags(conn, [{"name": "交易域"}])
    kb.confirm_tag(conn, "交易域")
    kb.assign_table_tags(conn, "orders", ["交易域"])
    await kb.confirm_all(conn)
    # 重建：当前注释/标签/已确认内容保留；AI 产新提案
    await kb.build(conn, _schema())
    ci = kb._tables[conn]["orders"].columns["status"]
    assert ci.status == "confirmed" and ci.comment == "状态v1" and ci.values == "P=待付款"
    assert any(t["name"] == "交易域" for t in kb.tags(conn)["library"])
    # 放弃：清提案，当前不动
    counts = await kb.discard_drafts(conn)
    ci2 = kb._tables[conn]["orders"].columns["status"]
    assert ci2.status == "confirmed" and ci2.comment == "状态v1" and ci2.values == "P=待付款"
    assert not ci2.has_proposal
    assert not kb.llm_graph_edges(conn)
    assert kb.current_version(conn) == 1
    assert kb.field_history(conn, "orders", "status") == []


async def test_rebuild_confirm_archives_and_bumps(app_state):
    """重建 -> 确认：旧版本归档（字段级）、版本+1。"""
    st = app_state
    conn = "v-archive"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "状态v1", "values": "P=待付款"}])
    await kb.confirm_all(conn)
    await kb.build(conn, _schema())
    # 确认新提案：应用提案前归档 v1 当前值
    r = await kb.confirm_all(conn)
    assert r["archived"] > 0
    assert kb.current_version(conn) == 2
    hist = kb.field_history(conn, "orders", "status")
    assert len(hist) == 1
    assert hist[0]["comment"] == "状态v1"
    assert hist[0]["values"] == "P=待付款"
    assert hist[0]["version"] == 1


async def test_rebuild_keeps_confirmed_no_overlap(app_state):
    """重建隔离（2026-09 修订）：当前已确认注释保留，新提案并行，不覆盖。"""
    st = app_state
    conn = "v-iso"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "旧注释"}])
    kb.upsert_tags(conn, [{"name": "旧标签"}])
    kb.confirm_tag(conn, "旧标签")
    await kb.confirm_all(conn)
    await kb.build(conn, _schema())
    ci = kb._tables[conn]["orders"].columns["status"]
    assert ci.status == "confirmed" and ci.comment == "旧注释"
    assert any(t["name"] == "旧标签" for t in kb.tags(conn)["library"])
    # 新提案与当前共存（不覆盖当前注释）
    assert ci.proposed_comment and ci.proposed_comment != "旧注释"


async def test_archive_trim_keeps_3_batches(app_state):
    """N=3：累计确认 4 个版本后，字段历史只保留最近 3 批（物理删除）。"""
    st = app_state
    conn = "v-trim"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "v1"}])
    await kb.confirm_all(conn)
    for i in range(2, 5):
        await kb.build(conn, _schema(), enable_ai_annotation=False)
        kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": f"v{i}"}])
        await kb.confirm_all(conn)
    hist = kb.field_history(conn, "orders", "status")
    assert len(hist) == 3
    assert [h["version"] for h in hist] == [3, 2, 1]


async def test_apply_field_history_overwrites_current(app_state):
    """字段回溯：用历史版本覆盖当前字段（comment/values/example）。"""
    st = app_state
    conn = "v-apply"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "v1", "values": "P=待付款"}])
    await kb.confirm_all(conn)
    await kb.build(conn, _schema())
    await kb.confirm_all(conn)
    hist = kb.field_history(conn, "orders", "status")
    assert kb.apply_field_history(conn, "orders", "status", hist[0]["id"]) is True
    ci = kb._tables[conn]["orders"].columns["status"]
    assert ci.comment == "v1"
    assert ci.values == "P=待付款"
    assert kb.apply_field_history(conn, "orders", "status", 999999) is False


