"""构建编排：build/incremental/sync/embed"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import os
import time
from typing import Any

from app.core.timeutil import utcnow_iso
from app.knowledge.embedding import make_embedder
from app.knowledge.filters import TableFilter

logger = logging.getLogger(__name__)


class BuildService:
    """构建服务：负责知识库构建、增量同步等"""

    def __init__(self, runtime: Any = None) -> None:
        self._runtime = runtime
        # 构建相关状态
        self._schema_fingerprint_map: dict[str, str] = {}  # conn -> 结构指纹
        self._synced_at: dict[str, str] = {}  # conn -> 最近增量同步时间
        self._annotation_cache: dict[str, dict[str, dict]] = {}  # conn -> 表名 -> {input_hash, items, created_at}

    def _embedder(self) -> Any:
        if self._runtime is not None:
            s = self._runtime.get()
            return make_embedder(s.embedding_provider, s.embedding_base_url, s.embedding_model, s.embedding_api_key)
        return make_embedder("", "", "", "")

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
        """当前嵌入配置指纹：api:model@base_url#合成版本 或 none。

        用户更换嵌入模型 或 合成算法升级（SYNTH_VERSION 变化）→ 指纹变化 → 触发向量重嵌。
        """
        from app.knowledge.semantic.store import SYNTH_VERSION  # noqa: PLC0415
        if self._runtime is not None:
            s = self._runtime.get()
            if s.embedding_provider and s.embedding_base_url:
                return f"api:{s.embedding_model}@{s.embedding_base_url}#{SYNTH_VERSION}"
        return f"none#{SYNTH_VERSION}"

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
    def _table_col_fingerprint(schema: dict[str, Any], table: str) -> str | None:
        """表级列指纹（rename 检测用）：列名/类型/主外键签名（不含注释/表注释）。

        指纹相同 → 结构完全一致 → 视为同一表改名，继承旧表知识。
        返回 None 表示该表无列（视图或空壳），不参与 rename 匹配。
        """
        cols = sorted(
            (c.get("name", ""), c.get("type", ""), bool(c.get("pk")), bool(c.get("fk")))
            for c in schema.get("columns", []) if c.get("table") == table
        )
        if not cols:
            return None
        return hashlib.sha1(
            _json.dumps(cols, ensure_ascii=False).encode()
        ).hexdigest()

    @classmethod
    def match_renames(cls, old_schema: dict[str, Any], new_schema: dict[str, Any],
                      removed: set[str], added: set[str]) -> list[dict[str, str]]:
        """removed ↔ added 一对一列指纹匹配（rename 启发式）。

        多候选歧义（一张旧表匹配多个新表）→ 放弃该旧表匹配（保守：当删+建处理）。
        """
        removed_fps: dict[str, str | None] = {t: cls._table_col_fingerprint(old_schema, t) for t in removed}
        added_fps: dict[str, str | None] = {t: cls._table_col_fingerprint(new_schema, t) for t in added}
        by_fp: dict[str, list[str]] = {}
        for t, fp in added_fps.items():
            if fp:
                by_fp.setdefault(fp, []).append(t)
        renames: list[dict[str, str]] = []
        used_new: set[str] = set()
        for old_t in sorted(removed):
            fp = removed_fps.get(old_t)
            if not fp:
                continue
            cands = [n for n in by_fp.get(fp, []) if n not in used_new]
            if len(cands) != 1:
                continue  # 歧义或无匹配 → 保持删+建语义
            used_new.add(cands[0])
            renames.append({"from": old_t, "to": cands[0]})
        return renames

    @staticmethod
    def _assert_schema_usable(schema: dict[str, Any]) -> None:
        """P0-C 空 schema 守卫：结构发现零表 = 上游异常（权限/方言/连接），拒绝构建。

        宁缺勿错——增量/全量共用；照常构建会把全部已确认边判失效、清光表知识后落盘。
        """
        if not schema.get("tables"):
            raise ValueError(
                "结构发现返回 0 张表（可能是权限/方言/连接问题），已拒绝构建以保护既有知识库；"
                "请检查连接后重试")

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
    def _touched_from_diff(diff: dict[str, Any]) -> set[str]:
        """结构 diff → touched 表集合（新增/变更/列变化/FK 端点；不含 rename 对消）。"""
        touched = (set(diff["added_tables"]) | set(diff["changed_tables"])
                   | set(diff["added_columns"]) | set(diff["removed_columns"])
                   | set(diff["changed_columns"])
                   | {f[0] for f in diff["added_fks"]} | {f[0] for f in diff["removed_fks"]}
                   | {f[2] for f in diff["added_fks"]} | {f[2] for f in diff["removed_fks"]})
        touched -= set(diff["removed_tables"])
        return touched

    @classmethod
    def compute_touched(cls, old_schema: dict[str, Any], new_schema: dict[str, Any],
                        ) -> tuple[set[str], dict[str, Any]]:
        """diff 模式 touched 集合 + 结构 diff（rename 未对消；调用方应用 rename 继承后再 adjust）。"""
        diff = cls.diff_schema(old_schema, new_schema)
        return cls._touched_from_diff(diff), diff

    @staticmethod
    def adjust_diff_for_renames(diff: dict[str, Any], renames: list[dict[str, str]]) -> dict[str, Any]:
        """rename 对消：diff 中 removed/added/changed 键同步清理。

        FK 增删对里的改名项对消（列未变，同一 FK 以新名重现 = 未变）：
        removed 项翻新名后与 added 求交，双方都清。
        """
        renamed_map = {r["from"]: r["to"] for r in renames}
        renamed_old = {r["from"] for r in renames}
        renamed_new = {r["to"] for r in renames}
        diff["removed_tables"] = [t for t in diff["removed_tables"] if t not in renamed_old]
        diff["added_tables"] = [t for t in diff["added_tables"] if t not in renamed_new]
        if renamed_map:
            _flip = lambda f: (renamed_map.get(f[0], f[0]), f[1], renamed_map.get(f[2], f[2]), f[3])
            flipped_removed = {_flip(f): f for f in diff["removed_fks"]}
            cancel = {tuple(f) for f in diff["added_fks"] if tuple(f) in flipped_removed}
            diff["added_fks"] = [f for f in diff["added_fks"] if tuple(f) not in cancel]
            diff["removed_fks"] = [f for f in diff["removed_fks"] if _flip(f) not in cancel]
        diff["removed_columns"] = {k: v for k, v in diff["removed_columns"].items() if k not in renamed_old}
        diff["added_columns"] = {k: v for k, v in diff["added_columns"].items() if k not in renamed_new}
        diff["changed_columns"] = {k: v for k, v in diff["changed_columns"].items()
                                   if k not in renamed_old and k not in renamed_new}
        return diff

    def apply_renames(self, facade: Any, conn_id: str, renames: list[dict[str, str]]) -> None:
        """rename 知识继承：表知识/样本/向量/标签绑定/图边/注释缓存/baseline 整体改挂新表名。

        全量 diff 模式与增量同步共用（行为一致，不再各写一份）。
        """
        _tables_map = facade.semantic_store._tables.get(conn_id, {})
        _samples_map = facade.semantic_store._samples.get(conn_id, {})
        _tv_map = facade.retrieval_service._table_vec.get(conn_id, {})
        _tt_map = facade.semantic_store._table_tags.get(conn_id, {})
        _ann_map = self._annotation_cache.get(conn_id, {})
        baseline = (facade.semantic_store.get_round(conn_id).get("baseline") or {}).get("tables") or {}
        for r in renames:
            old_t, new_t = r["from"], r["to"]
            tk = _tables_map.get(old_t)
            if tk is not None:
                tk.name = new_t  # 实体名同步改写（旧实现只挪 map 键，合成文本/卡片仍带旧表名）
                _tables_map[new_t] = _tables_map.pop(old_t)
            if old_t in _samples_map:
                _samples_map[new_t] = _samples_map.pop(old_t)
            if old_t in _tv_map:
                _tv_map[new_t] = _tv_map.pop(old_t)
            if old_t in _tt_map:
                _tt_map[new_t] = _tt_map.pop(old_t)
            if old_t in _ann_map:
                _ann_map[new_t] = _ann_map.pop(old_t)
            if old_t in baseline:
                baseline[new_t] = baseline.pop(old_t)  # 对比层旧侧以新表名可查
            # auto docs 改挂新表名（知识字段随 TableKnowledge 走，docs 只换归属）
            for d in facade._auto.get(conn_id, []):
                if d.table == old_t:
                    d.table = new_t
            facade.graph_store.rename_table(conn_id, old_t, new_t)

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

    def _archive_current_fields(self, conn_id: str, storage_fn: Any = None) -> int:
        """确认启用前：归档当前生效字段文本（提案提升前的旧值，供字段回溯）。

        取代旧的 stash 归档（2026-09 修订：当前知识不再让位，提升前直接读当前值）。
        """
        from app.state import get_state as _gst  # noqa: PLC0415 - 延迟导入取 tables

        try:
            tables = _gst().knowledge.semantic_store._tables.get(conn_id, {})
        except Exception:
            tables = {}
        if not tables:
            return 0
        if not storage_fn:
            return 0
        storage = storage_fn(conn_id)
        ver = storage.get_kb_version()
        rows: list[dict[str, Any]] = []
        for name, tk in tables.items():
            has_content = bool(tk.comment or tk.vector_override)  # ddl 是结构，非用户内容，不触发归档
            if has_content:
                rows.append({
                    "kind": "table", "table": name, "column": "",
                    "payload": {
                        "comment": tk.comment, "status": tk.status,
                        "ddl": tk.ddl, "vector_override": tk.vector_override,
                    },
                })
            for cname, ci in tk.columns.items():
                if not (ci.comment or ci.values or ci.example):
                    continue  # 空字段不入档（首轮确认 archived=0）
                rows.append({
                    "kind": "column", "table": name, "column": cname,
                    "payload": {
                        "comment": ci.comment, "values": ci.values,
                        "example": ci.example, "status": ci.status,
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

        读取链：confirmed values → proposed_values（2026-09 优化：增量场景新表的
        取值对照还是提案，一并纳入候选，产出仍为 draft——新表当轮即有概念候选）。
        概念名取 {table}.{column}（同名不同义列不自动合并，T7 §5#2）；
        同名 upsert 覆盖但 confirmed 状态不降级（T7 §5#4）。
        """
        from app.knowledge.semantic.concepts import Concept, ConceptStore

        tabs = facade.semantic_store._tables.get(conn_id, {})
        n = 0
        for tname, tk in tabs.items():
            for cname, ci in tk.columns.items():
                vals = (ci.values or "").strip() or (getattr(ci, "proposed_values", "") or "").strip()
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

    @staticmethod
    def _schema_subset(schema: dict[str, Any], tables: set[str]) -> dict[str, Any]:
        """schema 子集（增量补齐 filters/constants 等阶段用：只喂 touched 表）。"""
        sub = dict(schema)
        sub["tables"] = [t for t in schema.get("tables", []) if t["name"] in tables]
        sub["columns"] = [c for c in schema.get("columns", []) if c["table"] in tables]
        sub["foreign_keys"] = [
            f for f in schema.get("foreign_keys", [])
            if f["table"] in tables or f["ref_table"] in tables
        ]
        return sub

    async def sync(
        self, facade, conn_id: str, schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        """增量同步入口：指纹对比 → 无变化零副作用；有变化走 incremental_build。

        on_progress：job 层进度/取消回调（P1-15）——透传进增量构建的阶段边界。
        """
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
            result = await facade.build(conn_id, schema, samples, include_samples=include_samples)
            # P1-12：补 changed 键——apply_sync_result_status 首行判 changed，缺键则后台
            # 全量重建完成后 pending_review 永不出现（审核入口隐身、草案长期滞留）
            result.setdefault("changed", True)
            return result
        result = await facade.incremental_build(conn_id, schema, samples, include_samples=include_samples,
                                                on_progress=on_progress)
        result["fingerprint"] = new_fp
        logger.info(
            "[kb.sync] conn=%s 增量同步：+表%s -表%s 变更表%s",
            conn_id, result.get("tables_added", 0),
            result.get("tables_removed", 0), result.get("tables_changed", 0),
        )
        return result


    async def reembed_if_needed(self, facade, conn_id: str) -> bool:
        facade.ensure_loaded(conn_id)
        """嵌入配置变化（用户新配/更换嵌入模型）→ 重嵌表级向量（一表一 chunk），返回是否重嵌。

        嵌入配置被移除（base_url 清空）时构造 embedder 会抛错——降级跳过重嵌，
        保留旧向量继续可用，不让 overview/retrieve 因配置缺失而 500。
        """
        # 运行时引用同步：门面的 _runtime 是权威（可被替换/测试打桩），
        # 子模块持有的是构造时引用，委托前必须对齐，否则配置变更不生效。
        facade.build_service._runtime = facade._runtime
        cur = facade.build_service._emb_fingerprint()
        stored = facade.retrieval_service._artifact_fingerprint.get(conn_id, "")
        try:
            facade._emb = facade.build_service._embedder()
        except ValueError as e:
            logger.warning(
                "[kb.embed] conn=%s 嵌入配置缺失，跳过向量重嵌（保留旧向量继续可用）：%s", conn_id, e)
            return False
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
        await facade._save_conn_async(conn_id)
        return True


    async def _embed_tables(self, facade, conn_id: str, tables: set[str] | None = None,
                            on_progress: Any | None = None, p0: int = 82, p1: int = 95) -> None:
        """统一表级嵌入（一表一 chunk）

        向量文本 = override > AI 画像 > 空（零代码拼接）；空文本表跳过嵌入、
        保留旧向量继续可用（画像未生成的旧库/失败表不丢检索通道）。
        """
        tabs = facade.semantic_store._tables.get(conn_id, {})
        targets = [t for t in tabs if tables is None or t in tabs]
        if not targets:
            return
        # P1-3：嵌入器按需创建——confirm_all 等路径可能未经 overview/reembed 直接调用
        # （此前 _emb=None → 每表 AttributeError → 全部 [0.0] 覆盖，检索静默清零）
        if facade._emb is None:
            facade._emb = facade.build_service._embedder()
        vecs: dict[str, list[float]] = {}
        skipped = 0
        n = len(targets)
        _t0 = time.monotonic()
        for i, name in enumerate(targets):
            text = facade.semantic_store.table_vector_text(conn_id, tabs[name])
            if not text:
                skipped += 1
                continue
            if on_progress:
                on_progress("向量化", p0 + (p1 - p0) * i // max(1, n),
                            f"嵌入 {i + 1}/{n}")
            try:
                vecs[name] = await facade._emb.embed(text)
            except Exception as e:
                # P1-10：失败保留旧向量（下方 merge 语义）——此前写 [0.0] 覆盖旧值，
                # 一次嵌入抖动即把该表语义检索通道永久清零（与 docstring 宣称相反）
                logger.warning("[kb.embed] conn=%s 表级向量失败 table=%s（保留旧向量）：%s",
                               conn_id, name, e)
        logger.info(
            "[kb.embed] conn=%s 表级嵌入完成 表数=%d 无文本跳过=%d 耗时 %.2fs（串行 for，云端RTT主导，CPU≈0）",
            conn_id, n, skipped, time.monotonic() - _t0,
        )
        # 合并而非替换：无文本表/嵌入失败表保留旧向量；全量档丢弃已从库移除的表的陈旧向量
        cur = facade.retrieval_service._table_vec.get(conn_id, {})
        if tables is None:
            cur = {k: v for k, v in cur.items() if k in tabs}
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
            await facade._save_conn_async(conn_id)
            logger.info("[kb.store] conn=%s 确认/撤下后重嵌完成：tables=%s", conn_id, ",".join(names))
        except Exception as e:
            logger.warning("[kb.store] conn=%s 确认/撤下后重嵌失败 tables=%s：%s", conn_id, names, e)


    async def discard_drafts(self, facade, conn_id: str) -> dict[str, int]:
        """「放弃本轮」（2026-09 修订：当前生效知识从不在位让路，放弃 = 清提案，无需回滚）。

        - 字段/表：清除全部 proposed_*（当前 comment/values/example 原样保留）；
        - 标签：draft 标签移除并解绑（confirmed 标签是当前生效，保留）；
        - 图边：draft 队列清空（正式图已确认边不受影响）。
        提案不入合成文本/向量 -> 无需重嵌。
        """
        tabs = facade.semantic_store._tables.get(conn_id, {})
        n_cols = 0
        n_tables = 0
        for tk in tabs.values():
            if tk.clear_proposal():
                n_tables += 1
            for ci in tk.columns.values():
                if ci.clear_proposal():
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
        # draft 边清空（无墓碑，重建时各来源重新提案）
        pending = facade.graph_store._llm_graph_edges.get(conn_id, [])
        n_edges = len(pending)
        if pending:
            facade.graph_store._llm_graph_edges[conn_id] = []
        facade.graph_store.clear_diff_base(conn_id)
        if n_cols or n_tables or draft_names or n_edges:
            await facade._save_conn_async(conn_id)
        return {"columns": n_cols, "tables": n_tables, "tags": len(draft_names), "edges": n_edges}

    async def build(
        self, facade,
        conn_id: str,
        schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        on_progress: Any | None = None,
        include_samples: bool = False,
        enable_ai_annotation: bool = True,
        annotate_mode: str = "diff",
        tag_mode: str = "keep",
    ) -> dict[str, Any]:
        """构建知识库。

        annotate_mode：
        - "diff"：只对变化表重新提案（touched = 结构 diff 出的变化表；rename 表继承知识不重注释）。
          未变表已确认知识原样保留、不产新提案。首建（无旧 schema）天然等价 full。
        - "full"：清空全部提案后全表重新注释（走注释缓存，未变输入零 LLM 调用）。
        tag_mode：
        - "keep"/"anchor"：全量划分注入既有 confirmed 域锚点（anchor 额外允许 AI 提议改名/合并）；
        - "fresh"：先清空标签库与绑定，从零划分。
        """
        # P0-A：构建前必须恢复磁盘态——重启后内存为空时直接构建会拿空 _tables/_auto
        # 当"旧库"用（diff 退化 full、壳重建为空、_save_conn 落空快照覆盖全部已确认资产）。
        facade.ensure_loaded(conn_id)
        # P0-C：空 schema 一律拒绝（fail-closed）——权限/方言问题导致结构发现返回零表时，
        # 若照常构建会把每条已确认边判失效、清光表知识后正常落盘（整库静默清空）。
        self._assert_schema_usable(schema)
        facade.build_service._runtime = facade._runtime
        _t0 = time.monotonic()
        _t_prev = _t0
        _segs: list[tuple[str, float]] = []
        # 审核重构：构建发起即快照旧版知识（对比层"旧"侧）+ 旧结构集合（算 round_diff）。
        # 必须在 _sync_table_shells/提案覆盖之前——否则旧值被刷新丢失。
        facade.semantic_store.snapshot_round_baseline(conn_id)
        _round_prev_tables = set(facade.semantic_store._tables.get(conn_id, {}).keys())
        _round_prev_cols = {(t.name, c.name)
                            for t in facade.semantic_store._tables.get(conn_id, {}).values()
                            for c in t.columns.values()}
        # AI 阶段降级汇总（D3）：异常阶段记入 stats["degraded_phases"]，
        # jobs 层据此判定 tags+graph 双失败 → 阻断自动进待审
        degraded_phases: list[str] = []
        failed_tables: list[str] = []

        def _seg(label: str) -> None:
            nonlocal _t_prev
            now = time.monotonic()
            el = now - _t_prev
            _segs.append((label, el))
            _t_prev = now
            logger.info("[kb.build] conn=%s 耗时[%s] %.1fs（累计 t+%.1fs）",
                        conn_id, label, el, now - _t0)

        # ---- 模式解析：diff 模式算 touched + rename 继承；full/init 全表 ----
        old_schema = facade.semantic_store._schema.get(conn_id) or {}
        old_tables = {t["name"] for t in old_schema.get("tables", [])}
        new_tables = {t["name"] for t in schema.get("tables", [])}
        removed_tables = old_tables - new_tables
        touched: set[str] | None = None
        renames: list[dict[str, str]] = []
        diff0: dict[str, Any] | None = None
        if annotate_mode == "diff" and old_tables:
            touched, diff0 = self.compute_touched(old_schema, schema)
            renames = self.match_renames(
                old_schema, schema, set(diff0["removed_tables"]), set(diff0["added_tables"]))
            if renames:
                self.apply_renames(facade, conn_id, renames)
                diff0 = self.adjust_diff_for_renames(diff0, renames)
                touched = {t for t in touched if t not in {r["to"] for r in renames}}
                logger.info("[kb.build] conn=%s diff 模式检测到表重命名：%s（知识继承，不重新注释）",
                            conn_id, "；".join(f"{r['from']}→{r['to']}" for r in renames))
        tv = facade.retrieval_service._table_vec.get(conn_id, {})
        for t in removed_tables:
            tv.pop(t, None)
        _ann_map = self._annotation_cache.get(conn_id, {})
        for t in removed_tables:
            _ann_map.pop(t, None)
        # 已删表的标签绑定清理（孤儿标签后续由增量清理逻辑/标签管理处理）
        tt = facade.semantic_store._table_tags.get(conn_id, {})
        for t in removed_tables:
            tt.pop(t, None)
        if on_progress:
            on_progress("发现结构", 5, None)
        schema = dict(schema)
        schema["_conn_id"] = conn_id
        # auto 结构文档：full 全量重建；diff 只重建 touched（删表归档）
        if touched is None:
            facade._auto[conn_id] = facade.semantic_store._from_schema(schema)
        else:
            stale = touched | removed_tables
            auto = [d for d in facade._auto.get(conn_id, []) if d.table not in stale or d.archived]
            new_docs = self._from_schema_subset(schema, touched, conn_id, facade.semantic_store._from_schema)
            _now_docs = utcnow_iso()
            for d in new_docs:
                d.updated_at = _now_docs
            facade._auto[conn_id] = auto + new_docs
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
        facade.semantic_store._sync_table_shells(conn_id, schema, drop=removed_tables)
        # 清空上轮提案：full 清全部（本轮 AI 从零提案）；diff 只清 touched
        # （不清会导致上轮残留提案混入新一轮审查被误确认）
        facade.semantic_store.clear_round_proposals(conn_id, touched)
        if samples is not None:
            if touched is None:
                facade.semantic_store._samples[conn_id] = samples
            else:
                merged = {**facade.semantic_store._samples.get(conn_id, {}), **samples}
                for t in removed_tables:
                    merged.pop(t, None)
                facade.semantic_store._samples[conn_id] = merged
        if on_progress:
            on_progress("发现结构", 10, None)
        _seg("发现结构")

        ai_docs_added = 0
        ai_tags_added = 0
        _llm_edges: list[dict] = []
        if enable_ai_annotation:
            from app.knowledge.annotator import (
                _AdaptiveLimiter,
                annotate_domain,
                annotate_graph,
                annotate_tables,
            )
            from app.knowledge.ddl_context import (
                ddls_from_schema,
                generate_ddls_all,
                truncate_samples,
            )
            # 共享全局限流（D4）：阶段一~四共用一个自适应实例（429 减半/连成恢复），
            # 阶段一整任务占坑，阶段二~四在 _chat_with_beat 内按次占坑
            limiter = _AdaptiveLimiter(max(1, int(os.environ.get("TABLETALK_KB_ANNOTATION_CONCURRENCY", "10"))))

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
                if touched is not None:
                    # diff 模式：只注释变化表（未变表不产提案，缓存命中也省掉）
                    ddl_map = {t: ddl for t, ddl in ddl_map.items() if t in touched}
                if not ddl_map:
                    # 无变化表（diff 重构零 touched / 空库）：阶段一一步打满 + 明示跳过，
                    # 不再挂 0% 到全场结束（annotate_tables 零表时不产任何进度帧）
                    if on_progress:
                        on_progress("AI 正在处理", 100, "无表变更 · 跳过逐表注释",
                                    phase="annotate", step="per_table", step_index=1, step_total=1)
                    logger.info("[kb.build] conn=%s 无变化表，跳过逐表注释", conn_id)
                    _seg("阶段一·逐表注释（无变更，跳过）")
                else:
                    effective_samples = (
                        truncate_samples(facade.semantic_store._samples.get(conn_id, {})) if include_samples else None
                    )
                    ann = await annotate_tables(
                        _st, conn_id, ddl_map, facade.semantic_store._schema[conn_id],
                        samples=effective_samples, on_progress=on_progress, p0=0, p1=100,
                        limiter=limiter, cache=facade.annotation_cache(conn_id),
                    )
                    ai_docs_added = ann["added"]
                    failed_tables = ann["failed_tables"]
                    if failed_tables:
                        logger.warning("[kb.build] conn=%s 注释失败表：%s", conn_id, failed_tables)
                    logger.info("[kb.build] conn=%s 阶段=annotate 完成：items=%s cached=%s llm=%s",
                                conn_id, ai_docs_added, len(ann["cached_tables"]), len(ann["llm_tables"]))
                    _seg("阶段一·逐表注释")
            except Exception as e:
                degraded_phases.append("annotate")
                logger.warning("[kb.build] conn=%s 阶段=annotate AI注释异常：%s", conn_id, e)
                if on_progress:
                    on_progress("AI 正在处理", 100, None, phase="annotate")

            tags_error: str | None = None
            graph_error: str | None = None
            filters_error: str | None = None
            ai_tags_added = 0
            if tag_mode == "fresh":
                # 标签从零划分（用户显式选择：对现状彻底不满）：清库+解绑后重新划分
                cleared = facade.clear_tags(conn_id)
                logger.info("[kb.build] conn=%s tag_mode=fresh：清空标签库 %s 项后从零划分", conn_id, cleared)

            def _existing_tags_text() -> str:
                """既有 confirmed 域锚点文本（keep/anchor 档注入划分 prompt）。"""
                lib = facade.semantic_store._tags.get(conn_id, {})
                binds = facade.semantic_store._table_tags.get(conn_id, {})
                lines = [
                    f"- {nm}（{v.get('description', '')}）成员表：{', '.join(sorted(t for t, names in binds.items() if nm in names)) or '（暂无）'}"
                    for nm, v in sorted(lib.items()) if v.get("status") == "confirmed"
                ]
                return "\n".join(lines)

            async def _run_tags() -> None:
                nonlocal ai_tags_added, tags_error
                try:
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=facade.semantic_store._schema[conn_id],
                        on_progress=on_progress,
                        limiter=limiter,
                        existing_tags=_existing_tags_text() or None,
                        allow_rename=(tag_mode == "anchor"),
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
                    # 标签全集（版本制对比）：仅全量划分路径产出 → 挂本轮 round
                    if domain_result.get("domain_list"):
                        facade.semantic_store.set_round(conn_id, tags_new=domain_result["domain_list"])
                    logger.info("[kb.build] conn=%s 阶段=tags 完成：new_tags=%s", conn_id, ai_tags_added)
                except Exception as e:
                    tags_error = str(e) or type(e).__name__
                    degraded_phases.append("tags")
                    logger.warning("[kb.build] conn=%s 阶段=tags 标签提取异常：%s（%s）",
                                   conn_id, tags_error, type(e).__name__)

            async def _run_graph() -> None:
                nonlocal graph_error, _llm_edges
                try:
                    _llm_edges = await annotate_graph(
                        _st, conn_id, facade.semantic_store._schema[conn_id],
                        on_progress=on_progress,
                        limiter=limiter,
                    )
                    logger.info("[kb.build] conn=%s 阶段=graph 完成：llm_edges=%s", conn_id, len(_llm_edges))
                except Exception as e:
                    graph_error = str(e) or type(e).__name__
                    degraded_phases.append("graph")
                    logger.warning("[kb.build] conn=%s 阶段=graph 关系识别异常：%s（%s）",
                                   conn_id, graph_error, type(e).__name__)

            async def _run_filters() -> None:
                nonlocal filters_error
                try:
                    from app.knowledge.annotator import annotate_filters
                    await annotate_filters(
                        _st, conn_id, facade.semantic_store._schema[conn_id],
                        on_progress=on_progress,
                        limiter=limiter,
                    )
                    logger.info("[kb.build] conn=%s 阶段=filters AI 裁决完成", conn_id)
                except Exception as e:
                    filters_error = str(e) or type(e).__name__
                    degraded_phases.append("filters")
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
                        limiter=limiter,
                    )
                    logger.info("[kb.build] conn=%s 阶段=constants 识别完成", conn_id)
                except Exception as e:
                    degraded_phases.append("constants")
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
        # P0-B：队列重置推迟到新草案就绪——构建中途失败/取消时上一轮待审草案不丢失
        # （此前在构建一开始就 pop 且阶段一落盘，失败后待审 FK 边永久消失，只能整库重建找回）
        # P2-7：query_log 草案是 L3 生命周期资产（设计 §6 非构建来源），跨重建保留——
        # 它们既不由确定性建边重产、也不由 LLM 重提案，重建即清等于静默丢弃挖掘成果
        _qlog = [e for e in facade.graph_store._llm_graph_edges.get(conn_id, [])
                 if (e.get("source") or "") == "query_log"]
        facade.graph_store._llm_graph_edges[conn_id] = _qlog
        n_det = facade.graph_store.merge_draft_edges(conn_id, drafts)
        n_llm = facade.graph_store.merge_draft_edges(
            conn_id,
            [{**e, "source": "llm"} for e in _llm_edges],
        )
        # 图 diff 基线：本轮"重新主张过"的边 = 去重前 raw 提案（确定性 + LLM）∪ 现存队列。
        # 修复（2026-09）：此前误传去重后队列——与已确认边完全一致而被去重丢弃的边既不在
        # 队列也不在基线，被 diff 误判为"本轮未重新提案"（全量重建后 FK 边全红的根因）。
        facade.graph_store.set_diff_base(
            conn_id, [*drafts, *_llm_edges, *facade.graph_store.llm_graph_edges(conn_id)])
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
        # 结构 diff（审核页 new/del 徽章数据源）——必须在落盘**前**写入 round，否则快照缺 diff：
        # - diff 模式：真实结构 diff（含 renamed），与增量同步同构（mode=incr，镜头出增量摘要）
        # - full 模式：本轮 vs 构建前快照对比
        def _flat(d: dict[str, list[str]] | None) -> list[str]:
            return sorted(f"{t}.{c}" for t, cs in (d or {}).items() for c in cs)[:300]

        if touched is not None and diff0 is not None:
            facade.semantic_store.set_round(conn_id, mode="incr", diff={
                "tables": {
                    "added": sorted(diff0["added_tables"]),
                    "removed": sorted(diff0["removed_tables"]),
                    "renamed": [[r["from"], r["to"]] for r in renames],
                },
                "columns": {
                    "added": _flat(diff0["added_columns"]),
                    "removed": _flat(diff0["removed_columns"]),
                    "changed": _flat(diff0["changed_columns"]),
                },
            })
        else:
            cur_tables = set(facade.semantic_store._tables.get(conn_id, {}).keys())
            cur_cols = {(t.name, c.name)
                        for t in facade.semantic_store._tables.get(conn_id, {}).values()
                        for c in t.columns.values()}
            facade.semantic_store.set_round(conn_id, mode="full", diff={
                "tables": {
                    "added": sorted(cur_tables - _round_prev_tables),
                    "removed": sorted(_round_prev_tables - cur_tables),
                    "renamed": [],
                },
                "columns": {
                    "added": sorted(f"{t}.{c}" for t, c in cur_cols - _round_prev_cols)[:300],
                    "removed": sorted(f"{t}.{c}" for t, c in _round_prev_cols - cur_cols)[:300],
                    "changed": [],
                },
            })
        # failed_tables 无条件写入（空列表 = 清掉上轮残留，防跨轮误显示）
        facade.semantic_store.set_round(conn_id, failed_tables=failed_tables)
        facade._rebuild_vstore(conn_id)
        await facade._save_conn_async(conn_id)
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
            "degraded_phases": degraded_phases,
            "annotate_mode": annotate_mode,
            "tag_mode": tag_mode,
            "failed_tables": failed_tables,
        }


    async def incremental_build(
        self, facade, conn_id: str, new_schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
        include_samples: bool | None = None,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        """增量构建：只处理变化表（新增/变更/删除），不重建全部向量。

        on_progress（P1-15）：阶段边界进度 + 协作取消检查点——此前 sync job 无检查点，
        被新 build 替换后仍跑到底、并发写共享内存。
        """
        def _tick(pct: int, detail: str | None = None) -> None:
            if on_progress:
                on_progress("增量构建", pct, detail)
        # P0-C：空 schema 拒绝（与全量构建同守卫——零表会被当"全部删除"清库）
        self._assert_schema_usable(new_schema)
        if include_samples is None:
            include_samples = bool(facade._runtime and facade._runtime.get().kb_ai_annotation_samples)
        # 审核重构：增量同样快照旧版知识（对比层"旧"侧）
        facade.semantic_store.snapshot_round_baseline(conn_id)
        old_schema = facade.semantic_store._schema.get(conn_id, {})
        diff = facade.build_service.diff_schema(old_schema, new_schema)
        if facade.build_service.diff_is_empty(diff):
            return {"changed": False, **diff}

        # rename 检测（启发式）：removed ↔ added 列指纹一致 → 继承旧表知识，不重注释
        # （继承与 diff 对消逻辑与全量 diff 模式共用：apply_renames / adjust_diff_for_renames）
        renames = facade.build_service.match_renames(
            old_schema, new_schema, set(diff["removed_tables"]), set(diff["added_tables"])
        )
        if renames:
            facade.build_service.apply_renames(facade, conn_id, renames)
            diff = facade.build_service.adjust_diff_for_renames(diff, renames)
            logger.info("[kb.incr] conn=%s 检测到表重命名：%s（知识继承，不重新注释）",
                        conn_id, "；".join(f"{r['from']}→{r['to']}" for r in renames))
        touched = facade.build_service._touched_from_diff(diff)
        _tick(40, f"变化表 {len(touched)} 张")

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
        # 已删表的采样清理（无新采样时同样要清旧采样）
        if samples:
            merged = {**facade.semantic_store._samples.get(conn_id, {}), **samples}
            for t in diff["removed_tables"]:
                merged.pop(t, None)
            facade.semantic_store._samples[conn_id] = merged
        else:
            existing = facade.semantic_store._samples.get(conn_id, {})
            for t in diff["removed_tables"]:
                existing.pop(t, None)

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
        for t in removed:
            facade.build_service._annotation_cache.get(conn_id, {}).pop(t, None)

        rebuild_tables = touched - removed
        # 增量只清变化表的提案（改名表已从 rebuild 剔除，继承的知识不误清）
        facade.semantic_store.clear_round_proposals(conn_id, rebuild_tables)
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
        incr_degraded: list[str] = []
        incr_failed: list[str] = []
        if rebuild_tables:
            try:
                from app.knowledge.annotator import _AdaptiveLimiter, annotate_domain, annotate_tables
                from app.knowledge.ddl_context import (
                    ddls_from_schema,
                    generate_ddls_all,
                    truncate_samples,
                )
                from app.state import get_state as _get_state
                _st = _get_state()
                # 增量与全量共用同一限流语义（表少通常一轮就完）
                limiter = _AdaptiveLimiter(max(1, int(os.environ.get("TABLETALK_KB_ANNOTATION_CONCURRENCY", "10"))))
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
                    ann = await annotate_tables(
                        _st, conn_id, changed_ddl_map, facade.semantic_store._schema[conn_id],
                        samples=effective_samples, limiter=limiter,
                        cache=facade.annotation_cache(conn_id),
                    )
                    ai_docs_added = ann["added"]
                    incr_failed = ann["failed_tables"]
                    if incr_failed:
                        logger.warning("[kb.incr] conn=%s 注释失败表：%s", conn_id, incr_failed)
                if diff["added_tables"]:
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=facade.semantic_store._schema[conn_id],
                        mode="incremental", target_tables=diff["added_tables"],
                        limiter=limiter,
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
                # 增量补齐（2026-09 优化）：filters/constants/概念候选随增量刷新——
                # 此前三者仅全量构建运行，靠增量长大的库知识维度天然缺块。
                try:
                    from app.knowledge.annotator import annotate_constants, annotate_filters
                    sub_schema = self._schema_subset(facade.semantic_store._schema[conn_id], rebuild_tables)
                    if sub_schema["tables"]:
                        await annotate_filters(_st, conn_id, sub_schema, limiter=limiter)
                        await annotate_constants(_st, conn_id, sub_schema, limiter=limiter)
                except Exception as e:
                    incr_degraded.append("filters_constants")
                    logger.warning("[kb.incr] conn=%s 增量 filters/constants 补齐异常：%s", conn_id, e)
                self.ingest_values_candidates(facade, conn_id, new_schema)
                logger.info("[kb.incr] conn=%s AI注释完成：items=%s new_tags=%s", conn_id, ai_docs_added, ai_tags_added)
            except Exception as e:
                incr_degraded.append("annotate")
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
                    limiter=limiter,
                )
                facade.graph_store._upsert_llm_edges(conn_id, set(rebuild_tables), incr_edges)
                logger.info("[kb.incr] conn=%s 增量图谱补边：target=%s 新边=%s",
                            conn_id, len(rebuild_tables), len(incr_edges))
            except Exception as e:
                incr_degraded.append("graph")
                logger.warning("[kb.incr] conn=%s 增量图谱补边异常：%s", conn_id, e)

        _tick(80, "结构/注释完成，落盘")
        facade._schema_fingerprint_map[conn_id] = facade.build_service._schema_fingerprint(new_schema)
        facade._synced_at[conn_id] = now
        # T8：结构变化 → 表级过滤器 draft 候选随增量刷新（confirmed 保留）
        facade.build_service.ingest_filter_candidates(facade, conn_id, new_schema)
        # 结构 diff 挂本轮 round（审核页 new/del 徽章）；增量无标签全集 → tags_new 清空
        # 必须在落盘前写入（否则快照缺 diff）
        def _flat(d: dict[str, list[str]] | None) -> list[str]:
            return sorted(f"{t}.{c}" for t, cs in (d or {}).items() for c in cs)[:300]
        facade.semantic_store.set_round(conn_id, mode="incr", diff={
            "tables": {
                "added": sorted(diff.get("added_tables") or []),
                "removed": sorted(diff.get("removed_tables") or []),
                "renamed": [[r["from"], r["to"]] for r in (renames or [])],
            },
            "columns": {
                "added": _flat(diff.get("added_columns")),
                "removed": _flat(diff.get("removed_columns")),
                "changed": _flat(diff.get("changed_columns")),
            },
        }, tags_new=[], failed_tables=incr_failed)
        facade._rebuild_vstore(conn_id)
        await facade._save_conn_async(conn_id)
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
            "tables_renamed": renames,
            "cleared_tags": cleared_tags,
            "degraded_phases": incr_degraded,
            **diff,
        }

