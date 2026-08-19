"""VectorStore 分层：统一向量索引/检索接口（业务语义，不泄漏厂商 API）。

设计（见 design §8.7）：
- 业务层只依赖 VectorStore 的业务形状：search / scores_all / set_chunks。
- numpy 归一化预处理在 NumpyVectorStore 构造/更新时统一完成。
- collection + metadata 过滤在应用层做（numpy 全量算分下"先筛再算"零成本）。
- 未来换 LanceDB / pgvector / Qdrant = 新增一个 VectorStore 实现，业务代码零改动。

Pydantic 契约：VectorChunk / SearchQuery / SearchHit —— 跨后端的唯一中间语言。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class VectorChunk(BaseModel):
    """一条向量 chunk：用途由 collection 区分，metadata 为自由过滤区。"""
    id: str
    collection: str = "doc"          # doc | table | enum | intent | alias | ...
    text: str = ""                   # 内容原文（可展示 / 关键词融合）
    metadata: dict[str, Any] = Field(default_factory=dict)   # 自由元数据区，可过滤
    vector: list[float] = Field(default_factory=list)
    fingerprint: str = ""            # 嵌入指纹（模型变化 → 全量重嵌）
    updated_at: str = ""


class SearchQuery(BaseModel):
    qvec: list[float]
    collection: str | None = None            # 精确匹配 collection
    filter: dict[str, Any] | None = None     # metadata 子集匹配（所有键值都须命中）
    k: int = 10


class SearchHit(BaseModel):
    chunk: VectorChunk
    score: float


def _match_filter(meta: dict[str, Any], f: dict[str, Any]) -> bool:
    """metadata 子集匹配：filter 的每个键值都存在于 chunk.metadata。"""
    for k, v in f.items():
        if meta.get(k) != v:
            return False
    return True


class VectorStore(ABC):
    """统一向量检索接口（业务语义）。实现：NumpyVectorStore（默认）| 未来 Lance 等。"""

    @abstractmethod
    def set_chunks(self, chunks: list[VectorChunk]) -> None:
        """全量重建索引（构建/加载后调用）。归一化在此统一完成。"""

    @abstractmethod
    def __len__(self) -> int: ...

    @abstractmethod
    def scores_all(self, qvec: list[float], collection: str | None = None,
                   filter: dict[str, Any] | None = None) -> dict[str, float]:
        """全部候选 chunk 的相似度分（融合检索用；分数=余弦相似度）。"""

    @abstractmethod
    def search(self, qvec: list[float], collection: str | None = None,
               filter: dict[str, Any] | None = None, k: int = 10) -> list[SearchHit]:
        """Top-K 检索（先按 collection/filter 筛候选，再算相似度）。"""


class NumpyVectorStore(VectorStore):
    """默认实现：内存 numpy 矩阵（几万条毫秒级）。行向量构造时归一化（余弦）。"""

    def __init__(self) -> None:
        from app.knowledge.vectors import BatchIndex
        self._batch: BatchIndex | None = None
        self._chunks: dict[str, VectorChunk] = {}

    # -- 索引 --
    def set_chunks(self, chunks: list[VectorChunk]) -> None:
        from app.knowledge.vectors import BatchIndex
        self._chunks = {c.id: c for c in chunks}
        self._batch = BatchIndex({c.id: c.vector for c in chunks}) if chunks else None

    def __len__(self) -> int:
        return len(self._chunks)

    # -- 候选过滤 --
    def _candidates(self, collection: str | None, filter: dict[str, Any] | None) -> list[str]:
        if collection is None and not filter:
            return list(self._chunks.keys())
        out: list[str] = []
        for cid, c in self._chunks.items():
            if collection is not None and c.collection != collection:
                continue
            if filter and not _match_filter(c.metadata, filter):
                continue
            out.append(cid)
        return out

    # -- 检索 --
    def scores_all(self, qvec: list[float], collection: str | None = None,
                   filter: dict[str, Any] | None = None) -> dict[str, float]:
        if self._batch is None or not qvec:
            return {}
        cand = self._candidates(collection, filter)
        if not cand:
            return {}
        full = self._batch.scores_all(qvec)
        return {cid: full[cid] for cid in cand if cid in full}

    def search(self, qvec: list[float], collection: str | None = None,
               filter: dict[str, Any] | None = None, k: int = 10) -> list[SearchHit]:
        scores = self.scores_all(qvec, collection, filter)
        if not scores:
            return []
        top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:max(1, k)]
        return [SearchHit(chunk=self._chunks[cid], score=round(s, 4)) for cid, s in top]


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
