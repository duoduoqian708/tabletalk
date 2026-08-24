"""知识库 v3：结构 + 向量 + 图谱 + AI 草案 + 人工确认。

数据源接入 → build()（结构抽取 + 采样 + 图谱构建 + 向量索引 + 持久化）→
annotate_drafts()（AI 生成中文注释草案）→ confirm()（人工确认/拒绝）→
retrieve()（已确认优先 + 图谱扩展）喂给 AI 上下文。

隐私：采样只存本地（图谱值重叠用）；发送给模型的注释 prompt 是否含样本值由
`kb_ai_annotation_samples` 门控；嵌入默认离线哈希，真语义嵌入由设置选择（可本地）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.knowledge.docs import KnowledgeDoc
from app.knowledge.embedding import Embedder, HashingEmbedder, cosine, make_embedder
from app.knowledge.vectorstore import NumpyVectorStore, VectorChunk, VectorStore

if TYPE_CHECKING:
    from app.core.settings import SettingsStore


class KnowledgeBase:
    def __init__(self, data_dir: Path, runtime: "SettingsStore | None" = None) -> None:
        self._data_dir = data_dir
        self._runtime = runtime
        self._storage_backend = os.environ.get("TABLETALK_KB_STORAGE", "")  # sqlite(默认) | json
        self._storages: dict[str, Any] = {}
        self._emb: Embedder = HashingEmbedder()
        self._auto: dict[str, list[KnowledgeDoc]] = {}
        self._user: dict[str, list[KnowledgeDoc]] = {}
        self._drafts: dict[str, list[KnowledgeDoc]] = {}
        self._samples: dict[str, dict[str, dict[str, list[Any]]]] = {}  # conn -> table -> column -> [values]
        self._graph: dict[str, dict[str, Any]] = {}                       # conn -> {edges}
        self._vec: dict[str, dict[str, list[float]]] = {}                 # conn -> doc_id -> 向量
        self._tags: dict[str, dict[str, dict[str, Any]]] = {}               # conn -> tag名 -> {description,status}
        self._table_tags: dict[str, dict[str, list[str]]] = {}            # conn -> table -> [tag名]
        self._enums: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}  # conn -> table -> column -> [{value,meaning,status}]
        self._schema: dict[str, dict[str, Any]] = {}                      # conn -> 表/列/外键快照（审查视图用）
        self._table_vec: dict[str, dict[str, list[float]]] = {}           # conn -> 表名 -> 表级向量（向量路由用）
        self._artifact_fingerprint: dict[str, str] = {}                   # conn -> 构建时的嵌入指纹
        self._schema_fingerprint_map: dict[str, str] = {}                 # conn -> 结构指纹（增量对比）
        self._edge_tombstones: dict[str, list[dict]] = {}                 # conn -> 用户删除的 overlap 边（不复活）
        self._excluded: dict[str, set[str]] = {}                          # conn -> 图谱视图中移出的表
        self._synced_at: dict[str, str] = {}                              # conn -> 最近增量同步时间
        self._vstore: dict[str, VectorStore] = {}                        # conn -> 统一向量索引（doc+table 归一，collection 区分）
        self._llm_graph_edges: dict[str, list[dict[str, Any]]] = {}     # conn -> LLM 发现的 draft 边（待人工确认）
        self._llm_edge_tombstones: dict[str, list[dict[str, Any]]] = {}  # conn -> 用户拒绝过的 LLM 边（重建不复活）
        self._lock = threading.Lock()

    # ---------- 持久化：存储后端（JsonStorage 回退 / SqliteStorage 标准格式） ----------
    def _storage(self, conn_id: str) -> Any:
        st = self._storages.get(conn_id)
        if st is None:
            from app.knowledge.storage import make_storage
            st = make_storage(self._data_dir, conn_id, self._storage_backend)
            self._storages[conn_id] = st
        return st

    def _load_conn(self, conn_id: str) -> None:
        """从存储后端恢复一个连接的知识库（含旧 JSON artifact 自动迁移）。"""
        if conn_id in self._auto:
            return
        snap = self._storage(conn_id).load()
        if not snap.auto and not snap.drafts and not snap.user and not snap.edges:
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
        self._drafts[conn_id] = _docs_(snap.drafts)
        self._user[conn_id] = _docs_(snap.user)
        self._samples[conn_id] = snap.samples
        self._graph[conn_id] = {"edges": snap.edges}
        self._vec[conn_id] = snap.vec
        self._table_vec[conn_id] = snap.table_vec
        self._tags[conn_id] = snap.tags
        self._table_tags[conn_id] = snap.table_tags
        self._enums[conn_id] = snap.enums
        self._schema[conn_id] = snap.schema
        self._artifact_fingerprint[conn_id] = snap.emb_fingerprint
        self._schema_fingerprint_map[conn_id] = snap.schema_fingerprint
        self._edge_tombstones[conn_id] = snap.edge_tombstones
        self._excluded[conn_id] = set(snap.excluded)
        self._synced_at[conn_id] = snap.synced_at
        self._llm_graph_edges[conn_id] = snap.llm_graph_edges
        self._llm_edge_tombstones[conn_id] = snap.llm_edge_tombstones
        self._rebuild_vstore(conn_id)

    def _save_conn(self, conn_id: str) -> None:
        """全量快照写回存储后端（标准格式/JSON 均在此落盘）。"""
        try:
            from app.knowledge.storage import KbSnapshot
            snap = KbSnapshot(
                auto=[d.to_dict() for d in self._auto.get(conn_id, [])],
                drafts=[d.to_dict() for d in self._drafts.get(conn_id, [])],
                user=[d.to_dict() for d in self._user.get(conn_id, [])],
                samples=self._samples.get(conn_id, {}),
                edges=self._graph.get(conn_id, {"edges": []}).get("edges", []),
                vec=self._vec.get(conn_id, {}),
                table_vec=self._table_vec.get(conn_id, {}),
                tags=self._tags.get(conn_id, {}),
                table_tags=self._table_tags.get(conn_id, {}),
                enums=self._enums.get(conn_id, {}),
                schema=self._schema.get(conn_id, {}),
                emb_fingerprint=self._artifact_fingerprint.get(conn_id, ""),
                schema_fingerprint=self._schema_fingerprint_map.get(conn_id, ""),
                edge_tombstones=self._edge_tombstones.get(conn_id, []),
                excluded=list(self._excluded.get(conn_id, set())),
                synced_at=self._synced_at.get(conn_id, ""),
                llm_graph_edges=self._llm_graph_edges.get(conn_id, []),
                llm_edge_tombstones=self._llm_edge_tombstones.get(conn_id, []),
            )
            self._storage(conn_id).save(snap)
        except Exception:
            pass

    # ---------- 自动抽取 ----------
    @staticmethod
    def _from_schema(schema: dict[str, Any]) -> list[KnowledgeDoc]:
        conn_id = schema.get("_conn_id", "")
        docs: list[KnowledgeDoc] = []
        tables = {t["name"]: t for t in schema.get("tables", [])}
        for tname, tinfo in tables.items():
            body = f"{tname} 表，{tinfo.get('column_count', 0)} 列"
            if tinfo.get("comment"):
                body += f"。表注释：{tinfo['comment']}"
            docs.append(KnowledgeDoc(
                id=f"auto-tbl-{tname}", conn_id=conn_id, kind="table",
                title=f"表 {tname}", body=body, table=tname, tags=[tname, "table"],
            ))
        for c in schema.get("columns", []):
            marks = []
            if c.get("pk"):
                marks.append("主键")
            if c.get("fk"):
                marks.append("外键")
            body = f"{c['table']}.{c['name']} 列，类型 {c.get('type', '')}"
            if marks:
                body += "（" + "、".join(marks) + "）"
            if c.get("comment"):
                body += f"。列注释：{c['comment']}"
            docs.append(KnowledgeDoc(
                id=f"auto-col-{c['table']}-{c['name']}", conn_id=conn_id, kind="column",
                title=f"{c['table']}.{c['name']}", body=body,
                table=c["table"], column=c["name"],
                tags=[c["table"], c["name"], "column"],
            ))
        for fk in schema.get("foreign_keys", []):
            docs.append(KnowledgeDoc(
                id=f"auto-fk-{fk['table']}-{fk['column']}", conn_id=conn_id, kind="fk",
                title=f"外键 {fk['table']}.{fk['column']}",
                body=f"{fk['table']}.{fk['column']} → {fk['ref_table']}.{fk['ref_column']}",
                table=fk["table"], column=fk["column"],
                tags=[fk["table"], fk["column"], "外键", fk["ref_table"]],
            ))
        return docs

    # ---------- 图谱构建 ----------
    # 墓碑匹配：用户删除的 overlap 边（重建/增量不复活）
    @staticmethod
    def _tombstone_key(e: dict) -> tuple[str, str, str, str]:
        # 兼容旧墓碑记录缺 from_col/to_col 的情况（KeyError 曾导致构建崩溃）
        a, b = sorted([(e["from"], e.get("from_col") or ""), (e["to"], e.get("to_col") or "")])
        return (a[0], a[1], b[0], b[1])

    def _is_tombstoned(self, conn_id: str, e: dict) -> bool:
        return self._tombstone_key(e) in {
            self._tombstone_key(t) for t in self._edge_tombstones.get(conn_id, [])
        }

    def _build_graph(self, conn_id: str, schema: dict[str, Any], samples: dict[str, dict[str, list[Any]]]) -> dict[str, Any]:
        edges: list[dict[str, Any]] = []
        # 只画 FK 边（真实外键关系）；overlap 值重叠不上图，仅用于知识库内部检索
        for fk in schema.get("foreign_keys", []):
            e = {
                "from": fk["table"], "from_col": fk["column"],
                "to": fk["ref_table"], "to_col": fk["ref_column"],
                "kind": "fk", "weight": 1.0,
            }
            if not self._is_tombstoned(conn_id, e):
                edges.append(e)
        return {"edges": edges}

    # ---------- 构建 ----------
    def _embedder(self) -> Embedder:
        if self._runtime is not None:
            s = self._runtime.get()
            return make_embedder(s.embedding_provider, s.embedding_base_url, s.embedding_model, s.embedding_api_key)
        return HashingEmbedder()

    def _log_embedding_usage(self, conn_id: str, operation: str = "build") -> None:
        """记录嵌入模型用量到 llm_log（仅 ApiEmbedder 有累计 usage）。"""
        try:
            from app.knowledge.embedding import ApiEmbedder
            if not isinstance(self._emb, ApiEmbedder):
                return
            usage = self._emb.total_usage
            if not usage or usage.get("total_tokens", 0) == 0:
                return
            from app.ai.llm_log import LlmCallLog
            from app.config import get_env
            LlmCallLog(get_env().data_dir).log(
                conn_id=conn_id, skill="embedding",
                model=self._emb.model, provider="embedding_api",
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=0,
            )
        except Exception:
            pass

    def _emb_fingerprint(self) -> str:
        """当前嵌入配置指纹：hash | api:model@base_url。用户更换嵌入模型后指纹变化 → 触发向量重嵌。"""
        if self._runtime is not None:
            s = self._runtime.get()
            if s.embedding_provider == "api" and s.embedding_base_url:
                return f"api:{s.embedding_model}@{s.embedding_base_url}"
        return "hash"

    async def reembed_if_needed(self, conn_id: str) -> bool:
        """嵌入配置变化（用户新配/更换嵌入模型）→ 重嵌文档级与表级向量，返回是否重嵌。

        模型必须用户配置：用户配置了真语义嵌入后，旧 artifact 里哈希时代的向量必须作废重建，
        否则"配了模型却不生效"。只重嵌向量，不重建结构/图谱。
        注意：必须先用当前配置重建嵌入器（self._emb 可能是旧模型实例）。
        """
        cur = self._emb_fingerprint()
        stored = self._artifact_fingerprint.get(conn_id, "")
        # 无条件同步嵌入器到当前配置：进程重启后 _emb 可能仍是默认哈希嵌入器，
        # 即使指纹一致不重嵌，查询向量维度也必须与库中一致（避免 matmul 不匹配）。
        self._emb = self._embedder()
        # 维度自愈：活跃文档向量维度混杂（旧模型残留）也触发重嵌，避免检索 matmul 不匹配。
        # 只统计活跃文档（archived 残留向量不参与检索，不触发重嵌循环）。
        active_ids = {d.id for d in self._docs(conn_id)}
        active_vecs = {k: v for k, v in self._vec.get(conn_id, {}).items() if k in active_ids}
        active_vecs.update(self._table_vec.get(conn_id, {}))
        dims = {len(v) for v in active_vecs.values() if v}
        dim_mismatch = len(dims) > 1
        if cur == stored and not dim_mismatch:
            return False
        schema = self._schema.get(conn_id)
        if schema is None:
            return False
        # 顺带清理归档文档的残留向量（不在活跃文档集合）
        self._vec[conn_id] = {k: v for k, v in self._vec.get(conn_id, {}).items() if k in active_ids}
        await self._embed_docs(conn_id, self._docs(conn_id))
        await self._embed_table_docs(conn_id, schema)
        self._artifact_fingerprint[conn_id] = cur
        self._rebuild_vstore(conn_id)
        self._save_conn(conn_id)
        return True

    async def build(
        self,
        conn_id: str,
        schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        on_progress: Any | None = None,
        include_samples: bool = False,
        enable_ai_annotation: bool = True,
    ) -> dict[str, Any]:
        """构建知识库。on_progress(stage, percent) 可选进度回调（任务化构建用）。

        include_samples: 是否将采样值发给 AI 辅助注释（仅控制 AI 发送，采样始终执行）。
        enable_ai_annotation: 是否执行 AI 注释 + 标签生成（mock provider 时自动降级为伪注释）。
        """
        # 全量重建也遵守历史墓碑（用户删过的 overlap 边不复活）
        if conn_id not in self._edge_tombstones:
            try:
                snap = self._storage(conn_id).load()
                self._edge_tombstones[conn_id] = snap.edge_tombstones or []
            except Exception:
                self._edge_tombstones[conn_id] = []
        if on_progress:
            on_progress("发现结构", 5, None)
        schema = dict(schema)
        schema["_conn_id"] = conn_id
        self._auto[conn_id] = self._from_schema(schema)
        self._schema[conn_id] = {
            "tables": [
                {"name": t["name"], "kind": t.get("kind", "table"),
                 "column_count": t.get("column_count", 0), "comment": t.get("comment", "")}
                for t in schema.get("tables", [])
            ],
            "columns": [
                {"table": c["table"], "name": c["name"], "type": c.get("type", ""),
                 "pk": c.get("pk", False), "fk": c.get("fk", False), "comment": c.get("comment", "")}
                for c in schema.get("columns", [])
            ],
            "foreign_keys": schema.get("foreign_keys", []),
        }
        if samples is not None:
            self._samples[conn_id] = samples
        if on_progress:
            on_progress("发现结构", 10, None)

        # ---- AI 语义增强：逐表注释 + 全局标签 ----
        ai_docs_added = 0
        ai_tags_added = 0
        ai_enums_added = 0
        if enable_ai_annotation:
            from app.knowledge.annotator import annotate_domain, annotate_tables
            from app.knowledge.ddl_context import build_ddl_overview, generate_ddls_all, truncate_samples

            # 阶段一：逐表 AI 处理（on_progress 逐表回调，phase="annotate"）
            if on_progress:
                on_progress("AI 正在处理", 0, None, phase="annotate")
            try:
                from app.state import get_state as _get_state
                _st = _get_state()
                ddl_map = await generate_ddls_all(_st, conn_id)
                # 授权才发送；出网唯一防护是值级截断（用户决策：无列级过滤）
                effective_samples = (
                    truncate_samples(self._samples.get(conn_id, {})) if include_samples else None
                )
                ai_docs_added = await annotate_tables(
                    _st, conn_id, ddl_map, self._schema[conn_id],
                    samples=effective_samples, on_progress=on_progress, p0=0, p1=100,
                )
            except Exception:
                if on_progress:
                    on_progress("AI 正在处理", 100, None, phase="annotate")

            # 阶段二：全局标签提取
            if on_progress:
                on_progress("AI 标签提取", 0, None, phase="tags")
            try:
                overview_text = build_ddl_overview(self._schema[conn_id])
                domain_result = await annotate_domain(
                    _st, conn_id,
                    schema=self._schema[conn_id], ddl_overview=overview_text,
                )
                ai_tags_added = domain_result.get("new_tags", 0)
            except Exception:
                pass
            if on_progress:
                on_progress("AI 标签提取", 100, None, phase="tags")

            # 阶段三：LLM 关系识别
            if on_progress:
                on_progress("AI 关系识别", 0, None, phase="graph")
            try:
                from app.knowledge.annotator import annotate_graph
                from app.knowledge.ddl_context import build_graph_overview
                graph_overview = build_graph_overview(self._schema[conn_id])
                llm_edges = await annotate_graph(
                    _st, conn_id, graph_overview, self._schema[conn_id],
                    on_progress=on_progress,
                )
                # 对比墓碑：用户之前拒绝过的边标记 previously_rejected
                tombstone_keys = {
                    self._llm_edge_key(t) for t in self._llm_edge_tombstones.get(conn_id, [])
                }
                for e in llm_edges:
                    if self._llm_edge_key(e) in tombstone_keys:
                        e["status"] = "previously_rejected"
                self._llm_graph_edges[conn_id] = llm_edges
            except Exception:
                pass
            if on_progress:
                on_progress("AI 关系识别", 100, None, phase="graph")

            # 阶段四：枚举字典（需数据授权；未授权跳过——枚举解释必须发送取值）
            if include_samples:
                if on_progress:
                    on_progress("枚举字典", 0, None, phase="enums")
                try:
                    from app.knowledge.annotator import annotate_enums_core
                    enum_result = await annotate_enums_core(
                        _st, conn_id, self._schema[conn_id],
                        self._samples.get(conn_id, {}),
                    )
                    ai_enums_added = enum_result.get("added", 0)
                except Exception:
                    pass
                if on_progress:
                    on_progress("枚举字典", 100, None, phase="enums")

        # ---- 构图（程序 FK 边） ----
        if on_progress:
            on_progress("构图", 40, None)
        self._graph[conn_id] = self._build_graph(conn_id, schema, self._samples.get(conn_id, {}))
        self._emb = self._embedder()
        self._artifact_fingerprint[conn_id] = self._emb_fingerprint()
        self._schema_fingerprint_map[conn_id] = self._schema_fingerprint(schema)
        self._synced_at[conn_id] = time.strftime("%Y-%m-%dT%H:%M:%S")
        # 向量化（含 AI 生成的 draft 文档）
        if on_progress:
            on_progress("向量化", 45, None)
        await self._embed_docs(conn_id, self._docs(conn_id), on_progress=on_progress, p0=45, p1=75)
        if on_progress:
            on_progress("向量化", 80, None)
        await self._embed_table_docs(conn_id, schema, on_progress=on_progress, p0=80, p1=90)
        if on_progress:
            on_progress("落盘", 95, None)
        self._rebuild_vstore(conn_id)
        self._save_conn(conn_id)
        # 记录嵌入模型用量（ApiEmbedder 累计 usage → llm_log）
        self._log_embedding_usage(conn_id, "build")
        return {
            "docs": len(self._auto[conn_id]),
            "ai_docs_added": ai_docs_added,
            "ai_tags_added": ai_tags_added,
            "enums_added": ai_enums_added,
            "graph_edges": len(self._graph.get(conn_id, {}).get("edges", [])),
            "sample_cols": sum(
                len(cols) for cols in self._samples.get(conn_id, {}).values()
            ),
        }

    # ---------- 增量同步（结构指纹 + diff + 局部重建） ----------
    @staticmethod
    def _schema_fingerprint(schema: dict[str, Any]) -> str:
        """结构指纹：规范化（排序）的 表/列/FK 签名 → sha1。表顺序变化不影响指纹。"""
        import hashlib
        import json as _json

        canon = {
            "tables": sorted((t["name"], t.get("comment", "")) for t in schema.get("tables", [])),
            "columns": sorted(
                (c["table"], c["name"], c.get("type", ""),
                 bool(c.get("pk")), bool(c.get("fk")), c.get("comment", ""))
                for c in schema.get("columns", [])
            ),
            "foreign_keys": sorted(
                (f["table"], f["column"], f["ref_table"], f["ref_column"])
                for f in schema.get("foreign_keys", [])
            ),
        }
        return hashlib.sha1(
            _json.dumps(canon, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def diff_schema(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        """结构 diff：新增/删除表、列变化（签名含类型/PK/FK/注释）、FK 增删。"""
        def _tables(s: dict) -> dict[str, dict]:
            return {t["name"]: t for t in s.get("tables", [])}
        def _cols(s: dict) -> dict[tuple, tuple]:
            return {(c["table"], c["name"]): (c.get("type", ""), bool(c.get("pk")), bool(c.get("fk")), c.get("comment", ""))
                    for c in s.get("columns", [])}
        def _fks(s: dict) -> set[tuple]:
            return {(f["table"], f["column"], f["ref_table"], f["ref_column"])
                    for f in s.get("foreign_keys", [])}

        ot, nt = _tables(old), _tables(new)
        oc, nc = _cols(old), _cols(new)
        of, nf = _fks(old), _fks(new)

        added_tables = sorted(set(nt) - set(ot))
        removed_tables = sorted(set(ot) - set(nt))
        changed_tables = sorted(
            t for t in set(ot) & set(nt)
            if ot[t].get("comment", "") != nt[t].get("comment", "")
        )
        added_cols: dict[str, list[str]] = {}
        removed_cols: dict[str, list[str]] = {}
        changed_cols: dict[str, list[str]] = {}
        for key in sorted(set(nc) - set(oc)):
            added_cols.setdefault(key[0], []).append(key[1])
        for key in sorted(set(oc) - set(nc)):
            removed_cols.setdefault(key[0], []).append(key[1])
        for key in sorted(set(oc) & set(nc)):
            if oc[key] != nc[key]:
                changed_cols.setdefault(key[0], []).append(key[1])
        added_fks = sorted(nf - of)
        removed_fks = sorted(of - nf)
        return {
            "added_tables": added_tables,
            "removed_tables": removed_tables,
            "changed_tables": changed_tables,
            "added_columns": added_cols,
            "removed_columns": removed_cols,
            "changed_columns": changed_cols,
            "added_fks": added_fks,
            "removed_fks": removed_fks,
        }

    @staticmethod
    def diff_is_empty(diff: dict[str, Any]) -> bool:
        return not any(
            diff[k] for k in ("added_tables", "removed_tables", "changed_tables",
                              "added_columns", "removed_columns", "changed_columns",
                              "added_fks", "removed_fks")
        )

    @staticmethod
    def _from_schema_subset(schema: dict[str, Any], tables: set[str], conn_id: str = "") -> list[KnowledgeDoc]:
        """按表名子集生成 auto 文档（增量新增/变化表用）。"""
        subset = dict(schema)
        subset["_conn_id"] = conn_id
        subset["tables"] = [t for t in schema.get("tables", []) if t["name"] in tables]
        subset["columns"] = [c for c in schema.get("columns", []) if c["table"] in tables]
        subset["foreign_keys"] = [
            f for f in schema.get("foreign_keys", [])
            if f["table"] in tables or f["ref_table"] in tables
        ]
        return KnowledgeBase._from_schema(subset)  # 复用生成逻辑（_from_schema 为实例方法，用静态调用）

    async def incremental_build(
        self, conn_id: str, new_schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
    ) -> dict[str, Any]:
        """增量构建：只处理变化表（新增/变更/删除），不重建全部向量。

        - 新增/变化表：重生成 auto 文档（draft/user 不动）+ 重嵌入 + 表级向量
        - 删除表：auto 文档 archived（保留可回溯），移除表级向量与相关边
        - 图：基于新 schema + 合并样本全量重构图（快；墓碑自动遵守）
        - include_samples: 数据授权门控（None=沿用运行时设置 kb_ai_annotation_samples）；
          未授权时变化表不重提枚举、不发样本注释（枚举解释/注释均需发送数据）
        - 返回 diff 摘要
        """
        if include_samples is None:
            include_samples = bool(self._runtime and self._runtime.get().kb_ai_annotation_samples)
        old_schema = self._schema.get(conn_id, {})
        diff = self.diff_schema(old_schema, new_schema)
        if self.diff_is_empty(diff):
            return {"changed": False, **diff}

        # 1. 更新 schema 快照与样本（新表/变化表样本合并）
        self._schema[conn_id] = {
            "tables": [
                {"name": t["name"], "kind": t.get("kind", "table"),
                 "column_count": t.get("column_count", 0), "comment": t.get("comment", "")}
                for t in new_schema.get("tables", [])
            ],
            "columns": [
                {"table": c["table"], "name": c["name"], "type": c.get("type", ""),
                 "pk": c.get("pk", False), "fk": c.get("fk", False), "comment": c.get("comment", "")}
                for c in new_schema.get("columns", [])
            ],
            "foreign_keys": new_schema.get("foreign_keys", []),
        }
        if samples:
            merged = {**self._samples.get(conn_id, {}), **samples}
            # 删除的表样本一并移除（否则 overlap 边残留）
            for t in diff["removed_tables"]:
                merged.pop(t, None)
            self._samples[conn_id] = merged
            all_samples = merged
        else:
            all_samples = self._samples.get(conn_id, {})
            for t in diff["removed_tables"]:
                all_samples.pop(t, None)

        touched = (set(diff["added_tables"]) | set(diff["changed_tables"])
                   | set(diff["added_columns"]) | set(diff["removed_columns"])
                   | set(diff["changed_columns"])
                   | {f[0] for f in diff["added_fks"]} | {f[0] for f in diff["removed_fks"]})
        touched |= {f[2] for f in diff["added_fks"]} | {f[2] for f in diff["removed_fks"]}

        auto = self._auto.get(conn_id, [])
        now = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 2. 删除的表：auto 文档归档 + 移除表级向量 + 移除文档向量
        removed = set(diff["removed_tables"])
        for d in auto:
            if d.table in removed and not d.archived:
                d.archived = True
                d.updated_at = now
        tv = self._table_vec.get(conn_id, {})
        for t in removed:
            tv.pop(t, None)
        if removed:
            vec = self._vec.get(conn_id, {})
            for d in auto:
                if d.table in removed:
                    vec.pop(d.id, None)

        # 3. 新增/变化表：移除旧 auto 文档（该表）→ 重新生成 → 重嵌入
        rebuild_tables = touched - removed
        docs_added = 0
        if rebuild_tables:
            auto = [d for d in auto if d.table not in rebuild_tables or d.archived]
            new_docs = self._from_schema_subset(new_schema, rebuild_tables, conn_id)
            for d in new_docs:
                d.updated_at = now
            auto.extend(new_docs)
            docs_added = len(new_docs)
            self._auto[conn_id] = auto
            # 重嵌入：移除这些文档的旧向量，嵌入新文档
            vec = self._vec.get(conn_id, {})
            for d in new_docs:
                vec.pop(d.id, None)
            await self._embed_docs(conn_id, new_docs)
            # 表级向量：变化表重算
            await self._embed_table_docs_for(conn_id, new_schema, rebuild_tables)
        else:
            self._auto[conn_id] = auto

        # 4. 增量 AI 注释：只为新增/变化表生成 AI 注释（不重做全库）
        ai_docs_added = 0
        ai_tags_added = 0
        ai_enums_added = 0
        if rebuild_tables:
            try:
                from app.knowledge.annotator import annotate_domain, annotate_tables
                from app.knowledge.ddl_context import build_ddl_overview, generate_ddls_all, truncate_samples
                from app.state import get_state as _get_state
                _st = _get_state()
                # DDL 为变化表生成注释
                ddl_map = await generate_ddls_all(_st, conn_id)
                # 只给变化表生成 AI 注释
                changed_ddl_map = {t: ddl_map[t] for t in ddl_map if t in rebuild_tables}
                if changed_ddl_map:
                    # 授权才发送；出网唯一防护是值级截断（与全量构建同语义）
                    effective_samples = (
                        truncate_samples(self._samples.get(conn_id, {})) if include_samples else None
                    )
                    ai_docs_added = await annotate_tables(
                        _st, conn_id, changed_ddl_map, self._schema[conn_id],
                        samples=effective_samples,
                    )
                # 有新表时追加标签
                if diff["added_tables"]:
                    overview_text = build_ddl_overview(self._schema[conn_id])
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=self._schema[conn_id], ddl_overview=overview_text,
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
            except Exception:
                pass
            # 变化表枚举重提（数据授权门控；独立容错不影响注释/标签流程）。
            # samples 用合并后的 all_samples；schema 用新 schema 的变化表子集（只重提变化表）。
            if include_samples:
                try:
                    from app.knowledge.annotator import annotate_enums_core
                    sub_schema = {
                        "tables": [
                            t for t in new_schema.get("tables", [])
                            if t["name"] in rebuild_tables
                        ],
                        "columns": [
                            c for c in self._schema[conn_id].get("columns", [])
                            if c["table"] in rebuild_tables
                        ],
                        "foreign_keys": [],
                    }
                    enum_result = await annotate_enums_core(
                        _st, conn_id, sub_schema, all_samples,
                    )
                    ai_enums_added = enum_result.get("added", 0)
                except Exception:
                    pass
            # 嵌入新增的 AI draft 文档
            if ai_docs_added:
                try:
                    new_drafts = [d for d in self._drafts.get(conn_id, []) if d.source == "ai_draft"]
                    if new_drafts:
                        await self._embed_docs(conn_id, new_drafts)
                except Exception:
                    pass

        # 5. 图谱：新 schema + 合并样本全量重构图（FK 边同步 + 墓碑遵守）
        self._graph[conn_id] = self._build_graph(conn_id, new_schema, all_samples)

        # 5. 指纹与同步时间
        self._schema_fingerprint_map[conn_id] = self._schema_fingerprint(new_schema)
        self._synced_at[conn_id] = now
        self._rebuild_vstore(conn_id)
        self._save_conn(conn_id)
        return {
            "changed": True,
            "docs_added": docs_added,
            "tables_added": len(diff["added_tables"]),
            "tables_removed": len(diff["removed_tables"]),
            "tables_changed": len(rebuild_tables),
            "enums_added": ai_enums_added,
            **diff,
        }

    async def _embed_table_docs_for(self, conn_id: str, schema: dict[str, Any], tables: set[str]) -> None:
        """只重算指定表的表级向量（增量用）。"""
        snap = self._schema.get(conn_id, {})
        columns = snap.get("columns", [])
        table_tags = self._table_tags.get(conn_id, {})
        lib = self._tags.get(conn_id, {})
        vecs = dict(self._table_vec.get(conn_id, {}))
        for t in tables:
            tinfo = next((x for x in snap.get("tables", []) if x.get("name") == t), {})
            cols = [c for c in columns if c.get("table") == t]
            col_txt = "，".join(
                f"{c.get('name')}（{c.get('type')}{'：' + c.get('comment', '') if c.get('comment') else ''}）"
                for c in cols
            )
            confirmed = [n for n in table_tags.get(t, []) if lib.get(n, {}).get("status") == "confirmed"]
            text = f"{t} 表：{col_txt}；注释：{tinfo.get('comment', '')}；领域标签：{'、'.join(confirmed)}"
            try:
                vecs[t] = await self._emb.embed(text)
            except Exception:
                vecs[t] = [0.0]
        self._table_vec[conn_id] = vecs

    def needs_sync(self, conn_id: str, schema: dict[str, Any]) -> bool:
        """指纹对比：schema 是否有变化（周期任务/手动检查的零开销预判）。"""
        self.ensure_loaded(conn_id)
        return self._schema_fingerprint(schema) != self._schema_fingerprint_map.get(conn_id, "")

    async def sync(
        self, conn_id: str, schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
    ) -> dict[str, Any]:
        """增量同步入口：指纹对比 → 无变化零副作用；有变化走 incremental_build。

        include_samples 在进入分支前统一解析 None → 运行时授权设置，
        保证全量回退分支与增量分支门控语义一致。
        """
        self.ensure_loaded(conn_id)
        if include_samples is None:
            include_samples = bool(self._runtime and self._runtime.get().kb_ai_annotation_samples)
        new_fp = self._schema_fingerprint(schema)
        old_fp = self._schema_fingerprint_map.get(conn_id, "")
        if new_fp == old_fp:
            return {"changed": False, "fingerprint": new_fp, "tables_added": 0, "tables_removed": 0, "tables_changed": 0}
        if not self._auto.get(conn_id):
            # 未构建过的连接不应走增量（调用方应保证 ready）；防御性直接全量
            return await self.build(conn_id, schema, samples, include_samples=include_samples)
        result = await self.incremental_build(conn_id, schema, samples, include_samples=include_samples)
        result["fingerprint"] = new_fp
        return result

    async def _embed_table_docs(self, conn_id: str, schema: dict[str, Any],
                                on_progress: Any | None = None, p0: int = 80, p1: int = 90) -> None:
        """每张表一条"表级文档"向量（表名+列名+类型+注释+标签）——供向量路由语义召回。

        与文档级向量互补：文档级管"注释/草案召回"，表级管"问题→表"定位，
        标签覆盖率不足时由向量兜底。构建时算好，持久化进 artifact。
        """
        snap = self._schema.get(conn_id, {})
        tables = snap.get("tables", [])
        columns = snap.get("columns", [])
        if not tables:
            return
        table_tags = self._table_tags.get(conn_id, {})
        lib = self._tags.get(conn_id, {})
        vecs: dict[str, list[float]] = {}
        n = len(tables)
        for i, t in enumerate(tables):
            if on_progress and i % max(1, n // 4) == 0:
                on_progress("向量化", p0 + (p1 - p0) * i // max(1, n), None)
            name = t.get("name", "")
            if not name:
                continue
            cols = [c for c in columns if c.get("table") == name]
            col_txt = "，".join(
                f"{c.get('name')}（{c.get('type')}{'：' + c.get('comment', '') if c.get('comment') else ''}）"
                for c in cols
            )
            confirmed = [n for n in table_tags.get(name, []) if lib.get(n, {}).get("status") == "confirmed"]
            text = f"{name} 表：{col_txt}；注释：{t.get('comment', '')}；领域标签：{'、'.join(confirmed)}"
            try:
                vecs[name] = await self._emb.embed(text)
            except Exception:
                vecs[name] = [0.0]
        self._table_vec[conn_id] = vecs

    async def _embed_docs(self, conn_id: str, docs: list[KnowledgeDoc],
                          on_progress: Any | None = None, p0: int = 40, p1: int = 75) -> None:
        vecs: dict[str, list[float]] = {}
        text_by_id = {d.id: self._doc_text(d) for d in docs}
        n = len(text_by_id)
        for i, (did, text) in enumerate(text_by_id.items()):
            if on_progress and i % max(1, n // 5) == 0:
                on_progress("向量化", p0 + (p1 - p0) * i // max(1, n), None)
            try:
                vecs[did] = await self._emb.embed(text)
            except Exception:
                vecs[did] = [0.0]
        self._vec[conn_id] = {**self._vec.get(conn_id, {}), **vecs}

    # ---------- 统一向量索引（VectorStore，doc+table 归一） ----------
    def _vector_store(self, conn_id: str) -> VectorStore:
        vs = self._vstore.get(conn_id)
        if vs is None:
            vs = NumpyVectorStore()
            self._vstore[conn_id] = vs
            self._rebuild_vstore(conn_id)
        return vs

    def _rebuild_vstore(self, conn_id: str) -> None:
        """把 _vec（文档级）+ _table_vec（表级）归一为带 collection/metadata 的 chunk 索引。

        doc chunk:  collection=doc, metadata={source, status, kind} → 可过滤已确认/来源
        table chunk: collection=table, metadata={table} → 问题→表路由
        向量维度统一对齐主维度（旧 artifact 可能有嵌入失败残留的 [0.0] 短向量）。
        """
        from collections import Counter

        chunks: list[VectorChunk] = []
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        fp = self._artifact_fingerprint.get(conn_id, "")
        # 主维度：出现最多的向量长度（doubao=2048 / 哈希=256）
        all_vecs = [
            v for v in list(self._vec.get(conn_id, {}).values()) + list(self._table_vec.get(conn_id, {}).values())
            if v
        ]
        main_dim = Counter(len(v) for v in all_vecs).most_common(1)[0][0] if all_vecs else 256

        def _aligned(vec: list[float]) -> list[float]:
            if len(vec) == main_dim:
                return vec
            return (vec[:main_dim] + [0.0] * main_dim)[:main_dim]

        for d in self._docs(conn_id):
            vec = self._vec.get(conn_id, {}).get(d.id)
            if not vec:
                continue
            chunks.append(VectorChunk(
                id=d.id, collection="doc",
                text=f"{d.title} {d.body}",
                metadata={"source": d.source, "status": d.status, "kind": d.kind},
                vector=_aligned(vec), fingerprint=fp, updated_at=d.updated_at or now,
            ))
        snap = self._schema.get(conn_id, {})
        for tname, vec in self._table_vec.get(conn_id, {}).items():
            cols = [c for c in snap.get("columns", []) if c.get("table") == tname]
            col_txt = " ".join(f"{c.get('name', '')} {c.get('comment', '')}" for c in cols)
            chunks.append(VectorChunk(
                id=tname, collection="table",
                text=f"{tname} {col_txt}",
                metadata={"table": tname},
                vector=_aligned(vec), fingerprint=fp, updated_at=now,
            ))
        self._vstore[conn_id] = NumpyVectorStore()
        self._vstore[conn_id].set_chunks(chunks)

    # ---------- 向量 ----------
    @staticmethod
    def _doc_text(d: KnowledgeDoc) -> str:
        return f"{d.title or ''} {d.body or ''} {' '.join(d.tags or [])}"

    async def _ensure_vectors(self, conn_id: str) -> dict[str, list[float]]:
        vecs = self._vec.setdefault(conn_id, {})
        missing = [d for d in self._docs(conn_id) if d.id not in vecs]
        if missing:
            await self._embed_docs(conn_id, missing)
        return self._vec[conn_id]

    # ---------- 检索 ----------
    def _docs(self, conn_id: str) -> list[KnowledgeDoc]:
        """全部活跃文档（过滤 archived——已删除表的归档文档不参与检索/向量/列表）。"""
        return [
            d for d in (
                self._auto.get(conn_id, [])
                + self._drafts.get(conn_id, [])
                + self._user.get(conn_id, [])
            )
            if not d.archived
        ]

    def _neighbors(self, conn_id: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for e in self._graph.get(conn_id, {}).get("edges", []):
            out.setdefault(e["from"].lower(), set()).add(e["to"].lower())
            out.setdefault(e["to"].lower(), set()).add(e["from"].lower())
        return out

    def is_built(self, conn_id: str) -> bool:
        return conn_id in self._auto or self._storage(conn_id).exists()

    def ensure_loaded(self, conn_id: str) -> None:
        """重启后如有工件但未加载进内存，先恢复（overview/graph 等只读入口调用）。"""
        if conn_id not in self._auto:
            self._load_conn(conn_id)

    async def retrieve(self, conn_id: str, query: str = "", table: str | None = None, k: int = 10) -> list[KnowledgeDoc]:
        if conn_id not in self._auto:
            self._load_conn(conn_id)  # 重启后未重新构建也能用上次的知识
        await self.reembed_if_needed(conn_id)  # 嵌入模型变化 → 向量重嵌（否则检索维度不匹配）
        docs = self._docs(conn_id)
        if not docs:
            return []
        vecs = await self._ensure_vectors(conn_id)
        tokens = [t for t in re.split(r"[\s,，。；;：:、/\\|()（）]+", (query or "").lower()) if t]
        q = (query or "").lower()
        tgt = (table or "").lower()
        qvec = await self._emb.embed(query) if query else None

        def kw_score(d: KnowledgeDoc) -> float:
            blob = (d.title + " " + d.body + " " + " ".join(d.tags)).lower()
            s = float(sum(1 for t in tokens if t in blob))
            if q and q in blob:
                s += 1.0
            if tgt:
                if d.table and d.table.lower() == tgt:
                    s += 3.0
                elif tgt in blob:
                    s += 2.0
            return s

        vec_scores = self._vector_store(conn_id).scores_all(qvec) if qvec is not None else {}
        base: dict[str, float] = {}
        for d in docs:
            s = kw_score(d)
            if qvec is not None:
                s += 2.0 * vec_scores.get(d.id, 0.0)
            # 已确认（auto/user）优先于 AI 草案
            if d.status == "confirmed":
                s += 1.5
            base[d.id] = s

        # 图谱扩展：相邻表高分 → 本表加分（FK + 值重叠边）
        neighbors = self._neighbors(conn_id)
        by_table: dict[str, list[str]] = {}
        for d in docs:
            if d.table:
                by_table.setdefault(d.table.lower(), []).append(d.id)
        final = dict(base)
        for d in docs:
            if not d.table:
                continue
            best = 0.0
            for nt in neighbors.get(d.table.lower(), ()):
                best = max(best, max((base.get(i, 0.0) for i in by_table.get(nt, ())), default=0.0))
            if best > 0:
                final[d.id] += 0.35 * best

        scored = sorted(docs, key=lambda d: final.get(d.id, 0.0), reverse=True)
        if not any(final.get(d.id, 0.0) > 0 for d in docs):
            scored = docs
        # 记录检索阶段的嵌入用量
        self._log_embedding_usage(conn_id, "retrieve")
        return scored[:k]

    async def to_context(self, conn_id: str, query: str = "", table: str | None = None, k: int = 8) -> str:
        """转成给 AI 的上下文文本（结构+标注，无行数据）。"""
        docs = await self.retrieve(conn_id, query, table, k)
        if not docs:
            return ""
        lines = ["【知识库】"]
        for d in docs:
            mark = "" if d.status == "confirmed" else "（AI 草案，待确认）"
            lines.append(f"- [{d.kind}]{mark} {d.title}: {d.body}")
        return "\n".join(lines)

    # ---------- AI 草案 + 人工确认 ----------
    def annotate_drafts(self, conn_id: str, items: list[dict[str, Any]]) -> int:
        """保存 AI 生成的注释草案（status=draft），待人工确认。items: [{table,column,comment}]"""
        existing = {d.id for d in self._drafts.get(conn_id, [])}
        added = 0
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for it in items:
            table = it.get("table")
            column = it.get("column")
            comment = (it.get("comment") or "").strip()
            if not table or not comment:
                continue
            if column:
                did = f"ai-{table}-{column}"
                title = f"{table}.{column}"
                kind = "column"
            else:
                did = f"ai-tbl-{table}"
                title = f"表 {table}"
                kind = "table"
            if did in existing:
                continue
            self._drafts.setdefault(conn_id, []).append(KnowledgeDoc(
                id=did, conn_id=conn_id, kind=kind, title=title, body=comment,
                table=table, column=column, tags=[table, column or "", "注释"],
                source="ai_draft", status="draft", updated_at=now,
            ))
            existing.add(did)
            added += 1
        if added:
            self._save_conn(conn_id)
        return added

    def confirm(self, conn_id: str, table: str | None = None, column: str | None = None) -> int:
        """人工确认草案 → 权威。不指定 table 则确认全部草案。"""
        drafts = self._drafts.get(conn_id, [])
        targets = [
            d for d in drafts
            if (table is None or d.table == table) and (column is None or d.column == column)
        ]
        for d in targets:
            d.status = "confirmed"
            d.source = "user"
        if targets:
            self._save_conn(conn_id)
        return len(targets)

    def confirm_all(self, conn_id: str) -> dict[str, int]:
        """确认闸（构建后一键启用）：批量确认全部草案文档 + 全部 draft 标签 + 全部 draft 枚举。

        标签确认后才参与"问题→选表"路由；文档确认后检索优先。
        kb_status → ready 由调用方（api 层）负责。
        """
        n_docs = self.confirm(conn_id)
        n_tags = 0
        for name in list(self._tags.get(conn_id, {}).keys()):
            if self.confirm_tag(conn_id, name):
                n_tags += 1
        n_enums = 0
        for table, cols in self._enums.get(conn_id, {}).items():
            for column in list(cols.keys()):
                n_enums += self.confirm_enum(conn_id, table, column)
        return {"docs": n_docs, "tags": n_tags, "enums": n_enums}

    def clear(self, conn_id: str) -> None:
        """取消构建/失败后清理半成品内存（不落盘）。"""
        for d in (self._auto, self._user, self._drafts, self._samples, self._graph,
                  self._vec, self._tags, self._table_tags, self._enums, self._schema,
                  self._table_vec, self._artifact_fingerprint, self._vstore,
                  self._schema_fingerprint_map, self._edge_tombstones, self._synced_at,
                  self._llm_graph_edges, self._llm_edge_tombstones):
            d.pop(conn_id, None)

    # ---------- LLM 图谱 draft 边管理 ----------
    def llm_graph_edges(self, conn_id: str) -> list[dict[str, Any]]:
        """LLM 发现的 draft 边列表（供审查页展示）。"""
        return list(self._llm_graph_edges.get(conn_id, []))

    @staticmethod
    def _llm_edge_key(e: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
        """LLM 边的去重键（用于墓碑对比）。"""
        return (e.get("from_table", ""), e.get("to_table", ""),
                e.get("from_col"), e.get("to_col"))

    def confirm_graph_edges(self, conn_id: str, from_table: str | None = None) -> int:
        """确认 LLM draft 边 → 写入正式图谱（from_table=None 则确认全部）。

        如果边之前被拒绝过（在墓碑中），恢复时同时移除墓碑记录。
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
        # 移除墓碑记录（用户恢复了之前拒绝的边）
        tombstones = self._llm_edge_tombstones.get(conn_id, [])
        if tombstones:
            self._llm_edge_tombstones[conn_id] = [
                t for t in tombstones if self._llm_edge_key(t) not in confirmed_keys
            ]
        # 写入正式图谱
        edges = self._graph.setdefault(conn_id, {"edges": []})["edges"]
        existing = {(e["from"], e["to"], e.get("from_col"), e.get("to_col")) for e in edges}
        added = 0
        for e in to_confirm:
            key = (e.get("from_table"), e.get("to_table"), e.get("from_col"), e.get("to_col"))
            if key in existing:
                continue
            edges.append({
                "from": e["from_table"], "from_col": e.get("from_col"),
                "to": e["to_table"], "to_col": e.get("to_col"),
                "kind": "semantic", "weight": 1.0,
                "reason": e.get("reason", ""),
            })
            added += 1
        if added:
            self._save_conn(conn_id)
        return added

    def reject_graph_edges(self, conn_id: str, from_table: str | None = None) -> int:
        """拒绝 LLM draft 边：从 draft 列表移除 + 记入墓碑（重建时识别但标记）。"""
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
        # 记入墓碑（去重）
        tombstones = self._llm_edge_tombstones.setdefault(conn_id, [])
        existing_keys = {self._llm_edge_key(t) for t in tombstones}
        for e in to_reject:
            if self._llm_edge_key(e) not in existing_keys:
                tombstones.append({
                    "from_table": e["from_table"], "from_col": e.get("from_col"),
                    "to_table": e["to_table"], "to_col": e.get("to_col"),
                })
        self._save_conn(conn_id)
        return len(to_reject)

    def synced_at(self, conn_id: str) -> str:
        return self._synced_at.get(conn_id, "")

    def pending_counts(self, conn_id: str) -> dict[str, int]:
        """待确认数（确认闸 UI 用）：草案文档 + draft 标签 + draft 枚举 + LLM draft 边。"""
        enum_drafts = sum(
            1 for cols in self._enums.get(conn_id, {}).values()
            for entries in cols.values()
            for e in entries if e.get("status") == "draft"
        )
        return {
            "draft_docs": len(self._drafts.get(conn_id, [])),
            "draft_tags": sum(1 for v in self._tags.get(conn_id, {}).values() if v.get("status") == "draft"),
            "draft_enums": enum_drafts,
            "llm_graph_draft": len(self._llm_graph_edges.get(conn_id, [])),
        }

    def reject(self, conn_id: str, doc_id: str) -> bool:
        drafts = self._drafts.get(conn_id, [])
        before = len(drafts)
        self._drafts[conn_id] = [d for d in drafts if d.id != doc_id]
        if len(self._drafts[conn_id]) != before:
            self._save_conn(conn_id)
            return True
        return False

    def reject_comment(self, conn_id: str, table: str, column: str | None = None) -> int:
        """按表/列拒绝草案注释（审查页用）。"""
        drafts = self._drafts.get(conn_id, [])
        targets = [d for d in drafts if d.table == table and d.column == column]
        kept = [d for d in drafts if d not in targets]
        if len(kept) != len(drafts):
            self._drafts[conn_id] = kept
            self._save_conn(conn_id)
        return len(targets)

    # ---------- 领域标签（每库一套，draft→人工确认） ----------
    def upsert_tags(self, conn_id: str, tags: list[dict[str, Any]]) -> int:
        """AI 提案的标签入库（新标签为 draft，已存在不重复）。tags: [{name, description}]"""
        lib = self._tags.setdefault(conn_id, {})
        added = 0
        for t in tags:
            name = (t.get("name") or "").strip()
            if not name or name in lib:
                continue
            lib[name] = {"description": (t.get("description") or "").strip(), "status": "draft"}
            added += 1
        if added:
            self._save_conn(conn_id)
        return added

    def create_tag(self, conn_id: str, name: str, description: str = "") -> bool:
        """人工新建标签（直接 confirmed，立即可用可路由）。"""
        name = (name or "").strip()
        lib = self._tags.setdefault(conn_id, {})
        if not name or name in lib:
            return False
        lib[name] = {"description": (description or "").strip(), "status": "confirmed"}
        self._save_conn(conn_id)
        return True

    def update_tag(self, conn_id: str, old_name: str, new_name: str | None = None,
                   description: str | None = None) -> bool:
        """人工编辑标签：改名（同步所有表绑定）/改描述。"""
        lib = self._tags.setdefault(conn_id, {})
        if old_name not in lib:
            return False
        nn = (new_name or "").strip() if new_name is not None else old_name
        if nn and nn != old_name and nn in lib:
            raise ValueError(f"标签 {nn} 已存在")
        if description is not None:
            lib[old_name]["description"] = description.strip()
        if nn and nn != old_name:
            lib[nn] = lib.pop(old_name)
            for t, names in self._table_tags.get(conn_id, {}).items():
                self._table_tags[conn_id][t] = [nn if n == old_name else n for n in names]
        self._save_conn(conn_id)
        return True

    def assign_table_tags(self, conn_id: str, table: str, names: list[str]) -> int:
        """把标签绑定到表（去重保序）；库中不存在的标签自动补为 draft（否则 overview 不可见、无法确认）。"""
        keep = [n for n in dict.fromkeys(names) if n]
        lib = self._tags.setdefault(conn_id, {})
        for n in keep:
            if n not in lib:
                lib[n] = {"description": "", "status": "draft"}
        self._table_tags.setdefault(conn_id, {})[table] = keep
        self._save_conn(conn_id)
        return len(keep)

    def confirm_tag(self, conn_id: str, name: str) -> bool:
        """人工确认标签 → 进入可路由标签库。"""
        lib = self._tags.get(conn_id, {})
        if name not in lib:
            return False
        lib[name]["status"] = "confirmed"
        self._save_conn(conn_id)
        return True

    def reject_tag(self, conn_id: str, name: str) -> bool:
        """拒绝标签：从库移除 + 从所有表上解除。"""
        lib = self._tags.get(conn_id, {})
        if name not in lib:
            return False
        del lib[name]
        for t, names in self._table_tags.get(conn_id, {}).items():
            if name in names:
                self._table_tags[conn_id][t] = [n for n in names if n != name]
        self._save_conn(conn_id)
        return True

    def tags(self, conn_id: str) -> dict[str, Any]:
        lib = self._tags.get(conn_id, {})
        return {
            "library": [
                {"name": n, "description": v.get("description", ""), "status": v.get("status", "draft")}
                for n, v in lib.items()
            ],
            "tables": self._table_tags.get(conn_id, {}),
        }

    def confirmed_tags(self, conn_id: str) -> list[str]:
        return [n for n, v in self._tags.get(conn_id, {}).items() if v.get("status") == "confirmed"]

    # ---------- 枚举（列级取值字典：value -> meaning，draft→人工确认） ----------
    def annotate_enums(self, conn_id: str, items: list[dict[str, Any]]) -> int:
        """AI 提案的列级枚举字典入库（status=draft）。items: [{table, column, entries:[{value, meaning}]}]。
        (table, column, value) 去重，已存在（任意状态）的 value 跳过。"""
        conn = self._enums.setdefault(conn_id, {})
        added = 0
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for it in items:
            table = it.get("table")
            column = it.get("column")
            entries = it.get("entries") or []
            if not table or not column or not entries:
                continue
            col = conn.setdefault(table, {}).setdefault(column, [])
            seen = {(e.get("value")) for e in col}
            for e in entries:
                value = e.get("value")
                if value is None or value == "":
                    continue
                if value in seen:
                    continue
                seen.add(value)
                col.append({
                    "value": value,
                    "meaning": (e.get("meaning") or "").strip(),
                    "status": "draft",
                    "updated_at": now,
                })
                added += 1
        if added:
            self._save_conn(conn_id)
        return added

    def enum_drafts(self, conn_id: str) -> list[dict[str, Any]]:
        """枚举 draft 列表（按列聚合），供审阅队列。"""
        out: list[dict[str, Any]] = []
        for table, cols in self._enums.get(conn_id, {}).items():
            for column, entries in cols.items():
                drafts = [e for e in entries if e.get("status") == "draft"]
                if drafts:
                    out.append({"table": table, "column": column, "entries": drafts})
        return out

    def confirm_enum(self, conn_id: str, table: str, column: str) -> int:
        """确认某列全部 draft 枚举 → confirmed。"""
        col = self._enums.get(conn_id, {}).get(table, {}).get(column)
        if not col:
            return 0
        n = 0
        for e in col:
            if e.get("status") == "draft":
                e["status"] = "confirmed"
                n += 1
        if n:
            self._save_conn(conn_id)
        return n

    def reject_enum(self, conn_id: str, table: str, column: str) -> int:
        """拒绝某列全部 draft 枚举 → 移除。"""
        cols = self._enums.get(conn_id, {}).get(table)
        if not cols or column not in cols:
            return 0
        before = len(cols[column])
        cols[column] = [e for e in cols[column] if e.get("status") != "draft"]
        if not cols[column]:
            del cols[column]
        removed = before - len(cols[column])
        if removed:
            self._save_conn(conn_id)
        return removed

    def save_enum(self, conn_id: str, table: str, column: str, value: str, meaning: str) -> bool:
        """编辑某枚举值的 meaning（确认前人工修正）。"""
        col = self._enums.get(conn_id, {}).get(table, {}).get(column)
        if not col:
            return False
        for e in col:
            if e.get("value") == value:
                e["meaning"] = (meaning or "").strip()
                e["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._save_conn(conn_id)
                return True
        return False

    def _fk_adj(self, conn_id: str) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {}
        for e in self._graph.get(conn_id, {}).get("edges", []):
            if e["kind"] != "fk":
                continue
            adj.setdefault(e["from"], set()).add(e["to"])
            adj.setdefault(e["to"], set()).add(e["from"])
        return adj

    def expand_tables(self, conn_id: str, seeds: set[str], hops: int = 2) -> set[str]:
        """沿 FK 多跳扩展种子表 → 连通子图（供标签/向量融合路由共用）。"""
        adj = self._fk_adj(conn_id)
        picks = set(seeds)
        frontier = set(seeds)
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for t in frontier:
                nxt |= adj.get(t, set())
            nxt -= picks
            if not nxt:
                break
            picks |= nxt
            frontier = nxt
        return picks

    async def vector_route_tables(self, conn_id: str, question: str, top_k: int = 6) -> list[tuple[str, float]]:
        """向量通道：问题向量 × 表级文档向量 → top-K 表（语义召回，不依赖标签覆盖率）。

        离线 HashingEmbedder 只桥接表面重叠，因此叠加词面加权作为底线：
        表名/注释/列名与问题同词 → 加分（配 API 真语义 embedder 时语义分数自动更强）。
        """
        await self.reembed_if_needed(conn_id)  # 嵌入模型变化 → 向量重嵌（维度一致）
        vecs = self._table_vec.get(conn_id, {})
        if not vecs or not (question or "").strip():
            return []
        try:
            qvec = await self._emb.embed(question)
        except Exception:
            qvec = None
        ql = (question or "").lower()
        snap = self._schema.get(conn_id, {})
        col_blob = {t.get("name", ""): " ".join(
            f"{c.get('name', '')} {c.get('comment', '')}"
            for c in snap.get("columns", []) if c.get("table") == t.get("name")
        ) for t in snap.get("tables", [])}
        base_vec = self._vector_store(conn_id).scores_all(qvec, collection="table") if qvec is not None else {}
        scored: list[tuple[str, float]] = []
        for table in vecs.keys():
            score = base_vec.get(table, 0.0)
            # 词面加权：表名 / 中文词 / 列名出现在问题或反之中
            blob = f"{table} {col_blob.get(table, '')}".lower()
            if table.lower() in ql:
                score += 0.6
            for tok in re.findall(r"[\u4e00-\u9fff]{2,}", ql):
                if tok in blob:
                    score += 0.35
            for tok in re.findall(r"[a-z_]{3,}", ql):
                if tok in blob:
                    score += 0.2
            if score > 0.1:
                scored.append((table, round(score, 4)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def route_tables(self, conn_id: str, tag_names: list[str], hops: int = 2) -> dict[str, Any]:
        """意图→标签→候选表：打这些标签的表 + 沿 FK 多跳覆盖的表 → 候选子图。

        这是检索的核心：避免全表扫描，准确圈定表范围。
        只认已确认的标签——draft 标签不参与路由（不变式）。
        """
        confirmed = set(self.confirmed_tags(conn_id))
        tag_set = {t for t in tag_names if t in confirmed}
        if not tag_set:
            return {"tables": [], "edges": [], "seeded": 0}
        picks: set[str] = set()
        for table, names in self._table_tags.get(conn_id, {}).items():
            if set(names) & tag_set:
                picks.add(table)
        picks = self.expand_tables(conn_id, picks, hops)

        sub_edges = [
            e for e in self._graph.get(conn_id, {}).get("edges", [])
            if e["from"] in picks and e["to"] in picks
        ]
        return {"tables": sorted(picks), "edges": sub_edges, "seeded": len(tag_set)}

    # ---------- 标注（用户手写） ----------
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
        t = title or (f"{table}.{column}" if column else (table or "全局"))
        doc = KnowledgeDoc(
            id=f"usr-{uuid.uuid4().hex[:8]}",
            conn_id=conn_id,
            kind=kind,
            title=t,
            body=note,
            table=table,
            column=column,
            tags=tags or [table or "", column or ""],
            source="user",
            status="confirmed",
            updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        self._user.setdefault(conn_id, []).append(doc)
        self._save_conn(conn_id)
        return doc

    def list_docs(self, conn_id: str, table: str | None = None) -> list[KnowledgeDoc]:
        docs = self._docs(conn_id)
        if table:
            docs = [d for d in docs if d.table == table]
        return docs

    def delete_user_doc(self, conn_id: str, doc_id: str) -> bool:
        """删除一条用户手写文档（仅 usr- 前缀可删）。"""
        if not doc_id.startswith("usr-"):
            return False
        users = self._user.get(conn_id, [])
        before = len(users)
        self._user[conn_id] = [d for d in users if d.id != doc_id]
        if len(self._user[conn_id]) != before:
            self._save_conn(conn_id)
            return True
        return False

    # ---------- 审查视图 ----------
    def overview(self, conn_id: str) -> dict[str, Any]:
        """人工审查页的数据：表/列注释解析（含草案与确认状态）+ 领域标签 + 图谱 + 采样统计。"""
        snap = self._schema.get(conn_id, {})
        auto = {d.id: d for d in self._auto.get(conn_id, [])}
        drafts = {d.id: d for d in self._drafts.get(conn_id, [])}
        users = {d.id: d for d in self._user.get(conn_id, [])}

        def comment_for(table: str, column: str | None) -> dict[str, Any]:
            # 优先级：用户手写 > AI 已确认 > AI 草案 > 结构注释
            candidates = [
                d for d in list(users.values()) + list(drafts.values()) + list(auto.values())
                if d.table == table and d.column == column and d.kind in ("column", "table", "note")
            ]
            if not candidates:
                return {"text": "", "status": "none", "source": ""}
            best = max(candidates, key=lambda d: (d.status == "confirmed", d.source == "user"))
            return {"text": best.body, "status": best.status, "source": best.source}

        def table_tags(table: str) -> list[dict[str, Any]]:
            lib = self._tags.get(conn_id, {})
            return [
                {"name": n, "status": lib.get(n, {}).get("status", "draft")}
                for n in self._table_tags.get(conn_id, {}).get(table, []) if n in lib
            ]

        tables = []
        for t in snap.get("tables", []):
            c = comment_for(t["name"], None)
            tables.append({
                "name": t["name"],
                "kind": t.get("kind", "table"),
                "column_count": t.get("column_count", 0),
                "comment": c["text"], "comment_status": c["status"],
                "tags": table_tags(t["name"]),
            })

        columns = []
        for c in snap.get("columns", []):
            m = comment_for(c["table"], c["name"])
            columns.append({
                "table": c["table"], "name": c["name"],
                "type": c.get("type", ""), "pk": c.get("pk", False), "fk": c.get("fk", False),
                "comment": m["text"], "status": m["status"],
            })

        return {
            "tables": tables,
            "columns": columns,
            "graph": {
                "edges": self._graph.get(conn_id, {"edges": []}).get("edges", []),
                "excluded": self.excluded_tables(conn_id),
                "llm_draft_edges": self._llm_graph_edges.get(conn_id, []),
            },
            "tags": self.tags(conn_id),
            "enums": self.enum_drafts(conn_id),
            "draft_count": len(self._drafts.get(conn_id, [])),
            "tag_draft_count": sum(1 for v in self._tags.get(conn_id, {}).values() if v.get("status") == "draft"),
            "enum_draft_count": sum(
                1 for cols in self._enums.get(conn_id, {}).values()
                for entries in cols.values()
                for e in entries if e.get("status") == "draft"
            ),
            "sample_cols": sum(len(cols) for cols in self._samples.get(conn_id, {}).values()),
            "embedding_provider": (
                self._runtime.get().embedding_provider if self._runtime else "hash"
            ),
        }

    def hop_sql(self, conn_id: str, table: str, hops: int = 2) -> str:
        """k-hop 标准递归 CTE 查询（SqliteStorage 提供；审计/服务化/企业版复用）。"""
        return self._storage(conn_id).hop_sql(table, hops)

    def vec_topn_sql(self, conn_id: str, k: int = 10) -> str:
        """向量 top-N 标准 SQL 查询（vec0 虚拟表，sqlite-vec 可用时）。"""
        return self._storage(conn_id).vec_topn_sql(k)

    def graph(self, conn_id: str) -> dict[str, Any]:
        g = self._graph.get(conn_id, {"edges": []})
        return {**g, "llm_draft_edges": self._llm_graph_edges.get(conn_id, [])}

    # ---------- 图谱编辑（持久化到知识库） ----------
    def excluded_tables(self, conn_id: str) -> list[str]:
        """图谱视图中已被移出的表（不影响审查页的表列表）。"""
        return sorted(self._excluded.get(conn_id, set()))

    def set_table_excluded(self, conn_id: str, table: str, excluded: bool) -> None:
        s = self._excluded.setdefault(conn_id, set())
        if excluded:
            s.add(table)
        else:
            s.discard(table)
        self._save_conn(conn_id)

    def add_graph_edge(self, conn_id: str, frm: str, to: str, kind: str,
                       frm_col: str | None = None, to_col: str | None = None,
                       weight: float | None = None) -> dict[str, Any]:
        """新增一条图谱边（user 为用户手动连线；fk/overlap 为恢复结构/取值边）。"""
        if kind not in ("fk", "overlap", "user"):
            raise ValueError("kind 必须是 fk|overlap|user")
        tables = {t["name"] for t in self._schema.get(conn_id, {}).get("tables", [])}
        if frm not in tables or to not in tables:
            raise ValueError("未知表名")
        edges = self._graph.setdefault(conn_id, {"edges": []})["edges"]
        existing = next((e for e in edges
                        if e["from"] == frm and e["to"] == to and e.get("kind") == kind), None)
        if existing:
            return existing
        e = {"from": frm, "from_col": frm_col, "to": to, "to_col": to_col,
             "kind": kind, "weight": weight, "shared": None}
        edges.append(e)
        self._save_conn(conn_id)
        return e

    def remove_graph_edge(self, conn_id: str, frm: str, to: str, kind: str) -> int:
        """删除一条图谱边。删除结构/取值派生边时记入 tombstone，避免重建复活。"""
        edges = self._graph.get(conn_id, {"edges": []})["edges"]
        before = len(edges)
        edges[:] = [e for e in edges
                    if not (e["from"] == frm and e["to"] == to and e.get("kind") == kind)]
        removed = before - len(edges)
        if removed and kind in ("overlap", "fk"):
            self._edge_tombstones.setdefault(conn_id, []).append(
                {"from": frm, "to": to, "kind": kind})
        if removed:
            self._save_conn(conn_id)
        return removed

    def samples(self, conn_id: str) -> dict[str, dict[str, list[Any]]]:
        return self._samples.get(conn_id, {})

    def _neighbors_for(self, conn_id: str, table: str) -> list[dict[str, Any]]:
        """某表的图谱邻居（供图谱可视化高亮）。"""
        t = table.lower()
        return [
            e for e in self._graph.get(conn_id, {}).get("edges", [])
            if e["from"].lower() == t or e["to"].lower() == t
        ]
