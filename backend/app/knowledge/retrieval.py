"""检索服务：retrieve/to_context/route_tables"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from app.core.timeutil import utcnow_iso
from app.knowledge.vectorstore import NumpyVectorStore, VectorChunk, VectorStore

logger = logging.getLogger(__name__)


class RetrievalService:
    """检索服务：负责向量检索、路由表等"""

    def __init__(self) -> None:
        # 检索相关状态
        self._table_vec: dict[str, dict[str, list[float]]] = {}  # conn -> 表名 -> 表级向量
        self._vstore: dict[str, VectorStore] = {}  # conn -> 统一向量索引
        self._artifact_fingerprint: dict[str, str] = {}  # conn -> 构建时的嵌入指纹

    def _vector_store(self, conn_id: str) -> VectorStore:
        vs = self._vstore.get(conn_id)
        if vs is None:
            vs = NumpyVectorStore()
            self._vstore[conn_id] = vs
            self._rebuild_vstore(conn_id)
        return vs

    def _rebuild_vstore(self, conn_id: str, tables: dict[str, Any] = None,
                        table_tags: dict[str, Any] = None, synced_at: str = "",
                        synthesize_text_fn: Any = None, table_payload_fn: Any = None) -> None:
        """单体系重建：每表一条 VectorChunk（collection=table，一表一 chunk，spec §4）。

        id=tbl-{name}；text=可读表描述；metadata={table} 过滤区；
        payload=结构化信息（ddl/tags/layout/draft_count/updated_at）。
        向量维度统一对齐主维度（旧 artifact 可能有嵌入失败残留的 [0.0] 短向量）。
        """
        chunks: list[VectorChunk] = []
        now = utcnow_iso()
        fp = self._artifact_fingerprint.get(conn_id, "")
        tabs = (tables or {}).get(conn_id, {})
        vecs = self._table_vec.get(conn_id, {})
        # 主维度：出现最多的向量长度（doubao=2048 / 哈希=256）
        all_vecs = [v for v in vecs.values() if v]
        main_dim = Counter(len(v) for v in all_vecs).most_common(1)[0][0] if all_vecs else 256

        def _aligned(vec: list[float]) -> list[float]:
            if len(vec) == main_dim:
                return vec
            return (vec[:main_dim] + [0.0] * main_dim)[:main_dim]

        for name, tk in tabs.items():
            vec = vecs.get(name)
            if not vec:
                continue
            text = synthesize_text_fn(conn_id, tk) if synthesize_text_fn else ""
            payload = table_payload_fn(conn_id, tk, synced_at) if table_payload_fn else {}
            chunks.append(VectorChunk(
                id=f"tbl-{name}", collection="table",
                text=text,
                metadata={"table": name},
                payload=payload,
                vector=_aligned(vec), fingerprint=fp,
                updated_at=synced_at or now,
            ))
        self._vstore[conn_id] = NumpyVectorStore()
        self._vstore[conn_id].set_chunks(chunks)

    def _neighbors(self, conn_id: str, graph: dict[str, Any] = None) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        g = (graph or {}).get(conn_id, {"edges": []})
        for e in g.get("edges", []):
            out.setdefault(e["from"].lower(), set()).add(e["to"].lower())
            out.setdefault(e["to"].lower(), set()).add(e["from"].lower())
        return out

    async def retrieve(self, conn_id: str, query: str = "", table: str | None = None, k: int = 10,
                       tables: dict[str, Any] = None, emb: Any = None,
                       reembed_fn: Any = None, log_usage_fn: Any = None,
                       synthesize_text_fn: Any = None, table_payload_fn: Any = None,
                       graph: dict[str, Any] = None,
                       table_whitelist: set[str] | None = None) -> list[Any]:
        """检索命中表知识卡（一表一卡，spec §4）：关键词 × 表级向量 × 图谱邻居扩散。

        - 关键词词面命中 / 向量语义召回（同一 chunk 集）；
        - 已确认（payload.draft_count=0）优先于 AI 草案；
        - 图谱扩展：相邻表高分 → 本表加分（FK 边连通子图）；
        - 零命中（空查询/无相关）回退全表排序，保证返回非空；
        - table_whitelist（T9）：语义检索限定在图选定的候选表内（双写 §4.1）。
        """
        if reembed_fn:
            await reembed_fn(conn_id)
        tabs = (tables or {}).get(conn_id, {})
        if not tabs:
            return []
        tokens = [t for t in re.split(r"[\s,，。；;：:、/\\|()（）]+", (query or "").lower()) if t]
        q = (query or "").lower()
        tgt = (table or "").lower()
        # 嵌入失败降级词法（与 _vector_route_impl 对齐）：向量端点抖动不阻断检索
        try:
            qvec = await emb.embed(query) if emb and query else None
        except Exception as e:
            logger.warning("[kb.retrieve] conn=%s 查询向量失败，降级词法检索：%s", conn_id, e)
            qvec = None
        vec_scores = self._vector_store(conn_id).scores_all(qvec, collection="table") if qvec is not None else {}

        texts: dict[str, str] = {}
        payloads: dict[str, dict[str, Any]] = {}
        for name, tk in tabs.items():
            texts[name] = synthesize_text_fn(conn_id, tk) if synthesize_text_fn else ""
            payloads[name] = table_payload_fn(conn_id, tk) if table_payload_fn else {}

        base: dict[str, float] = {}
        for name in tabs:
            blob = texts[name].lower()
            s = float(sum(1 for t in tokens if t in blob))
            if q and q in blob:
                s += 1.0
            if qvec is not None:
                s += 2.0 * vec_scores.get(f"tbl-{name}", 0.0)
            if tgt:
                if name.lower() == tgt:
                    s += 3.0
                elif tgt in blob:
                    s += 2.0
            # 已确认（无 draft）优先于 AI 草案
            if payloads[name].get("draft_count", 0) == 0:
                s += 1.5
            base[name] = s

        # 图谱扩展：相邻表高分 → 本表加分（FK 边）
        neighbors = self._neighbors(conn_id, graph)
        final = dict(base)
        for name in tabs:
            best = max((base.get(nt, 0.0) for nt in neighbors.get(name.lower(), ())), default=0.0)
            if best > 0:
                final[name] += 0.35 * best

        scored = sorted(tabs.keys(), key=lambda n: final.get(n, 0.0), reverse=True)
        if not any(final.get(n, 0.0) > 0 for n in tabs):
            scored = list(tabs.keys())
        # 记录检索阶段的嵌入用量
        if log_usage_fn:
            log_usage_fn(conn_id, "retrieve")
        from app.knowledge.store import TableCard  # noqa: PLC0415
        cards = [
            TableCard(table=n, text=texts[n], payload=payloads[n], score=round(final.get(n, 0.0), 4))
            for n in scored[:max(1, k)]
        ]
        if table_whitelist:
            cards = [c for c in cards if c.table in table_whitelist]
        return cards

    async def to_context(self, conn_id: str, query: str = "", table: str | None = None, k: int = 8,
                         **kwargs: Any) -> str:
        """转成给 AI 的上下文文本（spec §4）：【知识库】段为命中表的完整知识卡列表。"""
        cards = await self.retrieve(conn_id, query, table, k, **kwargs)
        if not cards:
            return ""
        lines = ["【知识库】"]
        for c in cards:
            mark = "" if c.payload.get("draft_count", 0) == 0 else "（AI 草案，待确认）"
            lines.append(f"- [表]{mark} {c.text}")
        return "\n".join(lines)

    async def _vector_route_impl(self, conn_id: str, question: str, top_k: int = 6,
                                 tables: dict[str, Any] = None, emb: Any = None,
                                 reembed_fn: Any = None,
                                 synthesize_text_fn: Any = None) -> list[tuple[str, float]]:
        """向量通道：问题向量 × 表级 chunk（与 retrieve 同一 chunk 集）→ top-K 表。

        语义召回不依赖标签覆盖率；向量模型需通过 API 接入，
        因此叠加词面加权作为底线：表名/字段/注释与问题同词 → 加分
        （配 API 真语义 embedder 时语义分数自动更强）。
        """
        if reembed_fn:
            await reembed_fn(conn_id)
        tabs = (tables or {}).get(conn_id, {})
        if not tabs or not (question or "").strip():
            return []
        try:
            qvec = await emb.embed(question) if emb else None
        except Exception:
            qvec = None
        ql = (question or "").lower()
        base_vec = self._vector_store(conn_id).scores_all(qvec, collection="table") if qvec is not None else {}
        scored: list[tuple[str, float]] = []
        for name, tk in tabs.items():
            score = base_vec.get(f"tbl-{name}", 0.0)
            # 词面加权：表名 / 中文词 / 字段名出现在问题或反之中
            blob = (synthesize_text_fn(conn_id, tk) if synthesize_text_fn else "").lower()
            if name.lower() in ql:
                score += 0.6
            # 中文词面：重叠二元组（"哪些订单申请了退款" -> 哪些/些订/订单/.../退款）
            for i in range(len(ql) - 1):
                a, b = ql[i], ql[i + 1]
                if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff":
                    if a + b in blob:
                        score += 0.35
                        break  # 一表得一次加权即可（防全命中刷分）
            for tok in re.findall(r"[a-z_]{3,}", ql):
                if tok in blob:
                    score += 0.2
            if score > 0.1:
                scored.append((name, round(score, 4)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


    # ---------- 迁入门面（R8/T1） ----------
    def rebuild_vstore(self, facade, conn_id: str) -> None:
        """重建向量索引（状态经门面注入）。"""
        self._rebuild_vstore(
            conn_id,
            tables=facade.semantic_store._tables,
            table_tags=facade.semantic_store._table_tags,
            synced_at=facade._synced_at.get(conn_id, ""),
            synthesize_text_fn=facade.semantic_store.table_vector_text,
            table_payload_fn=facade.semantic_store._table_payload,
        )

    def route_tables(self, facade, conn_id: str, tag_names: list[str], hops: int = 2) -> dict[str, Any]:
        facade.ensure_loaded(conn_id)
        confirmed = set(facade.confirmed_tags(conn_id))
        tag_set = {t for t in tag_names if t in confirmed}
        if not tag_set:
            return {"tables": [], "edges": [], "seeded": 0}
        picks: set[str] = set()
        for table, names in facade.semantic_store._table_tags.get(conn_id, {}).items():
            if set(names) & tag_set:
                picks.add(table)
        picks = facade.expand_tables(conn_id, picks, hops)
        sub_edges = [
            e for e in facade.graph_store._graph.get(conn_id, {}).get("edges", [])
            if e["from"] in picks and e["to"] in picks
        ]
        return {"tables": sorted(picks), "edges": sub_edges, "seeded": len(tag_set)}


    async def vector_route_tables(self, facade, conn_id: str, question: str, top_k: int = 6) -> list[tuple[str, float]]:
        facade.ensure_loaded(conn_id)
        return await facade.retrieval_service._vector_route_impl(
            conn_id, question, top_k,
            tables=facade.semantic_store._tables,
            emb=facade._emb,
            reembed_fn=facade.reembed_if_needed,
            synthesize_text_fn=facade.semantic_store.table_vector_text,
        )


    def overview(self, facade, conn_id: str) -> dict[str, Any]:
        facade.ensure_loaded(conn_id)
        """人工审查页的数据（v2 按表组织）：表块含逐列注释/取值对照/示例与确认状态。"""
        snap = facade.semantic_store._schema.get(conn_id, {})
        kind_by_table = {t["name"]: t.get("kind", "table") for t in snap.get("tables", [])}
        lib = facade.semantic_store._tags.get(conn_id, {})

        def table_tags(table: str) -> list[dict[str, Any]]:
            return [
                {
                    "name": n,
                    "status": lib.get(n, {}).get("status", "draft"),
                    "color": lib.get(n, {}).get("color", ""),
                }
                for n in facade.semantic_store._table_tags.get(conn_id, {}).get(table, []) if n in lib
            ]

        excluded = set(facade.excluded_tables(conn_id))
        draft_count = 0
        tables_out: list[dict[str, Any]] = []
        for tk in facade.semantic_store._tables.get(conn_id, {}).values():
            if tk.has_proposal:
                draft_count += 1
            cols = []
            for ci in tk.columns.values():
                if ci.has_proposal:
                    draft_count += 1
                cols.append({
                    "name": ci.name, "type": ci.type,
                    "pk": ci.pk, "fk": ci.fk,
                    "db_comment": ci.db_comment,
                    "comment": ci.comment,
                    "values": ci.values,
                    "is_enum": bool(ci.values or ci.proposed_values),
                    "example": ci.example,
                    "status": ci.status,
                    # 2026-09 修订：本轮提案（与当前生效值并行，供审查页对比）
                    "proposed_comment": ci.proposed_comment,
                    "proposed_values": ci.proposed_values,
                    "proposed_example": ci.proposed_example,
                })
            tables_out.append({
                "name": tk.name,
                "kind": kind_by_table.get(tk.name, "table"),
                "db_comment": tk.db_comment,
                "column_count": tk.column_count,
                "comment": tk.comment,
                "comment_status": tk.status,
                "proposed_comment": tk.proposed_comment,  # 本轮提案（表级）
                "tags": table_tags(tk.name),
                "excluded": tk.name in excluded,
                "ddl": tk.ddl,
                "columns": cols,
                "vector_text": facade.semantic_store.table_vector_text(conn_id, tk),
                "vector_override": tk.vector_override or None,
                "vector_profile": tk.vector_profile or None,
                "proposed_profile": tk.proposed_profile or None,
            })

        return {
            "tables": tables_out,
            "graph": {
                # 走 diff 计算（red/removed 三色）
                **facade.graph(conn_id),
                "excluded": sorted(excluded),
                "layout": facade.graph_layout(conn_id),
            },
            "tags": facade.tags(conn_id),
            "draft_count": draft_count,
            "tag_draft_count": sum(1 for v in lib.values() if v.get("status") == "draft"),
            "sample_cols": sum(len(cols) for cols in facade.semantic_store._samples.get(conn_id, {}).values()),
            "embedding_provider": (
                facade._runtime.get().embedding_provider if facade._runtime else ""
            ),
        }

    # ---------- 公开方法：构建相关 ----------

