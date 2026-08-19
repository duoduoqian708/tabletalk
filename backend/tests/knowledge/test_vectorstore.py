"""VectorStore 契约测试：归一化、collection/metadata 过滤、top-K、空索引。"""
from __future__ import annotations

import pytest

from app.knowledge.vectorstore import NumpyVectorStore, VectorChunk


def _chunks() -> list[VectorChunk]:
    return [
        VectorChunk(id="d1", collection="doc", text="订单 表", vector=[1.0, 0.0],
                    metadata={"source": "auto", "status": "confirmed", "kind": "table"}),
        VectorChunk(id="d2", collection="doc", text="退货 表", vector=[0.9, 0.1],
                    metadata={"source": "ai_draft", "status": "draft", "kind": "table"}),
        VectorChunk(id="d3", collection="doc", text="用户 表", vector=[0.1, 0.9],
                    metadata={"source": "user", "status": "confirmed", "kind": "table"}),
        VectorChunk(id="orders", collection="table", text="orders 表", vector=[1.0, 0.0],
                    metadata={"table": "orders"}),
        VectorChunk(id="customers", collection="table", text="customers 表", vector=[0.1, 0.9],
                    metadata={"table": "customers"}),
    ]


@pytest.fixture()
def vs() -> NumpyVectorStore:
    s = NumpyVectorStore()
    s.set_chunks(_chunks())
    return s


async def test_len_and_empty():
    s = NumpyVectorStore()
    assert len(s) == 0
    assert s.search([1.0, 0.0]) == []
    assert s.scores_all([1.0, 0.0]) == {}


async def test_top_k_ranks_by_cosine(vs):
    hits = vs.search([1.0, 0.0], k=2)
    assert {h.chunk.id for h in hits} == {"d1", "orders"}
    assert hits[0].score >= hits[1].score
    # 归一化：非单位向量查询结果一致（余弦）
    hits2 = vs.search([5.0, 0.0], k=2)
    assert {h.chunk.id for h in hits2} == {h.chunk.id for h in hits}


async def test_collection_filter(vs):
    hits = vs.search([1.0, 0.0], collection="table", k=10)
    assert {h.chunk.id for h in hits} == {"orders", "customers"}
    assert all(h.chunk.collection == "table" for h in hits)


async def test_metadata_filter(vs):
    hits = vs.search([1.0, 0.0], collection="doc", filter={"status": "confirmed"}, k=10)
    assert {h.chunk.id for h in hits} == {"d1", "d3"}
    hits = vs.search([1.0, 0.0], collection="doc", filter={"source": "user"}, k=10)
    assert {h.chunk.id for h in hits} == {"d3"}
    # 组合过滤：不存在的键 → 空
    hits = vs.search([1.0, 0.0], filter={"table": "nope"}, k=10)
    assert hits == []


async def test_scores_all_respects_filter(vs):
    scores = vs.scores_all([1.0, 0.0], collection="table")
    assert set(scores.keys()) == {"orders", "customers"}
    scores = vs.scores_all([1.0, 0.0], collection="doc", filter={"status": "draft"})
    assert set(scores.keys()) == {"d2"}


async def test_set_chunks_rebuild(vs):
    vs.set_chunks([VectorChunk(id="only", collection="doc", vector=[0.0, 1.0])])
    assert len(vs) == 1
    assert [h.chunk.id for h in vs.search([0.0, 1.0], k=1)] == ["only"]
