"""KnowledgeBase 门面：保留全部公开方法签名，委托各模块。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.timeutil import utcnow_iso

from app.knowledge.docs import KnowledgeDoc  # noqa: F401 - 运行时使用（_load_conn 等）

from app.knowledge.graph.store import GraphStore
from app.knowledge.semantic.store import SemanticStore
from app.knowledge.semantic.concepts import ConceptStore
from app.knowledge.filters import FilterStore
from app.knowledge.behavior.fewshot import FewShotStore
from app.knowledge.behavior.service import BehaviorService
from app.knowledge.retrieval import RetrievalService
from app.knowledge.build import BuildService

if TYPE_CHECKING:
    from app.core.settings import SettingsStore

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """门面：公开方法签名与现有完全一致，内部委托各模块。"""

    def __init__(self, data_dir: Path, runtime: "SettingsStore | None" = None) -> None:
        self._data_dir = data_dir
        self._runtime = runtime
        self._storage_backend = os.environ.get("TABLETALK_KB_STORAGE", "")  # sqlite(默认) | json
        self._storages: dict[str, Any] = {}

        # 子模块
        self.graph_store = GraphStore()
        self.semantic_store = SemanticStore()
        self.concept_store = ConceptStore()
        self.filter_store = FilterStore()
        self.fewshot_store = FewShotStore()
        self.retrieval_service = RetrievalService()
        self.build_service = BuildService(runtime)
        self.behavior_service = BehaviorService()

        # 门面保留的状态（持久化/版本协调）
        self._auto: dict[str, list[Any]] = {}

        # 嵌入器（按需创建，依赖运行时配置）
        self._emb = None

    # ---------- 向后兼容：子模块内部状态经 property 暴露（无别名赋值 hack） ----------
    _tables = property(lambda self: self.semantic_store._tables)
    _tags = property(lambda self: self.semantic_store._tags)
    _table_tags = property(lambda self: self.semantic_store._table_tags)
    _samples = property(lambda self: self.semantic_store._samples)
    _schema = property(lambda self: self.semantic_store._schema)
    _graph = property(lambda self: self.graph_store._graph)
    _schema_fingerprint_map = property(lambda self: self.build_service._schema_fingerprint_map)
    _synced_at = property(lambda self: self.build_service._synced_at)
    _excluded = property(lambda self: self.graph_store._excluded)
    _llm_graph_edges = property(lambda self: self.graph_store._llm_graph_edges)
    _table_vec = property(lambda self: self.retrieval_service._table_vec)
    _vstore = property(lambda self: self.retrieval_service._vstore)
    _artifact_fingerprint = property(lambda self: self.retrieval_service._artifact_fingerprint)

    # ---------- 注释缓存（内容寻址，阶段一 LLM 注释跳过重复调用） ----------
    def annotation_cache(self, conn_id: str) -> dict[str, dict]:
        """返回连接级注释缓存 dict（就地读写，随快照落盘）。"""
        self.ensure_loaded(conn_id)
        return self.build_service._annotation_cache.setdefault(conn_id, {})

    # ---------- 持久化 ----------
    def _storage(self, conn_id: str) -> Any:
        st = self._storages.get(conn_id)
        if st is None:
            from app.knowledge.storage import make_storage
            st = make_storage(self._data_dir, conn_id, self._storage_backend)
            self._storages[conn_id] = st
        return st

    def _load_conn(self, conn_id: str) -> None:
        """从存储后端恢复一个连接的知识库（v2 快照；旧版本工件已在存储层作废为空）。

        空/作废快照也要写入哨兵（_auto/_user 置空表）——否则 ensure_loaded 的
        成员判定永远不满足，每次调用都重读一次磁盘文件。"""
        if conn_id in self.semantic_store._user:
            return
        snap = self._storage(conn_id).load()
        if not snap.tables and not snap.auto and not snap.user and not snap.edges:
            self._auto[conn_id] = []
            self.semantic_store._user[conn_id] = []
            return
        def _docs_(items: list[dict]) -> list[KnowledgeDoc]:
            out = []
            for d in items:
                dd = dict(d)
                dd.setdefault("conn_id", conn_id)
                dd.setdefault("updated_at", "")
                out.append(KnowledgeDoc(**dd))
            return out

        self._auto[conn_id] = _docs_(snap.auto)
        self.semantic_store._user[conn_id] = _docs_(snap.user)
        self.semantic_store._samples[conn_id] = snap.samples
        self.graph_store._graph[conn_id] = {"edges": snap.edges}
        self.retrieval_service._table_vec[conn_id] = snap.table_vec
        self.semantic_store._tags[conn_id] = snap.tags
        self.semantic_store._table_tags[conn_id] = snap.table_tags
        from app.knowledge.store import TableKnowledge
        self.semantic_store._tables[conn_id] = {
            name: TableKnowledge.from_dict(d) for name, d in snap.tables.items()
        }
        self.semantic_store._schema[conn_id] = snap.schema
        self.retrieval_service._artifact_fingerprint[conn_id] = snap.emb_fingerprint
        self._schema_fingerprint_map[conn_id] = snap.schema_fingerprint
        self.graph_store._excluded[conn_id] = set(snap.excluded)
        self._synced_at[conn_id] = snap.synced_at
        self.graph_store._llm_graph_edges[conn_id] = snap.llm_graph_edges
        self.concept_store.load(conn_id, snap.concepts)
        self.filter_store.load(conn_id, snap.table_filters)
        self.fewshot_store.load(conn_id, snap.fewshot)
        self.build_service._annotation_cache[conn_id] = dict(snap.annotation_cache or {})
        if snap.round:
            self.semantic_store.set_round(conn_id, **snap.round)
        # P1-5：图 diff 基线恢复（与 round 同生命周期）
        if snap.diff_base:
            self.graph_store._diff_base[conn_id] = {tuple(k) for k in snap.diff_base}
        if snap.diff_active:
            self.graph_store._diff_active[conn_id] = True
        self._rebuild_vstore(conn_id)

    def _save_conn(self, conn_id: str) -> None:
        """全量快照写回存储后端（标准格式/JSON 均在此落盘）。

        同步方法：供 sync 链（graph/tag 低频交互写）沿用；构建热路径请用
        _save_conn_async（to_thread，避免大库 DELETE+INSERT 阻塞事件循环）。
        """
        self._save_conn_blocking(conn_id)

    def _save_conn_blocking(self, conn_id: str) -> None:
        try:
            from app.knowledge.storage import KbSnapshot
            snap = KbSnapshot(
                tables={name: tk.to_dict() for name, tk in self.semantic_store._tables.get(conn_id, {}).items()},
                user=[d.to_dict() for d in self.semantic_store._user.get(conn_id, [])],
                auto=[d.to_dict() for d in self._auto.get(conn_id, [])],  # P1-3：AI 结构文档落盘
                samples=self.semantic_store._samples.get(conn_id, {}),
                edges=self.graph_store._graph.get(conn_id, {"edges": []}).get("edges", []),
                table_vec=self.retrieval_service._table_vec.get(conn_id, {}),
                tags=self.semantic_store._tags.get(conn_id, {}),
                table_tags=self.semantic_store._table_tags.get(conn_id, {}),
                schema=self.semantic_store._schema.get(conn_id, {}),
                emb_fingerprint=self.retrieval_service._artifact_fingerprint.get(conn_id, ""),
                schema_fingerprint=self._schema_fingerprint_map.get(conn_id, ""),
                excluded=list(self.graph_store._excluded.get(conn_id, set())),
                synced_at=self._synced_at.get(conn_id, ""),
                llm_graph_edges=self.graph_store._llm_graph_edges.get(conn_id, []),
                concepts=self.concept_store.dump(conn_id),
                table_filters=self.filter_store.dump(conn_id),
                fewshot=self.fewshot_store.dump(conn_id),
                annotation_cache=self.build_service._annotation_cache.get(conn_id, {}),
                round=self.semantic_store.get_round(conn_id),
                # P1-5：图 diff 基线随快照持久化（此前只在内存——重启后红边全灭转灰）
                diff_base=[list(k) for k in self.graph_store._diff_base.get(conn_id, set())],
                diff_active=bool(self.graph_store._diff_active.get(conn_id, False)),
            )
            self._storage(conn_id).save(snap)
        except Exception as e:
            logger.warning("[kb.store] conn=%s 快照落盘失败：%s", conn_id, e)

    async def _save_conn_async(self, conn_id: str) -> None:
        """构建热路径落盘：全量快照序列化+写库挪线程池，不卡事件循环（大库 SSE 不冻结）。"""
        await asyncio.to_thread(self._save_conn_blocking, conn_id)

    def _rebuild_vstore(self, conn_id: str) -> None:
        """重建向量索引"""
        self.retrieval_service.rebuild_vstore(self, conn_id)

    # ---------- 私有 API 兼容代理（测试/内部沿用旧类私有方法名） ----------
    _schema_fingerprint = staticmethod(BuildService._schema_fingerprint)
    diff_schema = staticmethod(BuildService.diff_schema)
    diff_is_empty = staticmethod(BuildService.diff_is_empty)
    _llm_edge_key = staticmethod(GraphStore._llm_edge_key)

    def _vector_store(self, conn_id: str) -> Any:
        return self.retrieval_service._vector_store(conn_id)

    def table_vector_text(self, conn_id: str, tk: Any) -> str:
        return self.semantic_store.table_vector_text(conn_id, tk)

    def _table_payload(self, conn_id: str, tk: Any, synced_at: str = "") -> dict[str, Any]:
        return self.semantic_store._table_payload(conn_id, tk, synced_at)

    def _sync_removed_tables(self, conn_id: str, removed: set[str]) -> list[str]:
        """删表清理（增量）：代理到图模块，注入语义层状态与持久化回调。"""
        return self.graph_store._sync_removed_tables(
            conn_id, removed,
            self.semantic_store._table_tags.get(conn_id, {}),
            self.semantic_store._tags.get(conn_id, {}),
            self._save_conn,
        )

    def _upsert_llm_edges(self, conn_id: str, targets: set[str], new_edges: list[dict]) -> None:
        return self.graph_store._upsert_llm_edges(conn_id, targets, new_edges)

    # ---------- 公开方法：图相关 ----------
    def graph(self, conn_id: str) -> dict[str, Any]:
        self.ensure_loaded(conn_id)
        return self.graph_store.graph(conn_id, self.semantic_store._schema.get(conn_id))

    def expand_tables(self, conn_id: str, seeds: set[str], hops: int = 2) -> set[str]:
        return self.graph_store.expand_tables(conn_id, seeds, hops)

    def add_graph_edge(self, conn_id: str, frm: str, to: str, source: str,
                       frm_col: str | None = None, to_col: str | None = None,
                       weight: float | None = None,
                       cardinality: str = "n:1",
                       guard: str | None = None,
                       cols: list[list[str]] | None = None) -> dict[str, Any]:
        return self.graph_store.add_graph_edge(
            conn_id, frm, to, source, frm_col, to_col, weight, cardinality,
            schema=self.semantic_store._schema.get(conn_id),
            save_conn_fn=self._save_conn,
            guard=guard, cols=cols,
        )

    def remove_graph_edge(self, conn_id: str, frm: str, to: str, source: str) -> int:
        return self.graph_store.remove_graph_edge(conn_id, frm, to, source, self._save_conn)

    def pin_graph_edge(self, conn_id: str, frm: str, to: str, frm_col: str | None = None,
                       to_col: str | None = None) -> int:
        """红边保留：给匹配正式边打 pinned 标记（人工决策资产）。"""
        return self.graph_store.pin_edge(conn_id, frm, to, frm_col, to_col, self._save_conn)

    def confirm_graph_edges(self, conn_id: str, from_table: str | None = None,
                            to_table: str | None = None, from_col: str | None = None,
                            to_col: str | None = None) -> int:
        """确认 draft 边：无参=全部；只给表=按表双向批量；给全列对=精确单边（P1-3）。"""
        return self.graph_store.confirm_graph_edges(
            conn_id, from_table, self._save_conn,
            to_table=to_table, from_col=from_col, to_col=to_col)

    def reject_graph_edges(self, conn_id: str, from_table: str | None = None,
                           to_table: str | None = None, from_col: str | None = None,
                           to_col: str | None = None) -> int:
        return self.graph_store.reject_graph_edges(
            conn_id, from_table, self._save_conn,
            to_table=to_table, from_col=from_col, to_col=to_col)

    def apply_diff_removals(self, conn_id: str) -> int:
        """P1-2 确认生效时移除未被保留(pin)的删除建议边。"""
        return self.graph_store.apply_diff_removals(
            conn_id, self.semantic_store._schema.get(conn_id), self._save_conn)

    def llm_graph_edges(self, conn_id: str) -> list[dict[str, Any]]:
        self.ensure_loaded(conn_id)
        return self.graph_store.llm_graph_edges(conn_id)

    def excluded_tables(self, conn_id: str) -> list[str]:
        return self.graph_store.excluded_tables(conn_id)

    def set_table_excluded(self, conn_id: str, table: str, excluded: bool) -> None:
        self.graph_store.set_table_excluded(conn_id, table, excluded, self._save_conn)

    def graph_layout(self, conn_id: str) -> dict[str, dict[str, Any]]:
        return self.graph_store.graph_layout(conn_id, self.semantic_store._tables)

    def set_layout(self, conn_id: str, layout: dict[str, Any]) -> int:
        return self.graph_store.set_layout(conn_id, layout, self.semantic_store._tables, self._save_conn)

    def hop_sql(self, conn_id: str, table: str, hops: int = 2) -> str:
        return self._storage(conn_id).hop_sql(table, hops)

    def vec_topn_sql(self, conn_id: str, k: int = 10) -> str:
        return self._storage(conn_id).vec_topn_sql(k)

    def _neighbors(self, conn_id: str) -> dict[str, set[str]]:
        return self.graph_store._neighbors(conn_id)

    def _neighbors_for(self, conn_id: str, table: str) -> list[dict[str, Any]]:
        return self.graph_store._neighbors_for(conn_id, table)

    # ---------- 公开方法：语义相关 ----------
    def annotate_drafts(self, conn_id: str, items: list[dict[str, Any]]) -> int:
        return self.semantic_store.annotate_drafts(conn_id, items, self._save_conn)

    async def annotate_drafts_async(self, conn_id: str, items: list[dict[str, Any]]) -> int:
        """构建热路径：批量注释落库 + 落盘挪线程（同步写会卡事件循环）。"""
        return await asyncio.to_thread(
            self.semantic_store.annotate_drafts, conn_id, items, self._save_conn
        )


    async def confirm(self, conn_id: str, table: str | None = None, column: str | None = None,
                      table_only: bool = False) -> int:
        return await self.semantic_store.confirm(
            conn_id, table, column,
            save_conn_fn=self._save_conn,
            reembed_fn=self._reembed_tables,
            table_only=table_only,
        )

    async def confirm_all(self, conn_id: str) -> dict[str, int]:
        # 2026-09 修订：先归档当前生效字段文本（提案提升前的旧值，供字段回溯），再确认
        archived = self.build_service._archive_current_fields(conn_id, self._storage)
        # 只提升提案不打重嵌：受影响表的向量由下方 _embed_tables(affected) 一次性完成
        # （2026-09 优化：只嵌受影响表，不再全库重嵌——500 表库每次确认省数百次嵌入调用）
        affected: list[str] = []
        n_docs = await self.semantic_store.confirm(
            conn_id, None, None,
            save_conn_fn=self._save_conn,
            reembed_fn=None,
            collect_affected=affected,
        )
        n_tags = 0
        for name in list(self.semantic_store._tags.get(conn_id, {}).keys()):
            if self.confirm_tag(conn_id, name):
                n_tags += 1
        # P1-2：先移除未被保留(pin)的删除建议边（红边「确认后移除」），再确认 draft 边
        self.apply_diff_removals(conn_id)
        n_edges = self.confirm_graph_edges(conn_id)
        self.graph_store.clear_diff_base(conn_id)
        self.semantic_store.clear_round(conn_id)  # 审核完成 → 清本轮对比区
        await self._embed_tables(conn_id, set(affected))
        # P1-9：确认后重建向量索引——此前只更新 _table_vec 不重建 vstore，
        # 检索/路由仍用确认前的旧向量评分（新画像、draft→confirmed 加分不生效）
        self._rebuild_vstore(conn_id)
        self._storage(conn_id).bump_kb_version()
        await self._save_conn_async(conn_id)
        return {"docs": n_docs, "tags": n_tags, "edges": n_edges, "archived": archived,
                "version": self.current_version(conn_id)}

    def clear(self, conn_id: str) -> None:
        """取消构建/失败后清理半成品内存（不落盘）。"""
        self.semantic_store.clear(conn_id)
        self._auto.pop(conn_id, None)  # P1-6：AI 文档一并清理，is_built 不再恒真
        self.graph_store._graph.pop(conn_id, None)
        self.graph_store._excluded.pop(conn_id, None)
        self.graph_store._llm_graph_edges.pop(conn_id, None)
        self.graph_store.clear_diff_base(conn_id)  # 取消后清图 diff 基线，避免陈旧红边
        self.retrieval_service._table_vec.pop(conn_id, None)
        self.retrieval_service._vstore.pop(conn_id, None)
        self.retrieval_service._artifact_fingerprint.pop(conn_id, None)
        self._schema_fingerprint_map.pop(conn_id, None)
        self._synced_at.pop(conn_id, None)
        self.concept_store._concepts.pop(conn_id, None)
        self.filter_store._filters.pop(conn_id, None)
        self.fewshot_store._items.pop(conn_id, None)
        self.build_service._annotation_cache.pop(conn_id, None)

    def clear_tags(self, conn_id: str) -> int:
        return self.semantic_store.clear_tags(conn_id, self._save_conn)

    def upsert_tags(self, conn_id: str, tags: list[dict[str, Any]]) -> int:
        return self.semantic_store.upsert_tags(conn_id, tags, self._save_conn)

    def create_tag(self, conn_id: str, name: str, description: str = "", color: str = "") -> bool:
        return self.semantic_store.create_tag(conn_id, name, description, color, self._save_conn)

    def update_tag(self, conn_id: str, old_name: str, new_name: str | None = None,
                   description: str | None = None, color: str | None = None) -> bool:
        return self.semantic_store.update_tag(conn_id, old_name, new_name, description, color, self._save_conn)

    def assign_table_tags(self, conn_id: str, table: str, names: list[str], merge: bool = False) -> int:
        return self.semantic_store.assign_table_tags(conn_id, table, names, self._save_conn, merge=merge)

    def confirm_tag(self, conn_id: str, name: str) -> bool:
        return self.semantic_store.confirm_tag(conn_id, name, self._save_conn)

    def reject_tag(self, conn_id: str, name: str) -> bool:
        return self.semantic_store.reject_tag(conn_id, name, self._save_conn)

    def tags(self, conn_id: str) -> dict[str, Any]:
        self.ensure_loaded(conn_id)
        return self.semantic_store.tags(conn_id)

    def confirmed_tags(self, conn_id: str) -> list[str]:
        self.ensure_loaded(conn_id)  # P3：重启后先恢复内存态，避免首轮丢标签
        return self.semantic_store.confirmed_tags(conn_id)

    def pending_counts(self, conn_id: str) -> dict[str, int]:
        self.ensure_loaded(conn_id)
        semantic_counts = self.semantic_store.pending_counts(conn_id)
        return {
            **semantic_counts,
            "llm_graph_draft": len(self.graph_store._llm_graph_edges.get(conn_id, [])),
        }

    def has_confirmed_content(self, conn_id: str) -> bool:
        return self.semantic_store.has_confirmed_content(conn_id)

    def annotate(
        self,
        conn_id: str,
        table: str | None,
        column: str | None,
        note: str,
        title: str | None = None,
        kind: str = "note",
        tags: list[str] | None = None,
    ) -> KnowledgeDoc:
        return self.semantic_store.annotate(conn_id, table, column, note, title, kind, tags, self._save_conn)

    def list_docs(self, conn_id: str, table: str | None = None) -> list[KnowledgeDoc]:
        return self.semantic_store.list_docs(conn_id, table, self.ensure_loaded, self._auto)

    def delete_user_doc(self, conn_id: str, doc_id: str) -> bool:
        return self.semantic_store.delete_user_doc(conn_id, doc_id, self._save_conn)

    def table_card(self, conn_id: str, table: str) -> dict[str, Any] | None:
        return self.semantic_store.table_card(conn_id, table, self.ensure_loaded, self._synced_at.get(conn_id, ""))

    def table_cards(self, conn_id: str) -> list[dict[str, Any]]:
        return self.semantic_store.table_cards(conn_id, self.ensure_loaded, self._synced_at.get(conn_id, ""))

    def samples(self, conn_id: str) -> dict[str, dict[str, list[Any]]]:
        self.ensure_loaded(conn_id)
        return self.semantic_store.samples(conn_id)

    def field_history(self, conn_id: str, table: str, column: str) -> list[dict[str, Any]]:
        self.ensure_loaded(conn_id)
        return self.semantic_store.field_history(conn_id, table, column, self._storage)

    def apply_field_history(self, conn_id: str, table: str, column: str, history_id: int) -> bool:
        self.ensure_loaded(conn_id)
        return self.semantic_store.apply_field_history(conn_id, table, column, history_id, self._storage, self._save_conn)

    async def edit_table_knowledge(
        self, conn_id: str, table: str,
        table_comment: str | None = None,
        column_comments: list[dict[str, Any]] | None = None,
        vector_text: str | None = None,
    ) -> dict[str, Any]:
        return await self.semantic_store.edit_table_knowledge(
            conn_id, table, table_comment, column_comments, vector_text,
            self._save_conn, self._reembed_tables,
        )

    async def reject(self, conn_id: str, table: str, column: str | None = None) -> int:
        return await self.semantic_store.reject(conn_id, table, column, self._save_conn, self._reembed_tables)

    async def reject_comment(self, conn_id: str, table: str, column: str | None = None) -> int:
        return await self.semantic_store.reject_comment(conn_id, table, column, self._save_conn, self._reembed_tables)

    async def discard_drafts(self, conn_id: str) -> dict[str, int]:
        """「放弃本轮」：清除全部提案（当前生效知识不动，无需回滚）。"""
        r = await self.build_service.discard_drafts(self, conn_id)
        self.semantic_store.clear_round(conn_id)  # 放弃 → 清本轮对比区
        return r
    def round_table_baseline(self, conn_id: str, table: str) -> dict[str, Any] | None:
        """单表旧版知识（审核页对比层"旧"侧）。"""
        self.ensure_loaded(conn_id)
        return self.semantic_store.round_table_baseline(conn_id, table)
    def round_meta(self, conn_id: str) -> dict[str, Any]:
        """本轮对比元信息（diff + 标签全集 + 注释失败表），overview 透传。"""
        self.ensure_loaded(conn_id)
        r = self.semantic_store.get_round(conn_id)
        return {"diff": r.get("diff"), "tags_new": r.get("tags_new") or [],
                "failed_tables": r.get("failed_tables") or []}
    def synced_at(self, conn_id: str) -> str:
        return self._synced_at.get(conn_id, "")

    # ---------- 公开方法：检索相关 ----------
    def is_built(self, conn_id: str) -> bool:
        """工件存在且版本未作废（P0-E：v1 旧工件存储层按空处理——这里同步视为未构建，
        KB 端点据此 409 引导重建，而不是对着空内存静默操作）。"""
        if conn_id in self._auto:
            return bool(self._auto[conn_id]) or self._storage(conn_id).is_current()
        return self._storage(conn_id).is_current()

    def is_voided(self, conn_id: str) -> bool:
        """工件存在但版本已作废（存储层按空处理）——P0-E 精确判定：
        注册表可能仍标 ready/pending_review，但内容读不回来。区别于「无工件」
        （历史路径/测试夹具，注册表状态说了算）。"""
        st = self._storage(conn_id)
        return st.exists() and not st.is_current()

    def ensure_loaded(self, conn_id: str) -> None:
        """重启后如有工件但未加载进内存，先恢复（overview/graph 等只读入口调用）。"""
        if conn_id not in self._auto:
            self._load_conn(conn_id)

    async def retrieve(self, conn_id: str, query: str = "", table: str | None = None, k: int = 10,
                       table_whitelist: set[str] | None = None) -> list[Any]:
        self.ensure_loaded(conn_id)
        return await self.retrieval_service.retrieve(
            conn_id, query, table, k,
            tables=self.semantic_store._tables,
            emb=self._emb,
            reembed_fn=self.reembed_if_needed,
            log_usage_fn=self.build_service._log_embedding_usage,
            synthesize_text_fn=self.semantic_store.table_vector_text,
            table_payload_fn=self.semantic_store._table_payload,
            graph=self.graph_store._graph,
            table_whitelist=table_whitelist,
        )

    async def to_context(self, conn_id: str, query: str = "", table: str | None = None, k: int = 8,
                         table_whitelist: set[str] | None = None) -> str:
        self.ensure_loaded(conn_id)
        return await self.retrieval_service.to_context(
            conn_id, query, table, k,
            tables=self.semantic_store._tables,
            emb=self._emb,
            reembed_fn=self.reembed_if_needed,
            log_usage_fn=self.build_service._log_embedding_usage,
            synthesize_text_fn=self.semantic_store.table_vector_text,
            table_payload_fn=self.semantic_store._table_payload,
            graph=self.graph_store._graph,
            table_whitelist=table_whitelist,
        )

    def path_strings(self, conn_id: str, seeds: set[str], hops: int = 2,
                     allowed: set[str] | None = None) -> list[str]:
        """候选 join 路径串（T6/T9）：图作为检索器的输出形态。

        allowed：可选表白名单（候选表封顶后的子集）——路径串只涉及允许表内。
        """
        self.ensure_loaded(conn_id)
        from app.knowledge.graph.traverse import path_strings as _path_strings
        edges = self.graph_store._graph.get(conn_id, {"edges": []}).get("edges", [])
        return _path_strings(edges, set(seeds), hops, allowed=allowed)

    def validate_join(self, conn_id: str, from_table: str, from_col: str,
                      to_table: str, to_col: str) -> bool:
        """图校验（plausibility gate，T6/T9）：join 是否在图里（拦截幻觉 join）。"""
        self.ensure_loaded(conn_id)
        from app.knowledge.graph.traverse import validate_join as _validate_join
        edges = self.graph_store._graph.get(conn_id, {"edges": []}).get("edges", [])
        if not edges:
            return True  # 图未就绪（未确认任何边）：不拦（宁缺勿错，plausibility gate 跳过）
        return _validate_join(edges, from_table, from_col, to_table, to_col)

    # ---------- L3 行为层（T10） ----------

    def record_query_success(self, conn_id: str, sql: str, question: str | None = None) -> dict:
        """查询成功回灌：join 边加权 + few-shot 入库（L3/T10，实现见 behavior/service.py）。"""
        return self.behavior_service.record_query_success(self, conn_id, sql, question)
    def apply_query_log_edges(self, conn_id: str, audit_rows: list[dict]) -> int:
        """审计日志挖掘 → query_log 边追加（L3/T10，实现见 behavior/service.py）。"""
        return self.behavior_service.apply_query_log_edges(self, conn_id, audit_rows)
    async def vector_route_tables(self, conn_id: str, question: str, top_k: int = 6) -> list[tuple[str, float]]:
        self.ensure_loaded(conn_id)
        return await self.retrieval_service.vector_route_tables(self, conn_id, question, top_k)
    def route_tables(self, conn_id: str, tag_names: list[str], hops: int = 2) -> dict[str, Any]:
        self.ensure_loaded(conn_id)
        return self.retrieval_service.route_tables(self, conn_id, tag_names, hops)
    def overview(self, conn_id: str) -> dict[str, Any]:
        return self.retrieval_service.overview(self, conn_id)
    def current_version(self, conn_id: str) -> int:
        return self.build_service.current_version(conn_id, self._storage)

    def needs_sync(self, conn_id: str, schema: dict[str, Any]) -> bool:
        self.ensure_loaded(conn_id)
        return self.build_service.needs_sync(conn_id, schema)

    async def sync(
        self, conn_id: str, schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
    ) -> dict[str, Any]:
        """增量同步入口：指纹对比 → 无变化零副作用；有变化走 incremental_build。"""
        return await self.build_service.sync(self, conn_id, schema, samples, include_samples)
    async def reembed_if_needed(self, conn_id: str) -> bool:
        return await self.build_service.reembed_if_needed(self, conn_id)
    async def _embed_tables(self, conn_id: str, tables: set[str] | None = None,
                            on_progress: Any | None = None, p0: int = 82, p1: int = 95) -> None:
        await self.build_service._embed_tables(self, conn_id, tables, on_progress, p0, p1)
    async def _reembed_tables(self, conn_id: str, table_names: list[str]) -> None:
        await self.build_service._reembed_tables(self, conn_id, table_names)
    async def build(
        self,
        conn_id: str,
        schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        on_progress: Any | None = None,
        include_samples: bool = False,
        enable_ai_annotation: bool = True,
        annotate_mode: str = "diff",
        tag_mode: str = "keep",
    ) -> dict[str, Any]:
        """构建知识库（实现见 build.py）。annotate_mode: diff|full；tag_mode: keep|anchor|fresh。"""
        return await self.build_service.build(
            self, conn_id, schema, samples, on_progress,
            include_samples, enable_ai_annotation, annotate_mode, tag_mode)
    async def incremental_build(
        self, conn_id: str, new_schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        return await self.build_service.incremental_build(
            self, conn_id, new_schema, samples, include_samples, on_progress)

    async def sync(self, conn_id: str, schema: dict[str, Any],
                   samples: dict[str, dict[str, list[Any]]] | None = None,
                   include_samples: bool | None = None,
                   on_progress: Any | None = None) -> dict[str, Any]:
        return await self.build_service.sync(
            self, conn_id, schema, samples, include_samples, on_progress)
