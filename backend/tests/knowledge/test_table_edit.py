"""按表编辑知识（详情面板两块）核心行为测试。

覆盖：
- 表级注释编辑 → 落库 + 状态 confirmed + 合成向量文本更新
- 列知识（comment/values/example）编辑 → 落库 + 列状态 confirmed
- vector_override 覆盖生效：_synthesize_table_text 用覆盖、overview 回显
- 覆盖清空（''）回落合成文本
- 未改动 noop → changed=False
- 未知表/未知列 → KeyError（api 层转 404）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.knowledge.store import ColumnInfo, KnowledgeBase, TableKnowledge


def _mk() -> KnowledgeBase:
    return KnowledgeBase(Path("/tmp/tt-kb-table-edit-test"))


def _mk_tables(kb: KnowledgeBase, conn: str = "c1") -> None:
    kb._tables[conn] = {
        "orders": TableKnowledge(
            name="orders", db_comment="订单表", column_count=2,
            comment="订单主表", status="draft",
            columns={
                "id": ColumnInfo(name="id", type="bigint", pk=True, db_comment="自增主键",
                                 comment="订单ID：订单唯一标识", status="draft"),
                "status": ColumnInfo(name="status", type="varchar", db_comment="状态",
                                     comment="订单状态", values="S=已发货；R=已退货", status="draft"),
            },
        ),
        "customers": TableKnowledge(name="customers", db_comment="客户表", column_count=1,
                                    columns={"id": ColumnInfo(name="id", type="bigint", pk=True)}),
    }


def _synthesized(kb: KnowledgeBase) -> str:
    return kb._synthesize_table_text("c1", kb._tables["c1"]["orders"])


async def test_edit_table_comment_confirms_and_updates_vector_text():
    kb = _mk()
    _mk_tables(kb)
    r = await kb.edit_table_knowledge("c1", "orders", table_comment="订单与退货主表（人工修订）")
    assert r["changed"] is True
    tk = kb._tables["c1"]["orders"]
    assert tk.comment == "订单与退货主表（人工修订）"
    assert tk.status == "confirmed"      # 人工写入 → 权威
    assert "订单与退货主表" in _synthesized(kb)
    # 未确认列仍是结构壳（合成文本语义：confirmed 才入注释/取值）
    assert "id：主键，bigint，自增主键" in _synthesized(kb)


async def test_edit_column_knowledge_fields_only_given():
    kb = _mk()
    _mk_tables(kb)
    r = await kb.edit_table_knowledge(
        "c1", "orders",
        column_comments=[{"name": "status", "comment": "订单生命周期状态", "values": ""}],
    )
    assert r["changed"] is True
    ci = kb._tables["c1"]["orders"].columns["status"]
    assert ci.comment == "订单生命周期状态"
    assert ci.values == ""                 # 显式清空取值对照
    assert ci.example == ""                # 未给字段不动
    assert ci.status == "confirmed"
    # 另一列不受影响
    assert kb._tables["c1"]["orders"].columns["id"].status == "draft"


async def test_vector_override_takes_precedence_and_clears():
    kb = _mk()
    _mk_tables(kb)
    await kb.edit_table_knowledge("c1", "orders", vector_text="订单表，人工重写的一段向量化描述。")
    tk = kb._tables["c1"]["orders"]
    assert tk.vector_override == "订单表，人工重写的一段向量化描述。"
    assert _synthesized(kb) == "订单表，人工重写的一段向量化描述。"
    tbl = next(t for t in kb.overview("c1")["tables"] if t["name"] == "orders")
    assert tbl["vector_text"] == "订单表，人工重写的一段向量化描述。"
    assert tbl["vector_override"] == "订单表，人工重写的一段向量化描述。"

    # 清空覆盖 → 回落合成
    await kb.edit_table_knowledge("c1", "orders", vector_text="")
    assert tk.vector_override == ""
    assert "字段有" in _synthesized(kb)
    tbl2 = next(t for t in kb.overview("c1")["tables"] if t["name"] == "orders")
    assert tbl2["vector_override"] is None


async def test_noop_returns_changed_false():
    kb = _mk()
    _mk_tables(kb)
    r = await kb.edit_table_knowledge("c1", "customers", table_comment="")
    assert r["changed"] is False   # customers 本就无注释，空写 noop


async def test_unknown_table_or_column_raises():
    kb = _mk()
    _mk_tables(kb)
    with pytest.raises(KeyError):
        await kb.edit_table_knowledge("c1", "ghost", table_comment="x")
    with pytest.raises(KeyError):
        await kb.edit_table_knowledge("c1", "orders", column_comments=[{"name": "nope"}])