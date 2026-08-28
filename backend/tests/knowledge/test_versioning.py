"""版本制知识库：stash（放弃回滚）/ 历史归档（字段回溯）/ 版本号 / 重建隔离。"""
from __future__ import annotations

from app.knowledge.store import KnowledgeBase


def _schema() -> dict:
    from tests.knowledge.test_discard import _schema as s

    return s()


async def test_first_round_confirm_version_1(app_state):
    """首轮构建+确认：版本号=1，无归档，无 stash 残留。"""
    st = app_state
    conn = "v-first"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "状态v1", "values": "P=待付款"}])
    r = await kb.confirm_all(conn)
    assert kb.current_version(conn) == 1
    assert r["archived"] == 0
    assert kb._stash.get(conn) is None
    assert not kb._stash_persisted(conn)


async def test_rebuild_discard_restores_prev_version(app_state):
    """重建 → 放弃：当前版本原样恢复（注释/标签），版本号不变，不入历史。"""
    st = app_state
    conn = "v-discard"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "状态v1", "values": "P=待付款"}])
    kb.upsert_tags(conn, [{"name": "交易域"}])
    kb.assign_table_tags(conn, "orders", ["交易域"])
    await kb.confirm_all(conn)
    # 重建：草稿全新（注释/标签重新生成，无旧共存）
    await kb.build(conn, _schema())
    assert kb.tags(conn)["library"], "重建应产生全新 draft 标签"
    assert kb._tables[conn]["orders"].columns["status"].status != "confirmed"
    # 放弃 → 恢复 v1
    await kb.discard_drafts(conn)
    ci = kb._tables[conn]["orders"].columns["status"]
    assert ci.status == "confirmed" and ci.comment == "状态v1" and ci.values == "P=待付款"
    tags = kb.tags(conn)["library"]
    assert any(t["name"] == "交易域" and t["status"] == "confirmed" for t in tags)
    assert kb.current_version(conn) == 1
    assert kb.field_history(conn, "orders", "status") == []


async def test_rebuild_confirm_archives_and_bumps(app_state):
    """重建 → 确认：旧版本归档（字段级）、版本+1、stash 清除。"""
    st = app_state
    conn = "v-archive"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "状态v1", "values": "P=待付款"}])
    await kb.confirm_all(conn)
    await kb.build(conn, _schema())
    r = await kb.confirm_all(conn)
    assert r["archived"] > 0
    assert kb.current_version(conn) == 2
    assert not kb._stash_persisted(conn)
    hist = kb.field_history(conn, "orders", "status")
    assert len(hist) == 1
    assert hist[0]["comment"] == "状态v1"
    assert hist[0]["values"] == "P=待付款"
    assert hist[0]["version"] == 1


async def test_rebuild_isolation_no_overlap(app_state):
    """重建隔离：标签/注释全新生成，无新旧共存；放弃恢复旧版本。"""
    st = app_state
    conn = "v-iso"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "旧注释"}])
    kb.upsert_tags(conn, [{"name": "旧标签"}])
    kb.assign_table_tags(conn, "orders", ["旧标签"])
    await kb.confirm_all(conn)
    await kb.build(conn, _schema())
    ci = kb._tables[conn]["orders"].columns["status"]
    assert ci.status != "confirmed"
    assert not any(t["name"] == "旧标签" for t in kb.tags(conn)["library"])
    await kb.discard_drafts(conn)
    assert kb._tables[conn]["orders"].columns["status"].comment == "旧注释"


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


async def test_pending_refresh_keeps_stash(app_state, tmp_path):
    """pending 期间模拟重启：stash 已落盘，新实例放弃仍可完整回滚。"""
    st = app_state
    conn = "v-refresh"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    kb.annotate_drafts(conn, [{"table": "orders", "column": "status", "comment": "v1"}])
    await kb.confirm_all(conn)
    await kb.build(conn, _schema())
    assert kb._stash_persisted(conn)
    kb2 = KnowledgeBase(tmp_path / "data")
    kb2.ensure_loaded(conn)
    assert kb2._stash_persisted(conn)
    await kb2.discard_drafts(conn)
    assert kb2._tables[conn]["orders"].columns["status"].comment == "v1"