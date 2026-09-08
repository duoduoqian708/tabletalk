"""L3 行为层服务（T10/R7）：查询成功回灌 + 审计日志挖掘追加。

状态经门面参数传入（与 BuildService 同模式），本类无自有状态。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class BehaviorService:
    """行为层服务：L3 接线点（query 成功路径 / SyncLoop）的编排实现。"""


    # ---------- 迁入门面（R8/T1） ----------
    def record_query_success(self, facade, conn_id: str, sql: str, question: str | None = None) -> dict:
        """查询成功回灌：join 边加权 + few-shot 入库（question 非空时）。

        整体静默降级：回灌失败绝不影响查询链路。返回 {"weighted": n, "fewshot": bool}。
        """
        if conn_id not in facade.semantic_store._schema:
            return {"weighted": 0, "fewshot": False}  # KB 未构建/未加载：跳过
        try:
            from app.knowledge.behavior.log_mining import mine_join_edges
            from app.knowledge.behavior.weighting import bump_weights

            used = mine_join_edges([{"sql": sql}])
            used_dicts = [e.to_dict() for e in used]
            weighted = 0
            if used_dicts:
                edges = facade.graph_store._graph.get(conn_id, {"edges": []}).get("edges", [])
                weighted = bump_weights(edges, used_dicts)
            fewshot = False
            if question and used:
                facade.fewshot_store.add(conn_id, question, sql,
                                       [e.join_condition() for e in used])
                fewshot = True
            if weighted or fewshot:
                facade._save_conn(conn_id)
            if weighted or fewshot:
                logger.info("[kb.behavior] conn=%s 回灌：加权 %d 条 / few-shot %s",
                            conn_id, weighted, fewshot)
            return {"weighted": weighted, "fewshot": fewshot}
        except Exception as e:
            logger.warning("[kb.behavior] conn=%s 查询成功回灌失败：%s", conn_id, e)
            return {"weighted": 0, "fewshot": False}


    def apply_query_log_edges(self, facade, conn_id: str, audit_rows: list[dict]) -> int:
        """审计日志挖掘 -> query_log 边投入待确认队列（2026-08-31 修订：L3 也过人工闸门）。

        - schema 校验在挖掘层（mine_join_edges 拦幻觉表，T10 §4#4）
        - 投递到 draft 队列（merge_draft_edges 统一去重：已确认/已在队列的不重复）
        - 幂等：SyncLoop 每次 tick 可重复调用。返回新增 draft 数。
        """
        if not audit_rows:
            return 0
        if conn_id not in facade.semantic_store._schema:
            return 0  # KB 未构建/未加载：跳过
        try:
            from app.knowledge.graph.builder import build_query_log_edges

            schema = facade.semantic_store._schema.get(conn_id, {})
            drafts = [
                {**_e_to_draft(e), "source": "query_log", "provenance": "query_log"}
                for e in build_query_log_edges(audit_rows, schema=schema)
            ]
            added = facade.graph_store.merge_draft_edges(conn_id, drafts)
            if added:
                facade._save_conn(conn_id)
                logger.info("[kb.behavior] conn=%s 日志挖掘投递 query_log draft 边 %d 条（待确认）",
                            conn_id, added)
            return added
        except Exception as e:
            logger.warning("[kb.behavior] conn=%s 日志挖掘失败：%s", conn_id, e)
            return 0


def _e_to_draft(e) -> dict:
    """GraphEdge -> 统一 draft dict（from_table/from_col 格式）。"""
    first = e.cols[0]
    return {
        "from_table": e.source_table, "from_col": first[0],
        "to_table": e.target_table, "to_col": first[1],
        "source": e.source, "cardinality": e.cardinality,
        "reason": e.reason, "guard": e.guard,
        "confidence": e.confidence, "cols": [list(p) for p in e.cols],
    }
