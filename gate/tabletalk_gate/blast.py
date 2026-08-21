"""爆炸半径（Blast Radius）— A3 本地计算（FK 图谱 + 行数预估）。

直接表：语句触及的表 + COUNT 预估行数
级联表：沿 FK 2 跳可达的表（标注约束是否存在），防爆炸最多 2 层
约束：命中的 FK 名（若 schema 无显式名则用 table.column→ref_table.ref_column）
"""
from __future__ import annotations

from typing import Any


def build_blast(
    state: Any,
    conn_id: str,
    direct_tables: list[str],
    preview_rows: int | None,
) -> dict[str, Any] | None:
    if not direct_tables:
        return None
    # 无知识库时仅返回直接表
    try:
        raw_graph = getattr(state.knowledge, "_graph", {})
        if isinstance(raw_graph, dict) and "edges" in raw_graph:
            # 测试替身直接为 {"edges": [...]}
            graph = raw_graph
        else:
            graph = raw_graph.get(conn_id, {}) if isinstance(raw_graph, dict) else {}
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        fk_edges = [e for e in edges if e.get("kind") == "fk"]
    except Exception:
        fk_edges = []

    # direct
    direct = [{"table": t, "estimated_rows": preview_rows if i == 0 else None} for i, t in enumerate(direct_tables)]

    # cascade：沿 FK 2 跳扩展
    cascade: list[dict[str, Any]] = []
    try:
        seeds = set(direct_tables)
        # 使用 KnowledgeBase.expand_tables 保证与路由一致
        expanded = state.knowledge.expand_tables(conn_id, seeds, hops=2) if hasattr(state.knowledge, "expand_tables") else seeds
        # expanded 包含 seeds 自身，去重后即为级联表
        cascade_tables = [t for t in expanded if t not in seeds]
        # 为每个级联表找一条最短路径的 FK 边（用于标注）
        for ct in cascade_tables:
            # 找一条从 seeds 到 ct 的 FK 路径（简化：直接找与 seeds 相连的 FK）
            via = None
            fk_str = None
            for e in fk_edges:
                if (e.get("from") in seeds and e.get("to") == ct) or (e.get("to") in seeds and e.get("from") == ct):
                    via = e.get("from") if e.get("to") == ct else e.get("to")
                    fk_str = f"{e.get('from')}.{e.get('from_col')} → {e.get('to')}.{e.get('to_col')}"
                    break
                # 2 跳：检查中间表
                # 简化：不做完整 BFS，仅标注 via 为直接 FK 的对端
            cascade.append({
                "table": ct,
                "via": via,
                "fk": fk_str,
                "hops": 1 if via else 2,
                "has_fk": fk_str is not None,
            })
        # 稳定排序
        cascade.sort(key=lambda x: (x["hops"], x["table"]))
        # 限制最多 8 个，防爆炸
        cascade = cascade[:8]
    except Exception:
        cascade = []

    # constraints：命中 FK 的描述（若有）
    constraints: list[str] = []
    for e in fk_edges:
        if e.get("from") in direct_tables or e.get("to") in direct_tables:
            constraints.append(f"{e.get('from')}.{e.get('from_col')}→{e.get('to')}.{e.get('to_col')}")

    return {
        "direct": direct,
        "cascade": cascade,
        "constraints": constraints[:10],
        "preview_rows": preview_rows,
    }
