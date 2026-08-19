"""批量向量检索：正确性（与暴力排序一致）+ 性能上限（几万条毫秒级）。"""

from __future__ import annotations

import random
import time

from app.knowledge.vectors import BatchIndex


def _vecs(n: int, d: int, seed: int = 7) -> dict[str, list[float]]:
    rng = random.Random(seed)
    out = {}
    for i in range(n):
        v = [rng.random() for _ in range(d)]
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out[f"d{i}"] = [x / norm for x in v]
    return out


def test_top_k_matches_bruteforce():
    vecs = _vecs(200, 64)
    idx = BatchIndex(vecs)
    q = [0.5] * 64
    got = dict(idx.top_k(q, 10))
    brute = dict(sorted(((k, sum(a * b for a, b in zip(q, v))) for k, v in vecs.items()),
                        key=lambda x: x[1], reverse=True)[:10])
    assert set(got) == set(brute)
    # 分数一致性
    for k in got:
        assert abs(got[k] - brute[k]) < 1e-4


def test_scores_all_covers_every_id():
    vecs = _vecs(50, 32)
    idx = BatchIndex(vecs)
    scores = idx.scores_all([1.0] * 32)
    assert set(scores.keys()) == set(vecs.keys())


def test_perf_10k_docs_under_300ms():
    """1 万条 × 256 维：scores_all 必须在 300ms 内（numpy 下实测个位数毫秒；CI 留裕量）。"""
    vecs = _vecs(10_000, 256)
    idx = BatchIndex(vecs)
    q = [0.3] * 256
    t0 = time.perf_counter()
    idx.scores_all(q)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.3, f"10k×256 scores_all 耗时 {elapsed:.3f}s，超出 300ms 预算"
