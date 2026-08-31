"""图遍历（T6）：路径序列化 + join 校验。纯函数（edges 由调用方传入），无 I/O。

对齐设计 §10②③⑤：
- path_strings：图作为"检索器"的输出形态——LLM 可直接用的 join 路径串
- validate_join：图作为"校验器"的落点——拦截幻觉 join
"""
from __future__ import annotations

import logging
from typing import Any

from app.knowledge.graph.model import GraphEdge

logger = logging.getLogger("kb.graph_traverse")


def _coerce(edges: list[Any]) -> list[GraphEdge]:
    """dict 边 → GraphEdge（兼容门面/存储的 dict 格式）。"""
    out: list[GraphEdge] = []
    for e in edges:
        if isinstance(e, GraphEdge):
            out.append(e)
        elif isinstance(e, dict):
            try:
                out.append(GraphEdge.from_dict(e))
            except ValueError:
                continue  # 空 cols 等非法边跳过
        else:
            continue
    return out


def path_strings(edges: list[Any], seeds: set[str], hops: int = 2,
                 allowed: set[str] | None = None) -> list[str]:
    """从种子表 BFS（按边区分，不按表去重），输出路径串列表。

    输出格式：
    - 普通边：  "orders.user_id = users.id (n:1)"
    - 复合键：  "orders.order_id = users.id AND orders.line_no = users.line_no (n:1)"
    - 守卫边：  "X.code = table1.code AND X.type = 1 (n:1)"
    - 自环：    正常输出（visited 集合防死循环）
    - 排序：    confidence 高优先，其次 (source_table, target_table)

    allowed：可选表白名单（如候选表封顶后的子集）——只输出两端都在允许表内的边，
    防止路径串提及候选子图之外的表（T9 §5#4）。
    """
    graph_edges = _coerce(edges)
    if not seeds:
        return []
    if allowed is not None:
        graph_edges = [
            e for e in graph_edges
            if e.source_table in allowed and e.target_table in allowed
        ]

    # 邻接：按表对展开每条边（平行边全保留），visited 按表名防环
    adj: dict[str, list[GraphEdge]] = {}
    for e in graph_edges:
        adj.setdefault(e.source_table, []).append(e)
        adj.setdefault(e.target_table, []).append(e)

    visited: set[str] = set()
    frontier: set[str] = set(seeds)
    seen_edges: set[tuple[str, str, str, str, str]] = set()
    out_edges: list[GraphEdge] = []

    for _ in range(max(0, hops)):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for t in frontier:
            if t in visited:
                continue
            visited.add(t)
            for e in adj.get(t, []):
                # 边的另一端
                other = e.target_table if e.source_table == t else e.source_table
                key = e.key()
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                out_edges.append(e)
                if other not in visited:
                    next_frontier.add(other)
        frontier = next_frontier

    # 排序：confidence 高优先，其次表对
    out_edges.sort(key=lambda e: (-e.confidence, e.source_table, e.target_table))

    out = [
        f"{e.join_condition()} ({e.cardinality})"
        for e in out_edges
    ]
    logger.info("[graph_traverse] seeds=%s hops=%d 输出路径 %d 条", seeds, hops, len(out))
    for p in out:
        logger.debug("[graph_traverse] 路径: %s", p)
    return out


def reachable_tables(edges: list[Any], seeds: set[str], hops: int = 2,
                     kinds: set[str] | None = None) -> set[str]:
    """BFS 可达表集合（按边展开、精确表名匹配，T6 修正 _fk_adj 缺陷）。

    - 返回表集合（路由候选/封顶/图读工具用）；列对级路径请用 path_strings
    - kinds：只沿指定 relation 扩展（路由场景传 {"fk"} 保持 FK 连通语义；
      默认 None = 全部边类型）
    - visited 按表名防环；自环天然不重复入队
    """
    graph_edges = _coerce(edges)
    if not seeds:
        return set()
    adj: dict[str, list[GraphEdge]] = {}
    for e in graph_edges:
        if kinds and e.relation not in kinds:
            continue
        adj.setdefault(e.source_table, []).append(e)
        adj.setdefault(e.target_table, []).append(e)
    visited: set[str] = set()
    frontier: set[str] = set(seeds)
    for _ in range(max(0, hops)):
        if not frontier:
            break
        nxt: set[str] = set()
        for t in frontier:
            for e in adj.get(t, []):
                other = e.target_table if e.source_table == t else e.source_table
                if other not in visited and other not in nxt:
                    nxt.add(other)
        visited |= nxt  # 每轮找到的节点都算可达（含第 hops 轮的边界端点）
        frontier = nxt
    return visited | set(seeds)


def validate_join(edges: list[Any], from_table: str, from_col: str,
                  to_table: str, to_col: str) -> bool:
    """给定 join 列对（双向任一方向）是否存在于图中（任意 confidence）。

    - 守卫边也参与匹配（存在即合法候选）
    - 纯函数，可穷举单测
    """
    for e in _coerce(edges):
        for sc, tc in e.cols:
            if (e.source_table == from_table and sc == from_col
                    and e.target_table == to_table and tc == to_col):
                logger.info("[graph_traverse] validate_join %s.%s = %s.%s → True",
                            from_table, from_col, to_table, to_col)
                return True
            if (e.source_table == to_table and sc == to_col
                    and e.target_table == from_table and tc == from_col):
                logger.info("[graph_traverse] validate_join %s.%s = %s.%s → True（反向）",
                            from_table, from_col, to_table, to_col)
                return True
    logger.info("[graph_traverse] validate_join %s.%s = %s.%s → False",
                from_table, from_col, to_table, to_col)
    return False
