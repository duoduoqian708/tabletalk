"""构建编排：build/incremental/sync/embed"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import time
from typing import Any

from app.core.timeutil import utcnow_iso
from app.knowledge.embedding import HashingEmbedder, make_embedder
from app.knowledge.filters import TableFilter

logger = logging.getLogger(__name__)


class BuildService:
    """构建服务：负责知识库构建、增量同步等"""

    def __init__(self, runtime: Any = None) -> None:
        self._runtime = runtime
        # 构建相关状态
        self._schema_fingerprint_map: dict[str, str] = {}  # conn -> 结构指纹
        self._synced_at: dict[str, str] = {}  # conn -> 最近增量同步时间
        self._stash: dict[str, dict[str, Any]] = {}  # conn -> 当前版本全量备份
        self._auto: dict[str, list[Any]] = {}  # conn -> 自动抽取文档

    def _embedder(self) -> Any:
        if self._runtime is not None:
            s = self._runtime.get()
            return make_embedder(s.embedding_provider, s.embedding_base_url, s.embedding_model, s.embedding_api_key)
        return HashingEmbedder()

    def _log_embedding_usage(self, conn_id: str, operation: str = "build", emb: Any = None) -> None:
        """记录嵌入模型用量到 llm_log（仅 ApiEmbedder 有累计 usage）。"""
        try:
            from app.knowledge.embedding import ApiEmbedder
            if not isinstance(emb, ApiEmbedder):
                return
            usage = emb.total_usage
            if not usage or usage.get("total_tokens", 0) == 0:
                return
            from app.ai.llm_log import LlmCallLog
            from app.config import get_env
            LlmCallLog(get_env().data_dir).log(
                conn_id=conn_id, skill="embedding",
                model=emb.model, provider="embedding_api",
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=0,
            )
        except Exception as e:
            logger.warning("[kb.store] conn=%s 记录嵌入用量失败：%s", conn_id, e)

    def _emb_fingerprint(self) -> str:
        """当前嵌入配置指纹：hash | api:model@base_url。用户更换嵌入模型后指纹变化 → 触发向量重嵌。"""
        if self._runtime is not None:
            s = self._runtime.get()
            if s.embedding_provider == "api" and s.embedding_base_url:
                return f"api:{s.embedding_model}@{s.embedding_base_url}"
        return "hash"

    @staticmethod
    def _schema_fingerprint(schema: dict[str, Any]) -> str:
        """结构指纹：规范化（排序）的 表/列/FK 签名 → sha1。表顺序变化不影响指纹。"""
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
    def _from_schema_subset(schema: dict[str, Any], tables: set[str], conn_id: str = "",
                            from_schema_fn: Any = None) -> list[Any]:
        """按表名子集生成 auto 文档（增量新增/变化表用）。"""
        subset = dict(schema)
        subset["_conn_id"] = conn_id
        subset["tables"] = [t for t in schema.get("tables", []) if t["name"] in tables]
        subset["columns"] = [c for c in schema.get("columns", []) if c["table"] in tables]
        subset["foreign_keys"] = [
            f for f in schema.get("foreign_keys", [])
            if f["table"] in tables or f["ref_table"] in tables
        ]
        return from_schema_fn(subset) if from_schema_fn else []

    def current_version(self, conn_id: str, storage_fn: Any = None) -> int:
        """当前生效版本号（0=未启用过；确认时 +1，放弃不消耗）。"""
        if not storage_fn:
            return 0
        try:
            return storage_fn(conn_id).get_kb_version()
        except Exception:
            return 0

    def _stash_snapshot(self, conn_id: str, tables: dict[str, Any] = None,
                        tags: dict[str, Any] = None, table_tags: dict[str, Any] = None,
                        llm_graph_edges: dict[str, Any] = None,
                        table_vec: dict[str, Any] = None,
                        samples: dict[str, Any] = None,
                        schema: dict[str, Any] = None,
                        excluded: dict[str, Any] = None,
                        graph: dict[str, Any] = None,
                        synced_at: dict[str, Any] = None) -> dict[str, Any] | None:
        """深拷贝当前版本全量（含向量）——放弃回滚源。无内容返回 None。"""
        if not (tables or {}).get(conn_id) and not (tags or {}).get(conn_id) \
                and not (llm_graph_edges or {}).get(conn_id):
            return None
        import copy
        return {
            "tables": {n: tk.to_dict() for n, tk in (tables or {}).get(conn_id, {}).items()},
            "tags": copy.deepcopy((tags or {}).get(conn_id, {})),
            "table_tags": copy.deepcopy((table_tags or {}).get(conn_id, {})),
            "llm_graph_edges": copy.deepcopy((llm_graph_edges or {}).get(conn_id, [])),
            "table_vec": copy.deepcopy((table_vec or {}).get(conn_id, {})),
            "samples": copy.deepcopy((samples or {}).get(conn_id, {})),
            "schema": copy.deepcopy((schema or {}).get(conn_id, {})),
            "excluded": list((excluded or {}).get(conn_id, set())),
            "graph": copy.deepcopy((graph or {}).get(conn_id, {})),
            "synced_at": (synced_at or {}).get(conn_id, ""),
        }

    def _stash_persisted(self, conn_id: str, storage_fn: Any = None) -> bool:
        if not storage_fn:
            return False
        try:
            return storage_fn(conn_id).load_stash() is not None
        except Exception:
            return False

    def _restore_stash(self, conn_id: str, tables: dict[str, Any] = None,
                       tags: dict[str, Any] = None, table_tags: dict[str, Any] = None,
                       llm_graph_edges: dict[str, Any] = None,
                       table_vec: dict[str, Any] = None,
                       samples: dict[str, Any] = None,
                       schema: dict[str, Any] = None,
                       excluded: dict[str, Any] = None,
                       graph: dict[str, Any] = None,
                       synced_at: dict[str, Any] = None,
                       stash: dict[str, Any] = None,
                       storage_fn: Any = None,
                       rebuild_vstore_fn: Any = None) -> bool:
        """从 stash（内存优先，落盘兜底）恢复当前版本；返回是否成功。"""
        if stash is None:
            stash = self._stash.get(conn_id)
        if stash is None and storage_fn:
            try:
                stash = storage_fn(conn_id).load_stash()
            except Exception:
                stash = None
        if not stash:
            return False
        from app.knowledge.store import TableKnowledge  # noqa: PLC0415
        if tables is not None:
            tables[conn_id] = {
                n: TableKnowledge.from_dict(v) for n, v in stash["tables"].items()
            }
        if tags is not None:
            tags[conn_id] = stash["tags"]
        if table_tags is not None:
            table_tags[conn_id] = stash["table_tags"]
        if llm_graph_edges is not None:
            llm_graph_edges[conn_id] = stash["llm_graph_edges"]
        if table_vec is not None:
            table_vec[conn_id] = stash["table_vec"]
        if samples is not None:
            samples[conn_id] = stash["samples"]
        if schema is not None:
            schema[conn_id] = stash["schema"]
        if excluded is not None:
            excluded[conn_id] = set(stash.get("excluded", []))
        if graph is not None:
            graph[conn_id] = stash["graph"]
        if synced_at is not None:
            synced_at[conn_id] = stash.get("synced_at", "")
        if rebuild_vstore_fn:
            rebuild_vstore_fn(conn_id)
        return True

    def _clear_stash(self, conn_id: str, storage_fn: Any = None) -> None:
        self._stash.pop(conn_id, None)
        if storage_fn:
            try:
                storage_fn(conn_id).clear_stash()
            except Exception:
                pass

    def _persist_stash(self, conn_id: str, storage_fn: Any = None) -> None:
        """构建完成落盘草稿时，同步把 stash（当前版本）落盘——pending 期间刷新不丢回滚源。"""
        stash = self._stash.get(conn_id)
        if not stash:
            return
        if storage_fn:
            try:
                storage_fn(conn_id).save_stash(stash)
            except Exception as e:
                logger.warning("[kb.store] conn=%s stash 落盘失败：%s", conn_id, e)

    def _archive_stash(self, conn_id: str, storage_fn: Any = None) -> int:
        """确认启用：stash（当前版本）字段文本归档进 version_archive，N=3 物理裁剪。"""
        stash = self._stash.get(conn_id)
        if stash is None and storage_fn:
            try:
                stash = storage_fn(conn_id).load_stash()
            except Exception:
                stash = None
        if not stash:
            return 0
        if not storage_fn:
            return 0
        storage = storage_fn(conn_id)
        ver = storage.get_kb_version()
        rows: list[dict[str, Any]] = []
        for name, tk in stash["tables"].items():
            rows.append({
                "kind": "table", "table": name, "column": "",
                "payload": {
                    "comment": tk.get("comment", ""), "status": tk.get("status", ""),
                    "ddl": tk.get("ddl", ""), "vector_override": tk.get("vector_override", ""),
                },
            })
            for cname, ci in (tk.get("columns") or {}).items():
                rows.append({
                    "kind": "column", "table": name, "column": cname,
                    "payload": {
                        "comment": ci.get("comment", ""), "values": ci.get("values", ""),
                        "example": ci.get("example", ""), "status": ci.get("status", ""),
                    },
                })
        if not rows:
            return 0
        try:
            storage.archive_fields(utcnow_iso(), ver, rows)
            storage.trim_archive(3)
        except Exception as e:
            logger.warning("[kb.store] conn=%s 历史归档失败：%s", conn_id, e)
            return 0
        return len(rows)

    def needs_sync(self, conn_id: str, schema: dict[str, Any]) -> bool:
        """指纹对比：schema 是否有变化（周期任务/手动检查的零开销预判）。"""
        return self._schema_fingerprint(schema) != self._schema_fingerprint_map.get(conn_id, "")


    # ---------- 迁入门面（R8/T1） ----------
    def _detect_filter_candidates(self, schema: dict[str, Any]) -> list[dict[str, Any]]:
        """S2-1：启发式预标记（宽信号）→ AI 裁决提示用——{table, column, hint, predicate}。"""
        out: list[dict[str, Any]] = []
        from app.knowledge.filters import _SOFT_DELETE_COLS, _TENANT_COLS, _TENANT_NEG
        cols_by_table: dict[str, list[dict]] = {}
        for c in schema.get("columns", []):
            cols_by_table.setdefault(c["table"], []).append(c)
        for tname, cols in cols_by_table.items():
            for c in cols:
                cname = c["name"].lower()
                if cname in _SOFT_DELETE_COLS or any(
                        s in cname for s in _SOFT_DELETE_COLS if len(s) >= 8):
                    pred = f"{c['name']} IS NULL" if cname == "deleted_at" else f"{c['name']} = 0"
                    out.append({"table": tname, "column": c["name"], "hint": "soft_delete",
                                "predicate": pred})
                    break
            for c in cols:
                cname = c["name"].lower()
                if any(s in cname for s in _TENANT_COLS) and not any(
                        s in cname for s in _TENANT_NEG):
                    out.append({"table": tname, "column": c["name"], "hint": "tenant",
                                "predicate": f"{c['name']} = :current_tenant"})
                    break
        return out

    def ingest_filter_candidates(self, facade: Any, conn_id: str, schema: dict[str, Any]) -> int:
        """T8 §8#2：结构检测 → 表级过滤器 draft 候选落库（人工确认后生效）。

        - 同表多条候选（软删除 + 租户）AND 合并为一条（FilterStore 一表一条）
        - confirmed 不降级、谓词不被重建覆盖（人工已确认的保留）
        - 无候选列 → 不落库、返回 0
        """
        fs = facade.filter_store
        merged: dict[str, TableFilter] = {}
        for cand in fs.detect_candidates(schema):
            cur = merged.get(cand.table)
            if cur is None:
                merged[cand.table] = cand
            else:
                cur.predicate = f"{cur.predicate} AND {cand.predicate}"
                if cand.scope == "connection_default" and cur.scope != "connection_default":
                    cur.scope = cand.scope
        n = 0
        for tname, cand in merged.items():
            existing = fs._filters.get(conn_id, {}).get(tname)
            if existing is not None and existing.status == "confirmed":
                continue  # 人工已确认：保留原谓词，不被重建覆盖
            fs.add(conn_id, tname, cand.predicate, scope=cand.scope, status="draft")
            n += 1
        return n

    def ingest_values_candidates(self, facade: Any, conn_id: str, schema: dict[str, Any]) -> int:
        """T7 §9#4：ColumnInfo.values 平铺串 → 概念条目候选（draft，人工确认后生效）。

        概念名取 {table}.{column}（同名不同义列不自动合并，T7 §5#2）；
        同名 upsert 覆盖但 confirmed 状态不降级（T7 §5#4）。
        """
        from app.knowledge.semantic.concepts import Concept, ConceptStore

        tabs = facade.semantic_store._tables.get(conn_id, {})
        n = 0
        for tname, tk in tabs.items():
            for cname, ci in tk.columns.items():
                vals = (ci.values or "").strip()
                if not vals:
                    continue
                entries = ConceptStore.parse_values_to_candidates(vals)
                if not entries:
                    continue
                concept = Concept(
                    name=f"{tname}.{cname}",
                    canonical_enum=entries,
                    members=[{"table": tname, "column": cname, "mapping": "code"}],
                    status="draft", kind="dimension", source="sampling",
                )
                if facade.concept_store.upsert(conn_id, concept, schema):
                    n += 1
        if n:
            logger.info("[concepts] conn=%s values→候选 %d 个概念", conn_id, n)
        return n

    async def sync(
        self, facade, conn_id: str, schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
    ) -> dict[str, Any]:
        """增量同步入口：指纹对比 → 无变化零副作用；有变化走 incremental_build。"""
        facade.ensure_loaded(conn_id)
        if include_samples is None:
            include_samples = bool(facade._runtime and facade._runtime.get().kb_ai_annotation_samples)
        new_fp = facade.build_service._schema_fingerprint(schema)
        old_fp = facade._schema_fingerprint_map.get(conn_id, "")
        if new_fp == old_fp:
            logger.debug("[kb.sync] conn=%s 结构无变化，跳过增量", conn_id)
            return {"changed": False, "fingerprint": new_fp, "tables_added": 0, "tables_removed": 0, "tables_changed": 0}
        if not facade._auto.get(conn_id):
            logger.warning("[kb.sync] conn=%s 无已构建工件，防御性回退全量构建", conn_id)
            return await facade.build(conn_id, schema, samples, include_samples=include_samples)
        result = await facade.incremental_build(conn_id, schema, samples, include_samples=include_samples)
        result["fingerprint"] = new_fp
        logger.info(
            "[kb.sync] conn=%s 增量同步：+表%s -表%s 变更表%s",
            conn_id, result.get("tables_added", 0),
            result.get("tables_removed", 0), result.get("tables_changed", 0),
        )
        return result


    async def reembed_if_needed(self, facade, conn_id: str) -> bool:
        facade.ensure_loaded(conn_id)
        """嵌入配置变化（用户新配/更换嵌入模型）→ 重嵌表级向量（一表一 chunk），返回是否重嵌。"""
        # 运行时引用同步：门面的 _runtime 是权威（可被替换/测试打桩），
        # 子模块持有的是构造时引用，委托前必须对齐，否则配置变更不生效。
        facade.build_service._runtime = facade._runtime
        cur = facade.build_service._emb_fingerprint()
        stored = facade.retrieval_service._artifact_fingerprint.get(conn_id, "")
        facade._emb = facade.build_service._embedder()
        active_vecs = {k: v for k, v in facade.retrieval_service._table_vec.get(conn_id, {}).items() if v}
        dims = {len(v) for v in active_vecs.values()}
        dim_mismatch = len(dims) > 1
        if cur == stored and not dim_mismatch:
            return False
        if not facade.semantic_store._tables.get(conn_id):
            return False
        await facade._embed_tables(conn_id)
        facade.retrieval_service._artifact_fingerprint[conn_id] = cur
        facade._rebuild_vstore(conn_id)
        facade._save_conn(conn_id)
        return True


    async def _embed_tables(self, facade, conn_id: str, tables: set[str] | None = None,
                            on_progress: Any | None = None, p0: int = 82, p1: int = 95) -> None:
        """统一表级嵌入（一表一 chunk）"""
        tabs = facade.semantic_store._tables.get(conn_id, {})
        targets = [t for t in tabs if tables is None or t in tables]
        if not targets:
            return
        vecs: dict[str, list[float]] = {}
        n = len(targets)
        for i, name in enumerate(targets):
            if on_progress:
                on_progress("向量化", p0 + (p1 - p0) * i // max(1, n),
                            f"嵌入 {i + 1}/{n}")
            try:
                vecs[name] = await facade._emb.embed(facade.semantic_store._synthesize_table_text(conn_id, tabs[name]))
            except Exception as e:
                logger.warning("[kb.embed] conn=%s 表级向量失败 table=%s：%s", conn_id, name, e)
                vecs[name] = [0.0]
        if tables is None:
            facade.retrieval_service._table_vec[conn_id] = vecs
        else:
            cur = facade.retrieval_service._table_vec.get(conn_id, {})
            for t in targets:
                cur.pop(t, None)
            facade.retrieval_service._table_vec[conn_id] = {**cur, **vecs}


    async def _reembed_tables(self, facade, conn_id: str, table_names: list[str]) -> None:
        """确认/撤下后受影响表即时重嵌"""
        tabs = facade.semantic_store._tables.get(conn_id, {})
        names = [t for t in dict.fromkeys(table_names or []) if t in tabs]
        if not names:
            return
        try:
            facade._emb = facade.build_service._embedder()
            await facade._embed_tables(conn_id, set(names))
            facade._rebuild_vstore(conn_id)
            facade._save_conn(conn_id)
            logger.info("[kb.store] conn=%s 确认/撤下后重嵌完成：tables=%s", conn_id, ",".join(names))
        except Exception as e:
            logger.warning("[kb.store] conn=%s 确认/撤下后重嵌失败 tables=%s：%s", conn_id, names, e)


    async def discard_drafts(self, facade, conn_id: str) -> dict[str, int]:
        """版本制「放弃」：丢弃本轮草稿，从 stash 恢复当前版本（零回滚逻辑、不重嵌）。"""
        if facade._stash.get(conn_id) is not None or facade.build_service._stash_persisted(conn_id, facade._storage):
            facade.build_service._restore_stash(
                conn_id,
                tables=facade.semantic_store._tables,
                tags=facade.semantic_store._tags,
                table_tags=facade.semantic_store._table_tags,
                llm_graph_edges=facade.graph_store._llm_graph_edges,
                table_vec=facade.retrieval_service._table_vec,
                samples=facade.semantic_store._samples,
                schema=facade.semantic_store._schema,
                excluded=facade.graph_store._excluded,
                graph=facade.graph_store._graph,
                synced_at=facade._synced_at,
                storage_fn=facade._storage,
                rebuild_vstore_fn=facade._rebuild_vstore,
            )
            facade.build_service._clear_stash(conn_id, facade._storage)
            facade._save_conn(conn_id)
            return {"columns": 0, "tables": 0, "tags": 0, "edges": 0}
        tabs = facade.semantic_store._tables.get(conn_id, {})
        n_cols = 0
        n_tables = 0
        for tk in tabs.values():
            if tk.status == "draft":
                tk.status = "none"
                n_tables += 1
            for ci in tk.columns.values():
                if ci.status == "draft":
                    ci.status = "none"
                    n_cols += 1
        # draft 标签移除并解绑
        lib = facade.semantic_store._tags.get(conn_id, {})
        draft_names = {n for n, v in lib.items() if v.get("status") == "draft"}
        for name in draft_names:
            del lib[name]
        if draft_names:
            for t, names in facade.semantic_store._table_tags.get(conn_id, {}).items():
                if draft_names & set(names):
                    facade.semantic_store._table_tags[conn_id][t] = [n for n in names if n not in draft_names]
        # LLM draft 边删除（重建时 LLM 重新提案，不记墓碑）
        pending = facade.graph_store._llm_graph_edges.get(conn_id, [])
        n_edges = len(pending)
        if pending:
            facade.graph_store._llm_graph_edges[conn_id] = []
        if n_cols or n_tables or draft_names or n_edges:
            facade._rebuild_vstore(conn_id)
            facade._save_conn(conn_id)
        return {"columns": n_cols, "tables": n_tables, "tags": len(draft_names), "edges": n_edges}


    async def build(
        self, facade,
        conn_id: str,
        schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        on_progress: Any | None = None,
        include_samples: bool = False,
        enable_ai_annotation: bool = True,
        self_check: bool | None = None,
    ) -> dict[str, Any]:
        """构建知识库"""
        # 保持原有逻辑不变
        facade.build_service._runtime = facade._runtime
        import time
        _t0 = time.monotonic()
        _t_prev = _t0
        _segs: list[tuple[str, float]] = []

        def _seg(label: str) -> None:
            nonlocal _t_prev
            now = time.monotonic()
            el = now - _t_prev
            _segs.append((label, el))
            _t_prev = now
            logger.info("[kb.build] conn=%s 耗时[%s] %.1fs（累计 t+%.1fs）",
                        conn_id, label, el, now - _t0)

        facade._stash[conn_id] = facade.build_service._stash_snapshot(
            conn_id,
            tables=facade.semantic_store._tables,
            tags=facade.semantic_store._tags,
            table_tags=facade.semantic_store._table_tags,
            llm_graph_edges=facade.graph_store._llm_graph_edges,
            table_vec=facade.retrieval_service._table_vec,
            samples=facade.semantic_store._samples,
            schema=facade.semantic_store._schema,
            excluded=facade.graph_store._excluded,
            graph=facade.graph_store._graph,
            synced_at=facade._synced_at,
        )
        facade.semantic_store._tables[conn_id] = {}
        facade.semantic_store._tags.pop(conn_id, None)
        facade.semantic_store._table_tags.pop(conn_id, None)
        facade.graph_store._llm_graph_edges.pop(conn_id, None)
        facade.retrieval_service._table_vec.pop(conn_id, None)
        facade.semantic_store._samples.pop(conn_id, None)
        if on_progress:
            on_progress("发现结构", 5, None)
        schema = dict(schema)
        schema["_conn_id"] = conn_id
        facade._auto[conn_id] = facade.semantic_store._from_schema(schema)
        facade.semantic_store._schema[conn_id] = {
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
        facade.semantic_store._sync_table_shells(conn_id, schema)
        if samples is not None:
            facade.semantic_store._samples[conn_id] = samples
        if on_progress:
            on_progress("发现结构", 10, None)
        _seg("发现结构")

        ai_docs_added = 0
        ai_tags_added = 0
        _llm_edges: list[dict] = []
        if enable_ai_annotation:
            from app.knowledge.annotator import annotate_domain, annotate_tables
            from app.knowledge.ddl_context import (
                ddls_from_schema,
                generate_ddls_all,
                truncate_samples,
            )

            if on_progress:
                on_progress(
                    "AI 正在处理", 0,
                    "含样本取值" if include_samples else "仅结构·未授权采样",
                    phase="annotate", step="per_table", step_index=0, step_total=1,
                )
            try:
                from app.state import get_state as _get_state
                _st = _get_state()
                ddl_map: dict[str, str] = {}
                try:
                    ddl_map = await generate_ddls_all(_st, conn_id)
                except Exception as e:
                    logger.warning(
                        "[kb.build] conn=%s 实时 DDL 获取失败，回退结构快照合成：%s", conn_id, e)
                snap_schema = facade.semantic_store._schema[conn_id]
                for tname, ddl in ddls_from_schema(snap_schema).items():
                    ddl_map.setdefault(tname, ddl)
                for tname, ddl in ddl_map.items():
                    tk = facade.semantic_store._tables.get(conn_id, {}).get(tname)
                    if tk is not None:
                        tk.ddl = ddl
                effective_samples = (
                    truncate_samples(facade.semantic_store._samples.get(conn_id, {})) if include_samples else None
                )
                ai_docs_added = await annotate_tables(
                    _st, conn_id, ddl_map, facade.semantic_store._schema[conn_id],
                    samples=effective_samples, on_progress=on_progress, p0=0, p1=100,
                )
                logger.info("[kb.build] conn=%s 阶段=annotate 完成：items=%s", conn_id, ai_docs_added)
                _seg("阶段一·逐表注释")
            except Exception as e:
                logger.warning("[kb.build] conn=%s 阶段=annotate AI注释异常：%s", conn_id, e)
                if on_progress:
                    on_progress("AI 正在处理", 100, None, phase="annotate")

            tags_error: str | None = None
            graph_error: str | None = None
            filters_error: str | None = None
            ai_tags_added = 0
            facade.clear_tags(conn_id)

            from app.knowledge.annotator import annotate_domain, annotate_graph

            async def _run_tags() -> None:
                nonlocal ai_tags_added, tags_error
                try:
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=facade.semantic_store._schema[conn_id],
                        self_check=self_check, on_progress=on_progress,
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
                    logger.info("[kb.build] conn=%s 阶段=tags 完成：new_tags=%s", conn_id, ai_tags_added)
                except Exception as e:
                    tags_error = str(e) or type(e).__name__
                    logger.warning("[kb.build] conn=%s 阶段=tags 标签提取异常：%s（%s）",
                                   conn_id, tags_error, type(e).__name__)

            async def _run_graph() -> None:
                nonlocal graph_error, _llm_edges
                try:
                    _llm_edges = await annotate_graph(
                        _st, conn_id, facade.semantic_store._schema[conn_id],
                        on_progress=on_progress, self_check=self_check,
                    )
                    logger.info("[kb.build] conn=%s 阶段=graph 完成：llm_edges=%s", conn_id, len(_llm_edges))
                except Exception as e:
                    graph_error = str(e) or type(e).__name__
                    logger.warning("[kb.build] conn=%s 阶段=graph 关系识别异常：%s（%s）",
                                   conn_id, graph_error, type(e).__name__)

            async def _run_filters() -> None:
                nonlocal filters_error
                try:
                    from app.knowledge.annotator import annotate_filters
                    await annotate_filters(
                        _st, conn_id, facade.semantic_store._schema[conn_id],
                        on_progress=on_progress,
                    )
                    logger.info("[kb.build] conn=%s 阶段=filters AI 裁决完成", conn_id)
                except Exception as e:
                    filters_error = str(e) or type(e).__name__
                    logger.warning("[kb.build] conn=%s 阶段=filters 过滤器裁决异常：%s（%s）",
                                   conn_id, filters_error, type(e).__name__)

            if on_progress:
                on_progress("AI 标签提取", 0, None, phase="tags")
                on_progress("AI 关系识别", 0, None, phase="graph")
            async def _run_constants() -> None:
                try:
                    from app.knowledge.annotator import annotate_constants
                    await annotate_constants(
                        _st, conn_id, facade.semantic_store._schema[conn_id],
                        on_progress=on_progress,
                    )
                    logger.info("[kb.build] conn=%s 阶段=constants 识别完成", conn_id)
                except Exception as e:
                    logger.warning("[kb.build] conn=%s 阶段=constants 异常：%s", conn_id, e)

            await asyncio.gather(_run_tags(), _run_graph(), _run_filters(), _run_constants())
            if on_progress:
                on_progress("AI 标签提取", 100,
                            f"领域标签划分失败：{tags_error}（已跳过，图谱继续）" if tags_error else None,
                            phase="tags")
            _seg("阶段二三·并行")

        # 2026-08-31 修订：确定性来源（FK+命名）只产 draft；正式图保留已确认边，
        # 按 schema 过滤失效边；LLM 提案合并进同一 draft 队列（全部待人工确认）
        facade.graph_store.filter_confirmed_by_schema(conn_id, schema)
        drafts = facade.graph_store.build_draft_edges(schema)
        n_det = facade.graph_store.merge_draft_edges(conn_id, drafts)
        n_llm = facade.graph_store.merge_draft_edges(
            conn_id,
            [{**e, "kind": "llm"} for e in _llm_edges],
        )
        logger.info("[kb.build] conn=%s draft 边：确定性 %d 条 + LLM 提案 %d 条（待人工确认）",
                    conn_id, n_det, n_llm)
        _seg("构图")
        facade._emb = facade.build_service._embedder()
        facade.retrieval_service._artifact_fingerprint[conn_id] = facade.build_service._emb_fingerprint()
        facade._schema_fingerprint_map[conn_id] = facade.build_service._schema_fingerprint(schema)
        facade._synced_at[conn_id] = utcnow_iso()
        # T7：values 平铺串 → 概念条目候选（draft，人工确认后生效）
        facade.build_service.ingest_values_candidates(facade, conn_id, schema)
        # T8：结构检测 → 表级过滤器 draft 候选（人工确认后生效）
        facade.build_service.ingest_filter_candidates(facade, conn_id, schema)
        facade._rebuild_vstore(conn_id)
        facade._save_conn(conn_id)
        facade.build_service._persist_stash(conn_id, facade._storage)
        _seg("落盘")
        facade.build_service._log_embedding_usage(conn_id, "build", facade._emb)
        logger.info(
            "[kb.build] conn=%s 链路耗时 %.1fs ｜ %s",
            conn_id, time.monotonic() - _t0,
            " ⇒ ".join(f"{k} {v:.1f}s" for k, v in _segs),
        )
        logger.info(
            "[kb.build] conn=%s build 完成：tables=%s docs=%s ai_items=%s tags=%s graph=%s",
            conn_id, len(facade.semantic_store._tables.get(conn_id, {})), len(facade._auto[conn_id]), ai_docs_added,
            ai_tags_added, len(facade.graph_store._graph.get(conn_id, {}).get("edges", [])),
        )
        return {
            "docs": len(facade._auto[conn_id]),
            "ai_docs_added": ai_docs_added,
            "ai_tags_added": ai_tags_added,
            "graph_edges": len(facade.graph_store._graph.get(conn_id, {}).get("edges", [])),
            "sample_cols": sum(
                len(cols) for cols in facade.semantic_store._samples.get(conn_id, {}).values()
            ),
        }


    async def incremental_build(
        self, facade, conn_id: str, new_schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
    ) -> dict[str, Any]:
        """增量构建：只处理变化表（新增/变更/删除），不重建全部向量。"""
        if include_samples is None:
            include_samples = bool(facade._runtime and facade._runtime.get().kb_ai_annotation_samples)
        old_schema = facade.semantic_store._schema.get(conn_id, {})
        diff = facade.build_service.diff_schema(old_schema, new_schema)
        if facade.build_service.diff_is_empty(diff):
            return {"changed": False, **diff}

        facade.semantic_store._schema[conn_id] = {
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
        facade.semantic_store._sync_table_shells(conn_id, new_schema, drop=set(diff["removed_tables"]))
        if samples:
            merged = {**facade.semantic_store._samples.get(conn_id, {}), **samples}
            for t in diff["removed_tables"]:
                merged.pop(t, None)
            facade.semantic_store._samples[conn_id] = merged
            all_samples = merged
        else:
            all_samples = facade.semantic_store._samples.get(conn_id, {})
            for t in diff["removed_tables"]:
                all_samples.pop(t, None)

        touched = (set(diff["added_tables"]) | set(diff["changed_tables"])
                   | set(diff["added_columns"]) | set(diff["removed_columns"])
                   | set(diff["changed_columns"])
                   | {f[0] for f in diff["added_fks"]} | {f[0] for f in diff["removed_fks"]})
        touched |= {f[2] for f in diff["added_fks"]} | {f[2] for f in diff["removed_fks"]}

        auto = facade._auto.get(conn_id, [])
        now = utcnow_iso()

        removed = set(diff["removed_tables"])
        for d in auto:
            if d.table in removed and not d.archived:
                d.archived = True
                d.updated_at = now
        tv = facade.retrieval_service._table_vec.get(conn_id, {})
        for t in removed:
            tv.pop(t, None)

        rebuild_tables = touched - removed
        docs_added = 0
        if rebuild_tables:
            auto = [d for d in auto if d.table not in rebuild_tables or d.archived]
            new_docs = facade.build_service._from_schema_subset(new_schema, rebuild_tables, conn_id, facade.semantic_store._from_schema)
            for d in new_docs:
                d.updated_at = now
            auto.extend(new_docs)
            docs_added = len(new_docs)
            facade._auto[conn_id] = auto
        else:
            facade._auto[conn_id] = auto

        ai_docs_added = 0
        ai_tags_added = 0
        if rebuild_tables:
            try:
                from app.knowledge.annotator import annotate_domain, annotate_tables
                from app.knowledge.ddl_context import (
                    ddls_from_schema,
                    generate_ddls_all,
                    truncate_samples,
                )
                from app.state import get_state as _get_state
                _st = _get_state()
                try:
                    ddl_map = await generate_ddls_all(_st, conn_id)
                except Exception as e:
                    logger.warning(
                        "[kb.incr] conn=%s 实时 DDL 获取失败，回退结构快照合成：%s", conn_id, e)
                    ddl_map = {}
                for tname, ddl in ddls_from_schema(facade.semantic_store._schema[conn_id]).items():
                    ddl_map.setdefault(tname, ddl)
                for tname, ddl in ddl_map.items():
                    if tname in rebuild_tables:
                        tk = facade.semantic_store._tables.get(conn_id, {}).get(tname)
                        if tk is not None:
                            tk.ddl = ddl
                changed_ddl_map = {t: ddl_map[t] for t in ddl_map if t in rebuild_tables}
                if changed_ddl_map:
                    effective_samples = (
                        truncate_samples(facade.semantic_store._samples.get(conn_id, {})) if include_samples else None
                    )
                    ai_docs_added = await annotate_tables(
                        _st, conn_id, changed_ddl_map, facade.semantic_store._schema[conn_id],
                        samples=effective_samples,
                    )
                if diff["added_tables"]:
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=facade.semantic_store._schema[conn_id],
                        mode="incremental", target_tables=diff["added_tables"],
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
                logger.info("[kb.incr] conn=%s AI注释完成：items=%s new_tags=%s", conn_id, ai_docs_added, ai_tags_added)
            except Exception as e:
                logger.warning("[kb.incr] conn=%s 增量AI注释/标签异常：%s", conn_id, e)

        cleared_tags = facade.graph_store._sync_removed_tables(
            conn_id, set(diff["removed_tables"]),
            facade.semantic_store._table_tags, facade.semantic_store._tags, facade._save_conn
        )
        if cleared_tags:
            logger.info("[kb.incr] conn=%s 删除清理：清理 0 表标签=%s", conn_id, cleared_tags)

        # 2026-08-31 修订：增量只补 draft（确定性来源 + 局部 LLM），正式图按 schema 过滤失效边
        facade.graph_store.filter_confirmed_by_schema(conn_id, new_schema)
        if rebuild_tables:
            facade.graph_store.merge_draft_edges(
                conn_id,
                facade.graph_store.build_draft_edges(new_schema),
                targets=set(rebuild_tables),
            )
        if rebuild_tables:
            try:
                from app.knowledge.annotator import annotate_graph
                from app.state import get_state as _gstate
                _st2 = _gstate()
                incr_edges = await annotate_graph(
                    _st2, conn_id, facade.semantic_store._schema[conn_id],
                    mode="incremental", target_tables=list(rebuild_tables),
                )
                facade.graph_store._upsert_llm_edges(conn_id, set(rebuild_tables), incr_edges)
                logger.info("[kb.incr] conn=%s 增量图谱补边：target=%s 新边=%s",
                            conn_id, len(rebuild_tables), len(incr_edges))
            except Exception as e:
                logger.warning("[kb.incr] conn=%s 增量图谱补边异常：%s", conn_id, e)

        facade._schema_fingerprint_map[conn_id] = facade.build_service._schema_fingerprint(new_schema)
        facade._synced_at[conn_id] = now
        # T8：结构变化 → 表级过滤器 draft 候选随增量刷新（confirmed 保留）
        facade.build_service.ingest_filter_candidates(facade, conn_id, new_schema)
        facade._rebuild_vstore(conn_id)
        facade._save_conn(conn_id)
        logger.info(
            "[kb.incr] conn=%s 增量同步完成：+表%s -表%s 变更表%s docs=%s ai_items=%s",
            conn_id, len(diff["added_tables"]), len(diff["removed_tables"]),
            len(rebuild_tables), docs_added, ai_docs_added,
        )
        return {
            "changed": True,
            "docs_added": docs_added,
            "tables_added": len(diff["added_tables"]),
            "tables_removed": len(diff["removed_tables"]),
            "tables_changed": len(rebuild_tables),
            "cleared_tags": cleared_tags,
            **diff,
        }

