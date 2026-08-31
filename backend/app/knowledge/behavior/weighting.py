"""L3 行为层：边加权（T10）。

成功查询使用的 join 边 → weight *= factor（封顶 10.0）。
纯函数：edges 由调用方传入（可变 dict 或 GraphEdge 列表）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.knowledge.graph.model import GraphEdge

logger = logging.getLogger("kb.behavior")

_MAX_WEIGHT = 10.0


def bump_weights(edges: list[Any], used: list[Any], factor: float = 1.1) -> int:
    """成功查询使用的 join 边 → weight *= factor（封顶 10.0），返回更新条数。

    - used：本次查询实际经过的边（调用方保证去重）
    - 幂等：重复调用不重复计数（按边身份匹配）
    """
    if not used:
        return 0
    used_keys = {e.key() if isinstance(e, GraphEdge) else _dict_key(e) for e in used}
    updated = 0
    for e in edges:
        key = e.key() if isinstance(e, GraphEdge) else _dict_key(e)
        if key not in used_keys:
            continue
        if isinstance(e, GraphEdge):
            e.weight = min(_MAX_WEIGHT, e.weight * factor)
        else:
            e["weight"] = min(_MAX_WEIGHT, float(e.get("weight") or 1.0) * factor)
        updated += 1
    logger.info("[behavior] 边加权：更新 %d 条（本次查询）", updated)
    return updated


def _dict_key(e: dict) -> tuple:
    """列对 + guard 归一化键。kind 不进键：used 边来自 query_log 挖掘（kind=query_log），
    存量边 kind 各异（fk/naming/...），按列对匹配才能跨来源加权（T10）。"""
    cols = e.get("cols")
    if cols:
        cols_key = "|".join(f"{a}~{b}" for a, b in cols)
    else:
        cols_key = f"{e.get('from_col') or ''}~{e.get('to_col') or ''}"
    return (e.get("from", ""), e.get("to", ""), cols_key, e.get("guard") or "")
