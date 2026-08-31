"""R15/B4 kb_write 双存储统一：改走 state.knowledge 门面，文档/标签写入统一知识库存储。

旧 `app/ai/knowledge/store.py` 直写同一 knowledge-{conn}.db（docs 表 schema 冲突），
改造后 kb_write 与知识库构建共享一份存储（同一 id 体系：usr- 前缀用户文档）。
"""
from __future__ import annotations

from app.ai.tools.kb_write import _kb_write


async def test_kb_write_doc_visible_in_kb(app_state, conn_id):
    """create → 文档出现在 state.knowledge.list_docs（usr- 前缀），与构建产物同存储。"""
    out = await _kb_write(app_state, {
        "action": "create", "target": "doc",
        "table_name": "orders", "content": "订单主表，含状态与金额",
    }, conn_id)
    assert out.result["ok"] is True, out.result
    doc = out.result["doc"]
    assert doc["id"].startswith("usr-")
    docs = app_state.knowledge.list_docs(conn_id, table="orders")
    assert any(d.id == doc["id"] and d.body == "订单主表，含状态与金额" for d in docs)


async def test_kb_write_reject_removes_doc(app_state, conn_id):
    """reject → 文档从知识库移除。"""
    out = await _kb_write(app_state, {
        "action": "create", "target": "doc",
        "table_name": "orders", "content": "待删文档",
    }, conn_id)
    doc_id = out.result["doc"]["id"]
    r = await _kb_write(app_state, {"action": "reject", "target": "doc", "doc_id": doc_id}, conn_id)
    assert r.result["ok"] is True
    assert not any(d.id == doc_id for d in app_state.knowledge.list_docs(conn_id, table="orders"))


async def test_kb_write_tag_flow(app_state, conn_id):
    """create tag（含 tables 绑定）→ confirm → confirmed_tags 可见（同统一标签存储）。"""
    out = await _kb_write(app_state, {
        "action": "create", "target": "tag",
        "name": "订单域", "tables": ["orders", "order_items"],
    }, conn_id)
    assert out.result["ok"] is True, out.result
    tags = app_state.knowledge.tags(conn_id)
    assert any(t["name"] == "订单域" for t in tags["library"])
    # 确认 → 参与路由
    r = await _kb_write(app_state, {"action": "confirm", "target": "tag", "name": "订单域"}, conn_id)
    assert r.result["ok"] is True
    assert "订单域" in app_state.knowledge.confirmed_tags(conn_id)
    assert "orders" in app_state.knowledge.route_tables(conn_id, ["订单域"], hops=2)["tables"]
    # reject → 移除
    r2 = await _kb_write(app_state, {"action": "reject", "target": "tag", "name": "订单域"}, conn_id)
    assert r2.result["ok"] is True
    assert not any(t["name"] == "订单域" for t in app_state.knowledge.tags(conn_id)["library"])

async def test_kb_write_confirm_draft_annotation(app_state, conn_id):
    """P2-10/§15.3：kb_write confirm 支持确认表/列级 AI 注释草案（此前无确认路径）。"""
    from app.core.schema import get_schema

    await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                    enable_ai_annotation=False)
    # 手工放一条 draft 注释（模拟 AI 草案）
    from app.knowledge.store import TableKnowledge
    tk = TableKnowledge(name="orders")
    tk.comment = "订单主表（草案）"
    tk.status = "draft"
    app_state.knowledge.semantic_store._tables.setdefault(conn_id, {})["orders"] = tk
    # 确认草案
    r = await _kb_write(app_state, {"action": "confirm", "target": "doc",
                                    "table_name": "orders"}, conn_id)
    assert r.result["ok"] is True, r.result
    assert app_state.knowledge.semantic_store._tables[conn_id]["orders"].status == "confirmed"
    # 再次确认 → 无待确认草案
    r2 = await _kb_write(app_state, {"action": "confirm", "target": "doc",
                                     "table_name": "orders"}, conn_id)
    assert r2.result["ok"] is False
