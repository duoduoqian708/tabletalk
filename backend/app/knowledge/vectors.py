"""批量向量检索：numpy 矩阵余弦，几万条文档毫秒级；numpy 不可用时回退纯 Python。

规模评估（实测基准）：5万条 × 2048 维（doubao 嵌入）——
- 纯 Python 逐条：≈ 6.6s/查询（不可接受）
- numpy 批量矩阵乘：≈ 20-50ms/查询（内存 ≈ 400MB float32）
几万条规模下这是"内置向量库"的可靠替代品；文档量到 10 万级以上再换 ANN（sqlite-vec/FAISS），
接口不变（scores_all / top_k）。
"""
from __future__ import annotations

try:
    import numpy as np

    _NP = True
except Exception:  # pragma: no cover - 依赖缺失回退
    _NP = False


class BatchIndex:
    """把 {id: vector} 批量索引为矩阵，提供全量分数与 top-K。向量按列归一化（余弦）。"""

    def __init__(self, vecs: dict[str, list[float]]) -> None:
        self.ids = list(vecs.keys())
        if _NP and self.ids:
            raw = np.asarray([vecs[i] for i in self.ids], dtype=np.float32)
            norms = np.sqrt((raw * raw).sum(axis=1, keepdims=True))
            self.mat = raw / np.where(norms > 0, norms, 1.0)
        else:
            self.mat = None
        self._py = {i: vecs[i] for i in self.ids}

    def __len__(self) -> int:
        return len(self.ids)

    def scores_all(self, qvec: list[float]) -> dict[str, float]:
        """全部向量的余弦分（供与关键词/图谱分数融合）。"""
        if not self.ids:
            return {}
        if self.mat is not None:
            q = np.asarray(qvec, dtype=np.float32)
            s = self.mat @ q
            return {self.ids[i]: float(s[i]) for i in range(len(self.ids))}
        return {i: _dot(qvec, v) for i, v in self._py.items()}

    def top_k(self, qvec: list[float], k: int) -> list[tuple[str, float]]:
        """Top-K（argpartition，O(N)）。"""
        if not self.ids or k <= 0:
            return []
        if self.mat is not None:
            q = np.asarray(qvec, dtype=np.float32)
            s = self.mat @ q
            n = min(k, len(self.ids))
            idx = np.argpartition(-s, n - 1)[:n]
            idx = idx[np.argsort(-s[idx])]
            return [(self.ids[i], float(s[i])) for i in idx]
        return sorted(
            ((i, _dot(qvec, v)) for i, v in self._py.items()),
            key=lambda x: x[1], reverse=True,
        )[:k]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
