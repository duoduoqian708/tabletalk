"""枚举消费：confirmed 枚举并入列注释文档（auto-col-*），向量同步重嵌。

- confirm 后 body 追加「。取值：V=M、…」，该文档向量按新 body 重算；
- draft 不进文档（确认前 body 无取值对照）;
- reject 后 body 恢复、向量回滚；
- 全量重建（build）时历史 confirmed 枚举自动并入新生成的列文档。
"""
from __future__ import annotations

import asyncio

from app.knowledge.store import KnowledgeBase


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "orders", "name": "status", "type": "varchar", "pk": False, "fk": False, "comment": "订单状态"},
        ],
        "foreign_keys": [],
    }


def _items() -> list[dict]:
    return [{
        "table": "orders", "column": "status",
        "entries": [
            {"value": "P", "meaning": "待付款"},
            {"value": "S", "meaning": "已发货"},
            {"value": "R", "meaning": "已退货"},
        ],
    }]


def _col_doc(kb: KnowledgeBase, conn_id: str):
    return next(d for d in kb._auto[conn_id] if d.id == "auto-col-orders-status")


async def _assert_vec_matches(kb: KnowledgeBase, conn_id: str, doc) -> None:
    vec = kb._vec[conn_id].get(doc.id)
    assert vec and len(vec) > 1  # 非占位向量 [0.0]
    expected = await kb._emb.embed(kb._doc_text(doc))
    assert all(abs(a - b) < 1e-9 for a, b in zip(vec, expected))


async def test_confirmed_enum_appends_to_column_doc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    assert kb.annotate_enums("c1", _items()) == 3
    assert kb.confirm_enum("c1", "orders", "status") == 3
    await asyncio.sleep(0)  # 调度的单文档重嵌完成（哈希嵌入无真实挂起点，一步即毕）

    doc = _col_doc(kb, "c1")
    assert doc.body.startswith("orders.status 列，类型 varchar")
    assert "。列注释：订单状态" in doc.body
    assert doc.body.endswith("。取值：P=待付款、S=已发货、R=已退货")
    await _assert_vec_matches(kb, "c1", doc)

    # 检索上下文天然携带枚举语义
    ctx = await kb.to_context("c1", query="查已退货订单")
    assert "R=已退货" in ctx

    # 全量重建：_from_schema 重生成后仍并入历史 confirmed 枚举（本轮 draft 不入）
    await kb.build("c1", _schema())
    assert _col_doc(kb, "c1").body.endswith("。取值：P=待付款、S=已发货、R=已退货")


async def test_draft_enum_not_in_doc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    assert kb.annotate_enums("c1", _items()) == 3
    doc = _col_doc(kb, "c1")
    assert "取值：" not in doc.body


async def test_reject_enum_removes_from_doc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    kb.annotate_enums("c1", _items())
    kb.confirm_enum("c1", "orders", "status")
    await asyncio.sleep(0)
    doc = _col_doc(kb, "c1")
    assert doc.body.endswith("。取值：P=待付款、S=已发货、R=已退货")

    assert kb.reject_enum("c1", "orders", "status") == 3
    await asyncio.sleep(0)
    assert "取值：" not in doc.body
    await _assert_vec_matches(kb, "c1", doc)  # 向量随 body 恢复而重算
