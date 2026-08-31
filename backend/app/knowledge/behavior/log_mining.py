"""L3 行为层：查询日志挖掘（T10）。

从审计日志 SQL 提取实际 join 过的表对/列对 → relation=query_log 边。
纯函数：入参 audit_rows = [{sql, connection, executed_at}...]，无 I/O。
"""
from __future__ import annotations

import logging
from typing import Any

from app.knowledge.graph.model import PROV_QUERY_LOG, RELATION_QUERY_LOG, GraphEdge
from app.knowledge.graph.sql_joins import extract_join_pairs

logger = logging.getLogger("kb.behavior")


def mine_join_edges(audit_rows: list[dict], schema: dict | None = None) -> list[GraphEdge]:
    """从审计日志 SQL 提取实际 join 过的表对/列对。

    - 用 sqlglot 解析每条 SQL 的 JOIN 子句 → 提取 (table, col) 对
    - schema 提供时：表/列不存在即丢弃（T10 §4#4 拦幻觉表）
    - 产出：relation=query_log, confidence=0.9, provenance=query_log
    - 解析失败/无法提取 → 跳过该条（宁缺勿错）
    - 去重：同 (表对, 列对) 只产一条
    """
    import sqlglot

    colset: set[tuple[str, str]] | None = None
    if schema is not None:
        colset = {(c["table"], c["name"]) for c in schema.get("columns", [])}

    edges: list[GraphEdge] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(from_t, from_c, to_t, to_c) -> None:
        if colset is not None and (
                (from_t, from_c) not in colset or (to_t, to_c) not in colset):
            return  # 幻觉表/列：丢弃
        key = (from_t, from_c, to_t, to_c)
        if key in seen:
            return
        seen.add(key)
        edges.append(GraphEdge(
            source_table=from_t, target_table=to_t,
            cols=[(from_c, to_c)],
            relation=RELATION_QUERY_LOG, confidence=0.9, provenance=PROV_QUERY_LOG,
            reason="查询日志实际 join",
        ))

    parsed = 0
    for row in audit_rows or []:
        sql = (row.get("sql") or "") if isinstance(row, dict) else ""
        if not sql:
            continue
        try:
            sqlglot.parse_one(sql)
        except Exception:
            logger.debug("[behavior] 日志挖掘跳过 SQL（解析失败）：%s", sql[:80])
            continue
        parsed += 1
        try:
            # 共享 join 解析（与图校验器单一实现，R1）；方向已按表名归一
            for from_t, from_c, to_t, to_c in extract_join_pairs(sql):
                _add(from_t, from_c, to_t, to_c)
        except Exception as e:  # pragma: no cover
            logger.debug("[behavior] 日志挖掘跳过 SQL（join 解析异常 %s）：%s", e, sql[:80])
            continue
    logger.info("[behavior] 日志挖掘：解析 %d 条 SQL，提取 join 边 %d 条", parsed, len(edges))
    return edges
