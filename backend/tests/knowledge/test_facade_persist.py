"""P1-3/P1-6：facade 快照落盘含 auto 文档；clear 清理 auto 后 is_built 不再恒真。"""

from __future__ import annotations

from app.knowledge.docs import KnowledgeDoc


def _auto_doc(conn_id: str) -> KnowledgeDoc:
    return KnowledgeDoc(
        id="a1", conn_id=conn_id, kind="column", title="orders.status",
        body="订单状态（自动标注）", table="orders", column="status",
        source="auto", status="draft",
    )


async def test_save_conn_persists_auto_docs(app_state, conn_id):
    """P1-3：_save_conn 快照包含 auto 文档，_load_conn 后仍在（重启不丢 AI 文档）。"""
    kb = app_state.knowledge
    kb._auto[conn_id] = [_auto_doc(conn_id)]
    kb._save_conn(conn_id)
    # 模拟重启：清内存后从存储重载
    kb._auto.pop(conn_id, None)
    kb.semantic_store._tables.pop(conn_id, None)
    kb.semantic_store._user.pop(conn_id, None)
    kb._load_conn(conn_id)
    docs = kb._auto.get(conn_id, [])
    assert docs and docs[0].title == "orders.status"
    assert docs[0].status == "draft"


async def test_clear_removes_auto(app_state, conn_id):
    """P1-6：clear 清 _auto，is_built 不再因残留 AI 文档恒真。"""
    kb = app_state.knowledge
    kb._auto[conn_id] = [_auto_doc(conn_id)]
    kb.semantic_store._tables[conn_id] = {}
    kb.clear(conn_id)
    assert conn_id not in kb._auto