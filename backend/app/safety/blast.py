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
    # 无知识库时仅返回直接表（优先公共接口，兼容测试替身）
    try:
        if hasattr(state.knowledge, "graph") and callable(getattr(state.knowledge, "graph")):
            graph = state.knowledge.graph(conn_id)
        else:
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

    # cascade：沿 FK 2 跳扩展（BFS 带路径，保持与 KnowledgeBase.expand_tables 一致）
    cascade: list[dict[str, Any]] = []
    try:
        seeds = set(direct_tables)
        # 构建邻接表（带边信息）
        adj: dict[str, list[dict[str, Any]]] = {}
        for e in fk_edges:
            adj.setdefault(e["from"], []).append(e)
            adj.setdefault(e["to"], []).append(e)
        # BFS（最多 2 跳），记录 parent 与 edge
        from collections import deque
        visited: set[str] = set(seeds)
        parent: dict[str, tuple[str | None, dict[str, Any] | None, int]] = {s: (None, None, 0) for s in seeds}
        q: deque[str] = deque(seeds)
        # 为了与 expand_tables 的 2 跳保持一致，手动 BFS
        while q:
            cur = q.popleft()
            _, _, cur_hops = parent[cur]
            if cur_hops >= 2:
                continue
            for e in adj.get(cur, []):
                nxt = e["to"] if e["from"] == cur else e["from"]
                if nxt in visited:
                    continue
                visited.add(nxt)
                parent[nxt] = (cur, e, cur_hops + 1)
                q.append(nxt)
        # expanded 即 visited
        expanded = visited
        cascade_tables = [t for t in expanded if t not in seeds]
        for ct in cascade_tables:
            via, edge, hops = parent.get(ct, (None, None, 2))
            fk_str = f"{edge['from']}.{edge['from_col']} → {edge['to']}.{edge['to_col']}" if edge else None
            cascade.append({
                "table": ct,
                "via": via,
                "fk": fk_str,
                "hops": hops if isinstance(hops, int) else 2,
                "has_fk": fk_str is not None,
            })
        cascade.sort(key=lambda x: (x["hops"], x["table"]))
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
