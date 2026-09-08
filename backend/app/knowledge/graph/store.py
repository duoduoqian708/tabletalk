"""L1 结构层：图状态 + 边读写 + BFS"""
from __future__ import annotations

import logging
from typing import Any

from app.knowledge.graph.model import GraphEdge, derive_edge_types, primary_edge_type

logger = logging.getLogger(__name__)


class GraphStore:
    """图谱存储：负责图状态、边读写、BFS扩展等"""

    def __init__(self) -> None:
        # 图相关状态
        self._graph: dict[str, dict[str, Any]] = {}  # conn -> {edges}
        self._excluded: dict[str, set[str]] = {}  # conn -> 图谱视图中移出的表
        self._llm_graph_edges: dict[str, list[dict[str, Any]]] = {}  # conn -> 待确认 draft 边
        self._diff_base: dict[str, set[tuple]] = {}  # conn -> 本轮重建提案键集合（图 diff 三色基线）
        self._diff_active: dict[str, bool] = {}  # conn -> diff 是否激活（重建后~确认/放弃止）

    # ---------- 图谱构建：一切边经人工确认才生效 ----------
    @staticmethod
    def _graph_edge_key(e: Any) -> tuple:
        """GraphEdge 的跨来源去重键：无序列对 + guard（source 不进键，T4 §4#9）。"""
        return tuple(sorted((e.source_table, sc, e.target_table, tc)
                            for sc, tc in e.cols)) + (e.guard or "",)

    @staticmethod
    def build_draft_edges(schema: dict[str, Any]) -> list[dict[str, Any]]:
        """确定性来源（FK + 命名推断）-> 统一 draft 边（见 graph/builder.py）。

        产出全部待人工确认：draft 不进正式图，confirm 后才生效（含 FK，置信度 1.0）。
        """
        from app.knowledge.graph import builder as _b
        return _b.build_draft_edges(schema)

    def filter_confirmed_by_schema(self, conn_id: str, schema: dict[str, Any]) -> int:
        """按 schema 校验已确认边：端点表/列不存在的边移除（schema 变更失效），返回移除数。"""
        tables = {t["name"] for t in schema.get("tables", [])}
        cols = {(c["table"], c["name"]) for c in schema.get("columns", [])}
        edges = self._graph.get(conn_id, {}).get("edges", [])
        keep = []
        removed = 0
        for e in edges:
            ok = e["from"] in tables and e["to"] in tables
            if ok and e.get("from_col") and (e["from"], e["from_col"]) not in cols:
                ok = False
            if ok and e.get("to_col") and (e["to"], e["to_col"]) not in cols:
                ok = False
            if ok:
                keep.append(e)
            else:
                removed += 1
        if removed:
            self._graph.setdefault(conn_id, {"edges": []})["edges"] = keep
            logger.info("[graph_store] conn=%s schema 变更失效 %d 条已确认边", conn_id, removed)
        return removed

    def _confirmed_edge_keys(self, conn_id: str) -> set[tuple]:
        """已确认边的列对键集合（draft 去重用：已确认的不再重复提案）。"""
        keys: set[tuple] = set()
        for e in self._graph.get(conn_id, {}).get("edges", []):
            a = (e.get("from", ""), e.get("from_col") or "")
            b = (e.get("to", ""), e.get("to_col") or "")
            x, y = sorted([a, b])
            keys.add((x[0], x[1], y[0], y[1]))  # 与 _llm_edge_key 同构（扁平四元组）
        return keys

    def merge_draft_edges(self, conn_id: str, drafts: list[dict[str, Any]],
                          targets: set[str] | None = None) -> int:
        """合并 draft 边到待确认队列（统一入口：确定性来源 / LLM 提案 / L3 日志挖掘）。

        - targets 提供时：替换涉及 targets 的既有 draft，其余保留（增量局部补边）
        - 去重：与已确认边重合的丢弃；队列内按列对去重
        """
        pending = self._llm_graph_edges.get(conn_id, [])
        if targets is not None:
            keep = [e for e in pending
                    if e.get("from_table") not in targets and e.get("to_table") not in targets]
        else:
            keep = list(pending)
        confirmed = self._confirmed_edge_keys(conn_id)
        confirmed_edges = self._graph.get(conn_id, {}).get("edges", [])
        seen = {self._llm_edge_key(e) for e in keep}
        out = list(keep)
        added = 0
        for e in drafts:
            key = self._llm_edge_key(e)
            if key in seen:
                continue
            if key in confirmed:
                # 与已确认边同列对：基数一致 = 无变化丢弃；基数不同 = 保留为"修改"（diff=modified）
                match = next((ce for ce in confirmed_edges
                              if self._llm_edge_key(ce) == key), None)
                if match is not None and (match.get("cardinality") or "n:1") == (e.get("cardinality") or "n:1"):
                    continue
            seen.add(key)
            out.append(e)
            added += 1
        self._llm_graph_edges[conn_id] = out
        return added

    # ---------- 待确认边（draft）管理 ----------
    def llm_graph_edges(self, conn_id: str) -> list[dict[str, Any]]:
        """待确认 draft 边列表（fk/naming/llm/query_log 来源统一，供审查页展示）。"""
        return list(self._llm_graph_edges.get(conn_id, []))

    def rename_table(self, conn_id: str, old: str, new: str) -> None:
        """表重命名（增量同步）：正式图边 + draft 边的端点表名就地改写。

        列名未变（rename 判定前提），from_col/to_col 无需调整。
        """
        for e in self._graph.get(conn_id, {}).get("edges", []):
            if e.get("from") == old:
                e["from"] = new
            if e.get("to") == old:
                e["to"] = new
        for e in self._llm_graph_edges.get(conn_id, []):
            if e.get("from_table") == old:
                e["from_table"] = new
            if e.get("to_table") == old:
                e["to_table"] = new

    @staticmethod
    def _llm_edge_key(e: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
        """边的去重/对比键（字段对，方向无关）。

        兼容 draft 格式（from_table/to_table）与正式图格式（from/to）：去重键统一。
        """
        a = (e.get("from_table") or e.get("from") or "", e.get("from_col") or "")
        b = (e.get("to_table") or e.get("to") or "", e.get("to_col") or "")
        x, y = sorted([a, b])
        return (x[0], x[1], y[0], y[1])

    def _sync_removed_tables(self, conn_id: str, removed: set[str], table_tags: dict[str, list[str]], tags: dict[str, dict[str, Any]], save_conn_fn: Any) -> list[str]:
        """删表清理（增量）：表→标签绑定移除 + 含删表的 LLM draft 边移除 + 0 表标签清理。

        返回被清理的标签名（供审计留痕 detail）。标签绑表数为 0 即孤儿（D2 收紧：清理）。
        """
        cleared: list[str] = []
        if not removed:
            return cleared
        # 1. 表→标签绑定
        tt = table_tags
        if tt:
            for t in removed:
                tt.pop(t, None)
        # 2. LLM draft 边涉及删表的移除
        pending = self._llm_graph_edges.get(conn_id, [])
        if pending:
            keep = [
                e for e in pending
                if e.get("from_table") not in removed and e.get("to_table") not in removed
            ]
            if len(keep) != len(pending):
                self._llm_graph_edges[conn_id] = keep
        # 3. 0 表标签清理（D2）：绑表数=0 即孤儿 → 清理
        lib = tags
        if lib:
            usage: dict[str, int] = {}
            for _t, names in (table_tags or {}).items():
                for n in names:
                    usage[n] = usage.get(n, 0) + 1
            dead = [n for n in list(lib) if usage.get(n, 0) == 0]
            for n in dead:
                del lib[n]
            if dead:
                save_conn_fn(conn_id)
            cleared = dead
        return cleared

    def _upsert_llm_edges(self, conn_id: str, targets: set[str], new_edges: list[dict]) -> int:
        """增量局部补边落库（兼容代理）：委托 merge_draft_edges。"""
        return self.merge_draft_edges(conn_id, new_edges, targets=set(targets))

    def confirm_graph_edges(self, conn_id: str, from_table: str | None = None, save_conn_fn: Any = None) -> int:
        """确认 LLM draft 边 → 写入正式图谱（from_table=None 则确认全部）。
        """
        pending = self._llm_graph_edges.get(conn_id, [])
        if from_table is not None:
            to_confirm = [e for e in pending if e.get("from_table") == from_table or e.get("to_table") == from_table]
        else:
            to_confirm = list(pending)
        if not to_confirm:
            return 0
        confirmed_keys = {self._llm_edge_key(e) for e in to_confirm}
        # 过滤掉待确认的 draft 边
        self._llm_graph_edges[conn_id] = [
            e for e in pending if self._llm_edge_key(e) not in confirmed_keys
        ]
        # 写入正式图谱（边 v2：source + cardinality + reason）
        edges = self._graph.setdefault(conn_id, {"edges": []})["edges"]
        existing = {(e["from"], e["to"], e.get("from_col"), e.get("to_col")) for e in edges}
        added = 0
        for e in to_confirm:
            key = (e.get("from_table"), e.get("to_table"), e.get("from_col"), e.get("to_col"))
            if key in existing:
                continue
            guard = e.get("guard") or None  # S2-4：守卫谓词透传（多态关联）
            cols = e.get("cols")
            if not cols and e.get("from_col") and e.get("to_col"):
                cols = [[e["from_col"], e["to_col"]]]
            edges.append({
                "from": e["from_table"], "from_col": e.get("from_col"),
                "to": e["to_table"], "to_col": e.get("to_col"),
                "source": e.get("source") or "llm", "weight": 1.0,
                "cardinality": e.get("cardinality") or "n:1",
                "reason": e.get("reason", ""),
                "guard": guard,
                "confidence": e.get("confidence", 1.0),
                "provenance": e.get("provenance", ""),
                "cols": cols,
            })
            added += 1
        if added and save_conn_fn:
            save_conn_fn(conn_id)
        return added

    def reject_graph_edges(self, conn_id: str, from_table: str | None = None, save_conn_fn: Any = None) -> int:
        """拒绝 LLM draft 边：从 draft 列表移除（重建时 LLM 重新提案）。"""
        pending = self._llm_graph_edges.get(conn_id, [])
        if from_table is not None:
            to_reject = [e for e in pending if e.get("from_table") == from_table or e.get("to_table") == from_table]
        else:
            to_reject = list(pending)
        if not to_reject:
            return 0
        reject_keys = {self._llm_edge_key(e) for e in to_reject}
        # 从 draft 列表移除
        self._llm_graph_edges[conn_id] = [
            e for e in pending if self._llm_edge_key(e) not in reject_keys
        ]
        if save_conn_fn:
            save_conn_fn(conn_id)
        return len(to_reject)

    # ---------- 图谱查询 ----------
    # ---------- 图 diff（2026-09：本轮重建 vs 当前生效三色标记） ----------
    def set_diff_base(self, conn_id: str, drafts: list[dict]) -> None:
        """重建后建立 diff 基线并激活三色标记：正式图中未被重新提议、端点仍在的边判"删除"（红）。"""
        self._diff_base[conn_id] = {self._llm_edge_key(d) for d in drafts}
        self._diff_active[conn_id] = True

    def clear_diff_base(self, conn_id: str) -> None:
        """确认全部/放弃后清除（审查结束，边全部恢复正常灰态）。"""
        self._diff_base.pop(conn_id, None)
        self._diff_active.pop(conn_id, None)

    def pin_edge(self, conn_id: str, frm: str, to: str, frm_col: str | None,
                 to_col: str | None, save_conn_fn: Any = None) -> int:
        """红边"保留"：给匹配的正式边打 pinned 标记（不再判红；人工决策资产，跨重建保留）。"""
        n = 0
        for e in self._graph.get(conn_id, {}).get("edges", []):
            if e.get("from") == frm and e.get("to") == to \
                    and e.get("from_col") == frm_col and e.get("to_col") == to_col:
                if not e.get("pinned"):
                    e["pinned"] = True
                    n += 1
        if n and save_conn_fn:
            save_conn_fn(conn_id)
        return n

    def graph(self, conn_id: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """图输出（2026-09 修订）：带三色 diff 标记。

        - 正式边 diff:
            "removed" = 本轮未重新提案（不在 draft 队列、不在重建基线）、端点表/字段仍
            存活、且非人工连线/未 pinned——红色（"理论上还应该存在但新版丢了"）
            None = 正常灰态
        - draft 边 diff: "new"（绿）| "modified"（黄，与已确认边同列对但基数不同）
        """
        edges = self._graph.get(conn_id, {"edges": []}).get("edges", [])
        drafts = self._llm_graph_edges.get(conn_id, [])
        base = self._diff_base.get(conn_id, set())
        diff_active = self._diff_active.get(conn_id, False)
        draft_keys = {self._llm_edge_key(d) for d in drafts}

        tables = {t["name"] for t in schema.get("tables", [])} if schema else None
        cols = {(c["table"], c["name"]) for c in schema.get("columns", [])} if schema else None

        def endpoints_alive(e: dict) -> bool:
            if tables is not None and (e.get("from") not in tables or e.get("to") not in tables):
                return False
            if cols is not None and e.get("from_col") and (e.get("from"), e.get("from_col")) not in cols:
                return False
            if cols is not None and e.get("to_col") and (e.get("to"), e.get("to_col")) not in cols:
                return False
            return True

        def key_of(e: dict) -> tuple:
            return self._llm_edge_key(e)

        confirmed_keys = {key_of(e) for e in edges}

        def _with_kinds(e: dict, diff: str | None) -> dict:
            # 形态字段三份输出：kinds/kind（新命名）+ type（旧名兼容），前端任取其一
            kinds = derive_edge_types(e)
            return {**e, "diff": diff,
                    "type": kinds, "kinds": kinds, "kind": primary_edge_type(kinds)}

        out_edges = []
        for e in edges:
            red = (diff_active and not e.get("pinned") and e.get("source") != "user"
                   and endpoints_alive(e)
                   and key_of(e) not in draft_keys and key_of(e) not in base)
            out_edges.append(_with_kinds(e, "removed" if red else None))
        out_drafts = [
            _with_kinds(d, "modified" if key_of(d) in confirmed_keys else "new")
            for d in drafts
        ]
        return {"edges": out_edges, "llm_draft_edges": out_drafts}

    def excluded_tables(self, conn_id: str) -> list[str]:
        """图谱视图中已被移出的表（不影响审查页的表列表）。"""
        return sorted(self._excluded.get(conn_id, set()))

    def set_table_excluded(self, conn_id: str, table: str, excluded: bool, save_conn_fn: Any = None) -> None:
        s = self._excluded.setdefault(conn_id, set())
        if excluded:
            s.add(table)
        else:
            s.discard(table)
        if save_conn_fn:
            save_conn_fn(conn_id)

    def graph_layout(self, conn_id: str, tables: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """2D 图布局坐标回读（表名 → {x,y}；仅含有坐标的表）。"""
        return {
            name: dict(tk.layout)
            for name, tk in tables.get(conn_id, {}).items() if tk.layout
        }

    def set_layout(self, conn_id: str, layout: dict[str, Any], tables: dict[str, Any], save_conn_fn: Any = None) -> int:
        """写入 2D 图布局坐标（前端拖拽回传全量快照，spec §4 payload.layout）。

        仅接受已知表名与 {x,y} 数值点；未知表忽略。只改 TableKnowledge.layout
        并落盘快照——不动嵌入指纹，不触发重嵌（布局纯渲染态）。
        返回实际写入的表数。
        """
        tabs = tables.get(conn_id, {})
        n = 0
        for name, pos in (layout or {}).items():
            tk = tabs.get(name)
            if tk is None or not isinstance(pos, dict):
                continue
            try:
                tk.layout = {"x": float(pos.get("x", 0)), "y": float(pos.get("y", 0))}
            except (TypeError, ValueError):
                continue
            n += 1
        if n and save_conn_fn:
            save_conn_fn(conn_id)
        return n

    def add_graph_edge(self, conn_id: str, frm: str, to: str, source: str,
                       frm_col: str | None = None, to_col: str | None = None,
                       weight: float | None = None,
                       cardinality: str = "n:1",
                       schema: dict[str, Any] | None = None,
                       save_conn_fn: Any = None,
                       guard: str | None = None,
                       cols: list[list[str]] | None = None) -> dict[str, Any]:
        """新增一条图谱边（边 v2：字段级端点 + 基数 + 守卫 + 复合列对）。

        source ∈ fk|user（user=手动连线）；cardinality ∈ n:1|1:1|1:N|N:M（默认 n:1）。
        guard：多态关联守卫谓词（如 "X.type = 1"），普通关联省略。
        cols：复合边完整列对 [[from,to],...]；缺省用 from_col/to_col 单列。
        去重按 (from/to/source/from_col/to_col/guard) 全量匹配。
        """
        if source not in ("fk", "user"):
            raise ValueError("source 必须是 fk|user")
        if cardinality not in ("n:1", "1:1", "1:N", "N:M"):
            raise ValueError("cardinality 必须是 n:1|1:1|1:N|N:M")
        tables = {t["name"] for t in (schema or {}).get("tables", [])}
        if frm not in tables or to not in tables:
            raise ValueError("未知表名")
        # 规范化列对：cols 提供则用它，否则用 from_col/to_col 单列
        col_pairs: list[tuple[str | None, str | None]]
        if cols:
            col_pairs = [(p[0], p[1]) if len(p) >= 2 else (None, None) for p in cols]
        else:
            col_pairs = [(frm_col, to_col)]
        edges = self._graph.setdefault(conn_id, {"edges": []})["edges"]
        existing = next((e for e in edges
                         if e["from"] == frm and e["to"] == to and e.get("source") == source
                         and e.get("from_col") == frm_col and e.get("to_col") == to_col
                         and e.get("guard") == guard), None)
        if existing:
            return existing
        e = {"from": frm, "from_col": frm_col, "to": to, "to_col": to_col,
             "source": source, "weight": weight, "shared": None,
             "cardinality": cardinality,
             "reason": "" if source != "user" else "人工连线"}
        if guard:
            e["guard"] = guard
        if col_pairs and len(col_pairs) > 1:
            e["cols"] = [list(p) for p in col_pairs]
        kinds = derive_edge_types(e)
        e["type"] = kinds
        e["kinds"] = kinds
        e["kind"] = primary_edge_type(kinds)
        edges.append(e)
        if save_conn_fn:
            save_conn_fn(conn_id)
        return e

    def remove_graph_edge(self, conn_id: str, frm: str, to: str, source: str, save_conn_fn: Any = None) -> int:
        """删除一条图谱边。注意：确定性来源（fk/naming/query_log）的边
        在下次重建/增量构建时会重新生成--删除只对当前版本生效。"""
        edges = self._graph.get(conn_id, {"edges": []})["edges"]
        before = len(edges)
        edges[:] = [e for e in edges
                    if not (e["from"] == frm and e["to"] == to and e.get("source") == source)]
        removed = before - len(edges)
        if removed and save_conn_fn:
            save_conn_fn(conn_id)
        return removed

    def _neighbors(self, conn_id: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for e in self._graph.get(conn_id, {}).get("edges", []):
            out.setdefault(e["from"].lower(), set()).add(e["to"].lower())
            out.setdefault(e["to"].lower(), set()).add(e["from"].lower())
        return out

    def _neighbors_for(self, conn_id: str, table: str) -> list[dict[str, Any]]:
        """某表的图谱邻居（供图谱可视化高亮）。"""
        t = table.lower()
        return [
            e for e in self._graph.get(conn_id, {}).get("edges", [])
            if e["from"].lower() == t or e["to"].lower() == t
        ]

    def expand_tables(self, conn_id: str, seeds: set[str], hops: int = 2) -> set[str]:
        """沿图边多跳扩展种子表 → 连通子图表集合（路由/封顶/图读工具用）。

        T6 修正：委托 traverse.reachable_tables（按边 BFS、精确表名匹配，
        不再 lower() 错配、不再 set 合并吞平行边）。
        P2-15/§10②：BFS 不限边类型——naming/value_overlap 可达表同样进候选
        （join 路径仍由 path_strings 的 fk/守卫语义决定，两者解耦）。
        """
        from app.knowledge.graph.traverse import reachable_tables
        edges = self._graph.get(conn_id, {}).get("edges", [])
        return reachable_tables(edges, set(seeds), hops)
