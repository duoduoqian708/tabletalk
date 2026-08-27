"""知识库 v3 存储模型 v2：按表组织（TableKnowledge/ColumnInfo）。

数据源接入 → build()（结构抽取 + 采样 + 逐表 AI 注释[含取值对照/示例] + 图谱 +
向量索引 + 持久化）→ 审查工作台逐列 ✓/✕ 确认 → retrieve()/route_tables()
喂给 AI 上下文。

隐私：采样只存本地；发送给模型的注释 prompt 是否含样本值由 `kb_ai_annotation_samples`
门控（未授权 → 无样本 → values/example 为空）；嵌入默认离线哈希，真语义嵌入由设置选择。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.knowledge.docs import KnowledgeDoc
from app.knowledge.embedding import Embedder, HashingEmbedder, cosine, make_embedder
from app.knowledge.vectorstore import NumpyVectorStore, VectorChunk, VectorStore

if TYPE_CHECKING:
    from app.core.settings import SettingsStore

logger = logging.getLogger(__name__)


@dataclass
class ColumnInfo:
    """列级知识（v2）：结构壳（type/pk/fk/db_comment）+ AI/人工知识字段。"""
    name: str
    type: str = ""
    pk: bool = False
    fk: bool = False
    db_comment: str = ""
    comment: str = ""      # 业务含义（AI 生成 → 人工确认）
    values: str = ""       # 取值对照 "P=待付款；S=已发货；R=已退货"
    example: str = ""      # 示例值（首个非空样本，截断 60 字符）
    status: str = "none"   # none | draft | confirmed（comment+values 整体确认）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ColumnInfo":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TableKnowledge:
    """表级知识（快照 v2 的核心存储单元，一表一块）。"""
    name: str
    db_comment: str = ""
    column_count: int = 0
    comment: str = ""
    status: str = "none"   # none | draft | confirmed（表级注释状态）
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    ddl: str = ""
    excluded: bool = False
    layout: dict[str, Any] = field(default_factory=dict)  # 2D 图布局坐标（透传存储，渲染在 T4）
    vector_override: str = ""  # 人工覆盖的向量化片段文本；空 = 用构建合成文本

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "db_comment": self.db_comment,
            "column_count": self.column_count,
            "comment": self.comment,
            "status": self.status,
            "columns": {k: v.to_dict() for k, v in self.columns.items()},
            "ddl": self.ddl,
            "excluded": self.excluded,
            "layout": self.layout,
            "vector_override": self.vector_override,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TableKnowledge":
        cols = {k: ColumnInfo.from_dict(v) for k, v in (d.get("columns") or {}).items()}
        return cls(
            name=d.get("name", ""),
            db_comment=d.get("db_comment", ""),
            column_count=int(d.get("column_count", 0) or 0),
            comment=d.get("comment", ""),
            status=d.get("status", "none"),
            columns=cols,
            ddl=d.get("ddl", ""),
            excluded=bool(d.get("excluded")),
            layout=dict(d.get("layout") or {}),
            vector_override=d.get("vector_override", ""),
        )


@dataclass
class TableCard:
    """检索命中的表知识卡（一表一卡，spec §4）：text=可读表描述，payload=结构化信息。"""
    table: str
    text: str
    payload: dict[str, Any]
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "text": self.text,
            "payload": self.payload,
            "score": self.score,
        }


class KnowledgeBase:
    def __init__(self, data_dir: Path, runtime: "SettingsStore | None" = None) -> None:
        self._data_dir = data_dir
        self._runtime = runtime
        self._storage_backend = os.environ.get("TABLETALK_KB_STORAGE", "")  # sqlite(默认) | json
        self._storages: dict[str, Any] = {}
        self._emb: Embedder = HashingEmbedder()
        self._auto: dict[str, list[KnowledgeDoc]] = {}
        self._user: dict[str, list[KnowledgeDoc]] = {}
        self._samples: dict[str, dict[str, dict[str, list[Any]]]] = {}  # conn -> table -> column -> [values]
        self._graph: dict[str, dict[str, Any]] = {}                       # conn -> {edges}
        self._tags: dict[str, dict[str, dict[str, Any]]] = {}               # conn -> tag名 -> {description,status}
        self._table_tags: dict[str, dict[str, list[str]]] = {}            # conn -> table -> [tag名]
        self._tables: dict[str, dict[str, TableKnowledge]] = {}           # conn -> 表名 -> TableKnowledge（v2 核心存储）
        self._schema: dict[str, dict[str, Any]] = {}                      # conn -> 表/列/外键快照（增量 diff/图谱校验用）
        self._table_vec: dict[str, dict[str, list[float]]] = {}           # conn -> 表名 -> 表级向量（唯一向量体系，一表一 chunk）
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
        """从存储后端恢复一个连接的知识库（v2 快照；旧版本工件已在存储层作废为空）。"""
        if conn_id in self._auto:
            return
        snap = self._storage(conn_id).load()
        if not snap.tables and not snap.auto and not snap.user and not snap.edges:
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
        self._user[conn_id] = _docs_(snap.user)
        self._samples[conn_id] = snap.samples
        self._graph[conn_id] = {"edges": snap.edges}
        self._table_vec[conn_id] = snap.table_vec  # 唯一向量体系（doc_vec 碎片已作废不加载）
        self._tags[conn_id] = snap.tags
        self._table_tags[conn_id] = snap.table_tags
        self._tables[conn_id] = {
            name: TableKnowledge.from_dict(d) for name, d in snap.tables.items()
        }
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
                tables={name: tk.to_dict() for name, tk in self._tables.get(conn_id, {}).items()},
                auto=[d.to_dict() for d in self._auto.get(conn_id, [])],
                user=[d.to_dict() for d in self._user.get(conn_id, [])],
                samples=self._samples.get(conn_id, {}),
                edges=self._graph.get(conn_id, {"edges": []}).get("edges", []),
                table_vec=self._table_vec.get(conn_id, {}),
                tags=self._tags.get(conn_id, {}),
                table_tags=self._table_tags.get(conn_id, {}),
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
        except Exception as e:
            logger.warning("[kb.store] conn=%s 快照落盘失败：%s", conn_id, e)

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
        """构图（边 v2）：只画 FK 边（真实外键关系）；overlap 值重叠不上图。

        方向不变式：FK 子表（多侧）= from → 被引用父表（一侧）= to；
        基数：FK 兼 PK → 1:1，否则 n:1；reason 记录推断依据（悬停展示）。
        墓碑按字段对（from_col/to_col）匹配，不复活。
        """
        edges: list[dict[str, Any]] = []
        col_pk = {(c["table"], c["name"]): bool(c.get("pk")) for c in schema.get("columns", [])}
        for fk in schema.get("foreign_keys", []):
            e = {
                "from": fk["table"], "from_col": fk["column"],
                "to": fk["ref_table"], "to_col": fk["ref_column"],
                "kind": "fk", "weight": 1.0,
                "cardinality": "1:1" if col_pk.get((fk["table"], fk["column"])) else "n:1",
                "reason": f"FK 约束：{fk['table']}.{fk['column']} → {fk['ref_table']}.{fk['ref_column']}",
            }
            if not self._is_tombstoned(conn_id, e):
                edges.append(e)
        return {"edges": edges}

    # ---------- 表壳同步（v2 模型：结构壳刷新，知识字段保留） ----------
    def _sync_table_shells(
        self, conn_id: str, schema: dict[str, Any],
        drop: set[str] | None = None,
    ) -> None:
        """从 schema 建/同步 TableKnowledge 壳。

        - 结构字段（type/pk/fk/db_comment/column_count）以 schema 为准刷新；
        - ddl 结构兜底：快照合成 CREATE TABLE（payload 用，实时 DDL 在 AI 阶段覆盖）；
        - 知识字段（comment/values/example/status）保留（重建/增量不清掉人工成果）；
        - drop: 增量删除的表落壳；schema 中已消失的列从壳中移除。
        """
        tabs = self._tables.setdefault(conn_id, {})
        for t in (drop or set()):
            tabs.pop(t, None)
        tables_idx = {t["name"]: t for t in schema.get("tables", [])}
        cols_by_table: dict[str, list[dict[str, Any]]] = {}
        for c in schema.get("columns", []):
            cols_by_table.setdefault(c["table"], []).append(c)
        from app.knowledge.ddl_context import ddls_from_schema  # noqa: PLC0415

        ddls = ddls_from_schema(schema)
        for name, tinfo in tables_idx.items():
            tk = tabs.get(name)
            if tk is None:
                tk = tabs[name] = TableKnowledge(name=name)
            tk.db_comment = tinfo.get("comment", "")
            tk.column_count = int(tinfo.get("column_count", 0) or 0)
            if not tk.ddl:
                tk.ddl = ddls.get(name, "")
            alive: set[str] = set()
            for c in cols_by_table.get(name, []):
                cname = c["name"]
                ci = tk.columns.get(cname)
                if ci is None:
                    ci = tk.columns[cname] = ColumnInfo(name=cname)
                ci.type = c.get("type", "")
                ci.pk = bool(c.get("pk"))
                ci.fk = bool(c.get("fk"))
                ci.db_comment = c.get("comment", "")
                alive.add(cname)
            for cname in list(tk.columns):
                if cname not in alive:
                    del tk.columns[cname]

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
        except Exception as e:
            logger.warning("[kb.store] conn=%s 记录嵌入用量失败：%s", conn_id, e)

    def _emb_fingerprint(self) -> str:
        """当前嵌入配置指纹：hash | api:model@base_url。用户更换嵌入模型后指纹变化 → 触发向量重嵌。"""
        if self._runtime is not None:
            s = self._runtime.get()
            if s.embedding_provider == "api" and s.embedding_base_url:
                return f"api:{s.embedding_model}@{s.embedding_base_url}"
        return "hash"

    async def reembed_if_needed(self, conn_id: str) -> bool:
        """嵌入配置变化（用户新配/更换嵌入模型）→ 重嵌表级向量（一表一 chunk），返回是否重嵌。

        模型必须用户配置：用户配置了真语义嵌入后，旧 artifact 里哈希时代的向量必须作废重建，
        否则"配了模型却不生效"。只重嵌向量，不重建结构/图谱。
        注意：必须先用当前配置重建嵌入器（self._emb 可能是旧模型实例）。
        """
        cur = self._emb_fingerprint()
        stored = self._artifact_fingerprint.get(conn_id, "")
        # 无条件同步嵌入器到当前配置：进程重启后 _emb 可能仍是默认哈希嵌入器，
        # 即使指纹一致不重嵌，查询向量维度也必须与库中一致（避免 matmul 不匹配）。
        self._emb = self._embedder()
        # 维度自愈：表级向量维度混杂（旧模型残留）也触发重嵌，避免检索 matmul 不匹配。
        active_vecs = {k: v for k, v in self._table_vec.get(conn_id, {}).items() if v}
        dims = {len(v) for v in active_vecs.values()}
        dim_mismatch = len(dims) > 1
        if cur == stored and not dim_mismatch:
            return False
        if not self._tables.get(conn_id):
            return False
        await self._embed_tables(conn_id)
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
        self_check: bool | None = None,
    ) -> dict[str, Any]:
        """构建知识库。on_progress(stage, percent) 可选进度回调（任务化构建用）。

        include_samples: 是否将采样值发给 AI 辅助注释（仅控制 AI 发送；采样本身由
            调用方门控——严格零采样：未授权时请求方不抽，传空/None 即可）。
        enable_ai_annotation: 是否执行 AI 注释 + 标签生成（mock provider 时自动降级为伪注释）。
        self_check: 阶段2/3 审校式自检覆盖（None=沿用运行时 kb_build_self_check，默认开）。
        """
        # 链路耗时审计：阶段段边界打点 → 构建结束一条汇总（每段也实时打印）
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

        # 全量重建也遵守历史墓碑（用户删过的 overlap 边不复活）
        if conn_id not in self._edge_tombstones:
            try:
                snap = self._storage(conn_id).load()
                self._edge_tombstones[conn_id] = snap.edge_tombstones or []
            except Exception as e:
                logger.warning("[kb.build] conn=%s 读取历史墓碑失败：%s", conn_id, e)
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
        # 表壳同步（v2）：结构字段刷新，已确认/草案知识跨重建保留
        self._sync_table_shells(conn_id, schema)
        if samples is not None:
            self._samples[conn_id] = samples
        if on_progress:
            on_progress("发现结构", 10, None)
        _seg("发现结构")

        # ---- AI 语义增强：逐表注释（含取值对照/示例）+ 全局标签 ----
        ai_docs_added = 0
        ai_tags_added = 0
        if enable_ai_annotation:
            from app.knowledge.annotator import annotate_domain, annotate_tables
            from app.knowledge.ddl_context import (
                ddls_from_schema,
                generate_ddls_all,
                truncate_samples,
            )

            # 阶段一：逐表 AI 处理（on_progress 逐表回调，phase="annotate"）
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
                # 回退补齐：实时拿不到的表用结构快照合成（注释管线不因 DDL 中断）
                snap_schema = self._schema[conn_id]
                for tname, ddl in ddls_from_schema(snap_schema).items():
                    ddl_map.setdefault(tname, ddl)
                # DDL 进 TableKnowledge（payload 用，spec §4）
                for tname, ddl in ddl_map.items():
                    tk = self._tables.get(conn_id, {}).get(tname)
                    if tk is not None:
                        tk.ddl = ddl
                # 授权才发送；出网唯一防护是值级截断（用户决策：无列级过滤）
                effective_samples = (
                    truncate_samples(self._samples.get(conn_id, {})) if include_samples else None
                )
                ai_docs_added = await annotate_tables(
                    _st, conn_id, ddl_map, self._schema[conn_id],
                    samples=effective_samples, on_progress=on_progress, p0=0, p1=100,
                )
                logger.info("[kb.build] conn=%s 阶段=annotate 完成：items=%s", conn_id, ai_docs_added)
                _seg("阶段一·逐表注释")
            except Exception as e:
                logger.warning("[kb.build] conn=%s 阶段=annotate AI注释异常：%s", conn_id, e)
                if on_progress:
                    on_progress("AI 正在处理", 100, None, phase="annotate")

            # 阶段二 + 阶段三 并行（无数据依赖：标签划分不依赖边，关系识别不依赖标签；
            # 阶段三 run 期间不写盘，最终 _save_conn 统一落库，避免并行快照互相覆盖）
            tags_error: str | None = None
            graph_error: str | None = None
            ai_tags_added = 0
            self.clear_tags(conn_id)   # 阶段二前置：全量重构先从空标签库划分

            from app.knowledge.annotator import annotate_domain, annotate_graph

            async def _run_tags() -> None:
                nonlocal ai_tags_added, tags_error
                try:
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=self._schema[conn_id],
                        self_check=self_check, on_progress=on_progress,
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
                    logger.info("[kb.build] conn=%s 阶段=tags 完成：new_tags=%s", conn_id, ai_tags_added)
                except Exception as e:
                    tags_error = str(e) or type(e).__name__
                    logger.warning("[kb.build] conn=%s 阶段=tags 标签提取异常：%s（%s）",
                                   conn_id, tags_error, type(e).__name__)

            async def _run_graph() -> None:
                nonlocal graph_error
                try:
                    edges = await annotate_graph(
                        _st, conn_id, self._schema[conn_id],
                        on_progress=on_progress, self_check=self_check,
                    )
                    # 对比墓碑：用户之前拒绝过的边标记 previously_rejected
                    tombstone_keys = {
                        self._llm_edge_key(t) for t in self._llm_edge_tombstones.get(conn_id, [])
                    }
                    for e in edges:
                        if self._llm_edge_key(e) in tombstone_keys:
                            e["status"] = "previously_rejected"
                    self._llm_graph_edges[conn_id] = edges
                    logger.info("[kb.build] conn=%s 阶段=graph 完成：llm_edges=%s", conn_id, len(edges))
                except Exception as e:
                    graph_error = str(e) or type(e).__name__
                    logger.warning("[kb.build] conn=%s 阶段=graph 关系识别异常：%s（%s）",
                                   conn_id, graph_error, type(e).__name__)

            if on_progress:
                on_progress("AI 标签提取", 0, None, phase="tags")
                on_progress("AI 关系识别", 0, None, phase="graph")
            await asyncio.gather(_run_tags(), _run_graph())
            if on_progress:
                # 阶段二完成：tags 条打满（已由 annotate_domain 尾部上报；此分支兜底失败态）
                on_progress("AI 标签提取", 100,
                            f"领域标签划分失败：{tags_error}（已跳过，图谱继续）" if tags_error else None,
                            phase="tags")
                # 注意：graph 条**不**在此强制打满——AI 段止步 74，其后 FK 构图与落盘
                # 瞬时完成，由 run_build_job 置 done 时统一全满。graph 条打满 = 构建完成。
            _seg("阶段二三·并行")

        # ---- FK 构图 + 落盘（瞬时、无独立进度段）：FK 正式边由程序照搬外键约束，随阶段三
        #      一并就位；draft 快照（表壳/图谱/标签/样本）写库。向量化**延迟到人工确认后**
        #      （confirm/reject/edit → _reembed_tables）：确认前 AI 草案不入向量文本，
        #      构建期嵌入纯属白做（必被确认重嵌覆盖），尤其 API 嵌入是真实 N 次 HTTP。----
        self._graph[conn_id] = self._build_graph(conn_id, schema, self._samples.get(conn_id, {}))
        _seg("构图")
        self._emb = self._embedder()
        self._artifact_fingerprint[conn_id] = self._emb_fingerprint()
        self._schema_fingerprint_map[conn_id] = self._schema_fingerprint(schema)
        self._synced_at[conn_id] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._rebuild_vstore(conn_id)
        self._save_conn(conn_id)
        _seg("落盘")
        # 记录嵌入模型用量（ApiEmbedder 累计 usage → llm_log）
        self._log_embedding_usage(conn_id, "build")
        # 链路耗时汇总（一键总览：整体 + 各阶段 + 阶段间间隙）
        logger.info(
            "[kb.build] conn=%s 链路耗时 %.1fs ｜ %s",
            conn_id, time.monotonic() - _t0,
            " ⇒ ".join(f"{k} {v:.1f}s" for k, v in _segs),
        )
        logger.info(
            "[kb.build] conn=%s build 完成：tables=%s docs=%s ai_items=%s tags=%s graph=%s",
            conn_id, len(self._tables.get(conn_id, {})), len(self._auto[conn_id]), ai_docs_added,
            ai_tags_added, len(self._graph.get(conn_id, {}).get("edges", [])),
        )
        return {
            "docs": len(self._auto[conn_id]),
            "ai_docs_added": ai_docs_added,
            "ai_tags_added": ai_tags_added,
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

        - 新增/变化表：重生成 auto 文档 + 表壳刷新 + 变化表重注释 + 重嵌入 + 表级向量
        - 删除表：auto 文档 archived（保留可回溯），移除表壳/表级向量与相关边
        - 图：基于新 schema + 合并样本全量重构图（快；墓碑自动遵守）
        - include_samples: 数据授权门控（None=沿用运行时设置 kb_ai_annotation_samples）；
          未授权时变化表不发样本注释（values/example 无从产生，仅凭结构注释）
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
        # 表壳同步（v2）：删表落壳、变化表结构刷新；已确认知识保留
        self._sync_table_shells(conn_id, new_schema, drop=set(diff["removed_tables"]))
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

        # 2. 删除的表：auto 文档归档 + 移除表级向量
        removed = set(diff["removed_tables"])
        for d in auto:
            if d.table in removed and not d.archived:
                d.archived = True
                d.updated_at = now
        tv = self._table_vec.get(conn_id, {})
        for t in removed:
            tv.pop(t, None)

        # 3. 新增/变化表：移除旧 auto 文档（该表）→ 重新生成 → 表级向量重算
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
            # 变化表向量延迟到人工确认后（confirm/reject/edit → _reembed_tables）：
            # 确认前 draft 不入向量文本，构建期嵌入纯属白做
        else:
            self._auto[conn_id] = auto

        # 4. 增量 AI 注释：只为新增/变化表生成 AI 注释（不重做全库）
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
                # DDL 为变化表生成注释；实时获取失败回退结构快照合成（与全量构建同语义）
                try:
                    ddl_map = await generate_ddls_all(_st, conn_id)
                except Exception as e:
                    logger.warning(
                        "[kb.incr] conn=%s 实时 DDL 获取失败，回退结构快照合成：%s", conn_id, e)
                    ddl_map = {}
                for tname, ddl in ddls_from_schema(self._schema[conn_id]).items():
                    ddl_map.setdefault(tname, ddl)
                # DDL 进 TableKnowledge（与全量构建同款）
                for tname, ddl in ddl_map.items():
                    if tname in rebuild_tables:
                        tk = self._tables.get(conn_id, {}).get(tname)
                        if tk is not None:
                            tk.ddl = ddl
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
                # 有新表时增量标签吸收（D1 收紧：新表归入既有 confirmed 域或提议新域，不改已绑定）
                if diff["added_tables"]:
                    domain_result = await annotate_domain(
                        _st, conn_id,
                        schema=self._schema[conn_id],
                        mode="incremental", target_tables=diff["added_tables"],
                    )
                    ai_tags_added = domain_result.get("new_tags", 0)
                logger.info("[kb.incr] conn=%s AI注释完成：items=%s new_tags=%s", conn_id, ai_docs_added, ai_tags_added)
            except Exception as e:
                logger.warning("[kb.incr] conn=%s 增量AI注释/标签异常：%s", conn_id, e)

        # 5. 删表清理（D2）：表→标签绑定 + LLM draft 边 + 0 表标签（返回被清名供审计）
        cleared_tags = self._sync_removed_tables(conn_id, set(diff["removed_tables"]))
        if cleared_tags:
            logger.info("[kb.incr] conn=%s 删除清理：清理 0 表标签=%s", conn_id, cleared_tags)

        # 6. 图谱：FK 正式边全量重构图（墓碑保护）+ 变化表 LLM 增量补边（D3）
        self._graph[conn_id] = self._build_graph(conn_id, new_schema, all_samples)
        if rebuild_tables:
            try:
                from app.knowledge.annotator import annotate_graph
                from app.state import get_state as _gstate  # noqa: PLC0415
                _st2 = _gstate()
                incr_edges = await annotate_graph(
                    _st2, conn_id, self._schema[conn_id],
                    mode="incremental", target_tables=list(rebuild_tables),
                )
                self._upsert_llm_edges(conn_id, set(rebuild_tables), incr_edges)
                logger.info("[kb.incr] conn=%s 增量图谱补边：target=%s 新边=%s",
                            conn_id, len(rebuild_tables), len(incr_edges))
            except Exception as e:
                logger.warning("[kb.incr] conn=%s 增量图谱补边异常：%s", conn_id, e)

        # 7. 指纹与同步时间
        self._schema_fingerprint_map[conn_id] = self._schema_fingerprint(new_schema)
        self._synced_at[conn_id] = now
        self._rebuild_vstore(conn_id)
        self._save_conn(conn_id)
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

    # ---------- 一表一 chunk（spec §4 向量合一） ----------
    def _synthesize_table_text(self, conn_id: str, tk: TableKnowledge) -> str:
        """合成可读表描述（embedding 文本，spec §4 格式）。

        - confirmed 列知识（comment/values/example）优先入文；
        - 未确认仅结构壳（类型 + 库注释兜底）；未授权构建天然无 values/example；
        - 表注释取 confirmed 的 AI 注释，否则回退 db_comment。
        例：orders，订单表。字段有：id：主键，订单ID，示例为123；status：订单状态，可选值：S=已发货、R=已退货
        """
        # 人工覆盖优先：编辑过向量化片段 → 直接用它（空串视为清空覆盖回落到合成）
        if tk.vector_override:
            return tk.vector_override
        header = tk.comment if tk.status == "confirmed" else tk.db_comment
        rows: list[str] = []
        for ci in tk.columns.values():
            parts = [ci.name]
            marks = []
            if ci.pk:
                marks.append("主键")
            if ci.fk:
                marks.append("外键")
            if marks:
                parts.append("、".join(marks))
            if ci.status == "confirmed":
                if ci.comment:
                    parts.append(ci.comment)
                if ci.values:
                    parts.append(f"可选值：{ci.values}")
                if ci.example:
                    parts.append(f"示例为{ci.example}")
            else:
                if ci.type:
                    parts.append(ci.type)
                if ci.db_comment:
                    parts.append(ci.db_comment)
            rows.append(f"{parts[0]}：" + "，".join(parts[1:]) if len(parts) > 1 else parts[0])
        head = f"{tk.name}，{header}" if header else tk.name
        return f"{head}。字段有：{'；'.join(rows) or '无'}"

    def _table_payload(self, conn_id: str, tk: TableKnowledge) -> dict[str, Any]:
        """表级 chunk payload（spec §4）：结构化信息（渲染在 T4，此处仅存储透传）。"""
        draft_count = (1 if tk.status == "draft" else 0) + sum(
            1 for ci in tk.columns.values() if ci.status == "draft"
        )
        return {
            "ddl": tk.ddl,
            "tags": list(self._table_tags.get(conn_id, {}).get(tk.name, [])),
            "layout": tk.layout,
            "draft_count": draft_count,
            "updated_at": self._synced_at.get(conn_id, ""),
        }

    async def _embed_tables(self, conn_id: str, tables: set[str] | None = None,
                            on_progress: Any | None = None, p0: int = 82, p1: int = 95) -> None:
        """统一表级嵌入（一表一 chunk）：文本 = _synthesize_table_text，key = 表名。

        tables=None → 全量表（重嵌）；否则只重算这些表（确认后/reembed，其余保留）。
        由确认/撤下/人工编辑路径（_reembed_tables）调用——构建期不嵌（确认前草案不入文）。
        """
        tabs = self._tables.get(conn_id, {})
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
                vecs[name] = await self._emb.embed(self._synthesize_table_text(conn_id, tabs[name]))
            except Exception as e:
                logger.warning("[kb.embed] conn=%s 表级向量失败 table=%s：%s", conn_id, name, e)
                vecs[name] = [0.0]
        if tables is None:
            self._table_vec[conn_id] = vecs
        else:
            cur = self._table_vec.get(conn_id, {})
            for t in targets:
                cur.pop(t, None)
            self._table_vec[conn_id] = {**cur, **vecs}

    async def _reembed_tables(self, conn_id: str, table_names: list[str]) -> None:
        """确认/撤下后受影响表即时重嵌：合成文本随确认状态变化，向量必须跟上。

        只重算指定表的向量 + 一次 vstore 重建 + 一次落盘（调用方先收集表集合，
        避免 N×全量重建）。失败 warning 不抛——状态变更本身不受影响，
        下次 retrieve 的 reembed_if_needed 仍可兜底。
        """
        tabs = self._tables.get(conn_id, {})
        names = [t for t in dict.fromkeys(table_names or []) if t in tabs]
        if not names:
            return
        try:
            self._emb = self._embedder()  # 与 reembed_if_needed 同款：先用当前配置重建嵌入器
            await self._embed_tables(conn_id, set(names))
            self._rebuild_vstore(conn_id)
            self._save_conn(conn_id)
            logger.info("[kb.store] conn=%s 确认/撤下后重嵌完成：tables=%s", conn_id, ",".join(names))
        except Exception as e:
            logger.warning("[kb.store] conn=%s 确认/撤下后重嵌失败 tables=%s：%s", conn_id, names, e)

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
            logger.debug("[kb.sync] conn=%s 结构无变化，跳过增量", conn_id)
            return {"changed": False, "fingerprint": new_fp, "tables_added": 0, "tables_removed": 0, "tables_changed": 0}
        if not self._auto.get(conn_id):
            # 未构建过的连接不应走增量（调用方应保证 ready）；防御性直接全量
            logger.warning("[kb.sync] conn=%s 无已构建工件，防御性回退全量构建", conn_id)
            return await self.build(conn_id, schema, samples, include_samples=include_samples)
        result = await self.incremental_build(conn_id, schema, samples, include_samples=include_samples)
        result["fingerprint"] = new_fp
        logger.info(
            "[kb.sync] conn=%s 增量同步：+表%s -表%s 变更表%s",
            conn_id, result.get("tables_added", 0),
            result.get("tables_removed", 0), result.get("tables_changed", 0),
        )
        return result

    # ---------- 统一向量索引（VectorStore，单一表级 chunk 体系） ----------
    def _vector_store(self, conn_id: str) -> VectorStore:
        vs = self._vstore.get(conn_id)
        if vs is None:
            vs = NumpyVectorStore()
            self._vstore[conn_id] = vs
            self._rebuild_vstore(conn_id)
        return vs

    def _rebuild_vstore(self, conn_id: str) -> None:
        """单体系重建：每表一条 VectorChunk（collection=table，一表一 chunk，spec §4）。

        id=tbl-{name}；text=可读表描述；metadata={table} 过滤区；
        payload=结构化信息（ddl/tags/layout/draft_count/updated_at）。
        向量维度统一对齐主维度（旧 artifact 可能有嵌入失败残留的 [0.0] 短向量）。
        """
        from collections import Counter

        chunks: list[VectorChunk] = []
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        fp = self._artifact_fingerprint.get(conn_id, "")
        tabs = self._tables.get(conn_id, {})
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
            chunks.append(VectorChunk(
                id=f"tbl-{name}", collection="table",
                text=self._synthesize_table_text(conn_id, tk),
                metadata={"table": name},
                payload=self._table_payload(conn_id, tk),
                vector=_aligned(vec), fingerprint=fp,
                updated_at=self._synced_at.get(conn_id, "") or now,
            ))
        self._vstore[conn_id] = NumpyVectorStore()
        self._vstore[conn_id].set_chunks(chunks)

    # ---------- 检索 ----------
    def _docs(self, conn_id: str) -> list[KnowledgeDoc]:
        """全部活跃文档（过滤 archived）——仅剩结构文档 + 用户手写笔记（列表/审计用，
        不再进向量检索；检索统一走表级知识卡）。"""
        return [
            d for d in (
                self._auto.get(conn_id, [])
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

    async def retrieve(self, conn_id: str, query: str = "", table: str | None = None, k: int = 10) -> list[TableCard]:
        """检索命中表知识卡（一表一卡，spec §4）：关键词 × 表级向量 × 图谱邻居扩散。

        - 关键词词面命中 / 向量语义召回（同一 chunk 集）；
        - 已确认（payload.draft_count=0）优先于 AI 草案；
        - 图谱扩展：相邻表高分 → 本表加分（FK 边连通子图）；
        - 零命中（空查询/无相关）回退全表排序，保证返回非空。
        """
        if conn_id not in self._auto:
            self._load_conn(conn_id)  # 重启后未重新构建也能用上次的知识
        await self.reembed_if_needed(conn_id)  # 嵌入模型变化 → 向量重嵌（否则检索维度不匹配）
        tabs = self._tables.get(conn_id, {})
        if not tabs:
            return []
        tokens = [t for t in re.split(r"[\s,，。；;：:、/\\|()（）]+", (query or "").lower()) if t]
        q = (query or "").lower()
        tgt = (table or "").lower()
        qvec = await self._emb.embed(query) if query else None
        vec_scores = self._vector_store(conn_id).scores_all(qvec, collection="table") if qvec is not None else {}

        texts: dict[str, str] = {}
        payloads: dict[str, dict[str, Any]] = {}
        for name, tk in tabs.items():
            texts[name] = self._synthesize_table_text(conn_id, tk)
            payloads[name] = self._table_payload(conn_id, tk)

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
        neighbors = self._neighbors(conn_id)
        final = dict(base)
        for name in tabs:
            best = max((base.get(nt, 0.0) for nt in neighbors.get(name.lower(), ())), default=0.0)
            if best > 0:
                final[name] += 0.35 * best

        scored = sorted(tabs.keys(), key=lambda n: final.get(n, 0.0), reverse=True)
        if not any(final.get(n, 0.0) > 0 for n in tabs):
            scored = list(tabs.keys())
        # 记录检索阶段的嵌入用量
        self._log_embedding_usage(conn_id, "retrieve")
        return [
            TableCard(table=n, text=texts[n], payload=payloads[n], score=round(final.get(n, 0.0), 4))
            for n in scored[:max(1, k)]
        ]

    async def to_context(self, conn_id: str, query: str = "", table: str | None = None, k: int = 8) -> str:
        """转成给 AI 的上下文文本（spec §4）：【知识库】段为命中表的完整知识卡列表。"""
        cards = await self.retrieve(conn_id, query, table, k)
        if not cards:
            return ""
        lines = ["【知识库】"]
        for c in cards:
            mark = "" if c.payload.get("draft_count", 0) == 0 else "（AI 草案，待确认）"
            lines.append(f"- [表]{mark} {c.text}")
        return "\n".join(lines)

    # ---------- AI 草案落库 + 人工确认（v2：写 TableKnowledge/ColumnInfo） ----------
    def annotate_drafts(self, conn_id: str, items: list[dict[str, Any]]) -> int:
        """AI 注释草案入库（status=draft），待人工确认。

        items: [{table, column|None, comment, values?, example?}]。
        - 列项 → ColumnInfo(comment/values/example)；表级 → TableKnowledge.comment；
        - 已确认（confirmed）内容不被草案覆盖；
        - 表/列不在库中（敏感过滤/已删除）忽略。
        """
        tabs = self._tables.get(conn_id, {})
        applied = 0
        for it in items:
            table = it.get("table")
            comment = str(it.get("comment") or "").strip()
            if not table or not comment:
                continue
            tk = tabs.get(table)
            if tk is None:
                continue
            column = it.get("column")
            if column:
                ci = tk.columns.get(column)
                if ci is None:
                    continue
                if ci.status == "confirmed":
                    continue  # 已确认内容不被草案覆盖
                ci.comment = comment
                ci.values = str(it.get("values") or "").strip()
                ci.example = str(it.get("example") or "").strip()[:60]
                ci.status = "draft"
            else:
                if tk.status == "confirmed":
                    continue  # 已确认内容不被草案覆盖
                tk.comment = comment
                tk.status = "draft"
            applied += 1
        if applied:
            self._save_conn(conn_id)
        return applied

    async def confirm(self, conn_id: str, table: str | None = None, column: str | None = None) -> int:
        """人工确认草案 → 权威（v2：状态机 none/draft → confirmed）。

        column 指定 → 单列（仅 draft 计数）；只给 table → 该表注释及其全部列；
        都不给 → 全库。表级 none（无 AI 注释的空内容表）同样定稿但不计数——
        确认闸后全库无残留草案。
        确认改变合成文本（注释/取值/示例入文）→ 收集受影响表一次性重嵌，
        避免向量停留纯结构文本。
        """
        tabs = self._tables.get(conn_id, {})
        targets = [tabs[table]] if table and table in tabs else (
            [] if table else list(tabs.values())
        )
        n = 0
        affected: list[str] = []
        for tk in targets:
            changed = False
            if column:
                ci = tk.columns.get(column)
                if ci and ci.status == "draft":
                    ci.status = "confirmed"
                    n += 1
                    changed = True
                if changed:
                    affected.append(tk.name)
                continue
            if tk.status == "draft":
                n += 1
                changed = True
            tk.status = "confirmed"  # none=空内容直接定稿；draft=草案确认
            for ci in tk.columns.values():
                if ci.status == "draft":
                    ci.status = "confirmed"
                    n += 1
                    changed = True
            if changed:
                affected.append(tk.name)
        if n:
            self._save_conn(conn_id)
        if affected:
            await self._reembed_tables(conn_id, affected)
        return n

    async def confirm_all(self, conn_id: str) -> dict[str, int]:
        """确认闸（构建后一键启用）：批量确认全部草案注释（表+列）+ 全部 draft 标签 + 全部 LLM draft 图边。

        标签确认后才参与"问题→选表"路由；注释确认后进入权威知识卡。
        注释确认会改变合成文本 → confirm 内部收集受影响表集合一次重嵌（不逐表重建）。
        kb_status → ready 由调用方（api 层）负责。
        """
        n_docs = await self.confirm(conn_id)
        n_tags = 0
        for name in list(self._tags.get(conn_id, {}).keys()):
            if self.confirm_tag(conn_id, name):
                n_tags += 1
        n_edges = self.confirm_graph_edges(conn_id)
        return {"docs": n_docs, "tags": n_tags, "edges": n_edges}

    def clear(self, conn_id: str) -> None:
        """取消构建/失败后清理半成品内存（不落盘）。"""
        for d in (self._auto, self._user, self._samples, self._graph,
                  self._tags, self._table_tags, self._tables, self._schema,
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
        """LLM 边的去重/墓碑键（边 v2：字段对，方向无关，与 FK 墓碑同语义）。"""
        a = (e.get("from_table", ""), e.get("from_col") or "")
        b = (e.get("to_table", ""), e.get("to_col") or "")
        x, y = sorted([a, b])
        return (x[0], x[1], y[0], y[1])

    def _sync_removed_tables(self, conn_id: str, removed: set[str]) -> list[str]:
        """删表清理（增量）：表→标签绑定移除 + 含删表的 LLM draft 边移除 + 0 表标签清理。

        返回被清理的标签名（供审计留痕 detail）。标签绑表数为 0 即孤儿（D2 收紧：清理）。
        """
        cleared: list[str] = []
        if not removed:
            return cleared
        # 1. 表→标签绑定
        tt = self._table_tags.get(conn_id)
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
        lib = self._tags.get(conn_id, {})
        if lib:
            usage: dict[str, int] = {}
            for _t, names in (self._table_tags.get(conn_id) or {}).items():
                for n in names:
                    usage[n] = usage.get(n, 0) + 1
            dead = [n for n in list(lib) if usage.get(n, 0) == 0]
            for n in dead:
                del lib[n]
            if dead:
                self._save_conn(conn_id)
            cleared = dead
        return cleared

    def _upsert_llm_edges(self, conn_id: str, targets: set[str], new_edges: list[dict]) -> None:
        """增量局部补边落库：替换涉及 targets 的旧 LLM draft 边为新边，其余保留；去重 + 墓碑。
        """
        pending = self._llm_graph_edges.get(conn_id, [])
        # 墓碑：用户拒绝过的边不复活
        tomb_keys = {self._llm_edge_key(t) for t in self._llm_edge_tombstones.get(conn_id, [])}
        # 保留不涉及 targets 的旧边
        keep = [
            e for e in pending
            if e.get("from_table") not in targets and e.get("to_table") not in targets
        ]
        seen = {self._llm_edge_key(e) for e in keep}
        out = list(keep)
        for e in new_edges:
            if self._llm_edge_key(e) in seen or self._llm_edge_key(e) in tomb_keys:
                continue
            seen.add(self._llm_edge_key(e))
            out.append(e)
        self._llm_graph_edges[conn_id] = out

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
        # 写入正式图谱（边 v2：kind=llm + cardinality + reason）
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
                "kind": "llm", "weight": 1.0,
                "cardinality": e.get("cardinality") or "n:1",
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
        """待确认数（确认闸 UI 用）：draft 注释（表+列）+ draft 标签 + LLM draft 边。"""
        n_comments = 0
        for tk in self._tables.get(conn_id, {}).values():
            if tk.status == "draft":
                n_comments += 1
            n_comments += sum(1 for ci in tk.columns.values() if ci.status == "draft")
        return {
            "draft_docs": n_comments,
            "draft_tags": sum(1 for v in self._tags.get(conn_id, {}).values() if v.get("status") == "draft"),
            "llm_graph_draft": len(self._llm_graph_edges.get(conn_id, [])),
        }

    async def reject_comment(self, conn_id: str, table: str, column: str | None = None) -> int:
        """拒绝草案注释（审查页逐列 ✕）：AI 内容整条撤下回 none。

        撤下同样改变合成文本（confirmed 注释/取值出文，draft_count 变化）→ 该表即时重嵌。
        """
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            return 0
        n = 0
        if column:
            ci = tk.columns.get(column)
            if ci and ci.status != "none":
                ci.comment = ""
                ci.values = ""
                ci.example = ""
                ci.status = "none"
                n += 1
        elif tk.status != "none" or tk.comment:
            tk.comment = ""
            tk.status = "none"
            n += 1
        if n:
            self._save_conn(conn_id)
            await self._reembed_tables(conn_id, [table])
        return n

    async def edit_table_knowledge(
        self, conn_id: str, table: str,
        table_comment: str | None = None,
        column_comments: list[dict[str, Any]] | None = None,
        vector_text: str | None = None,
    ) -> dict[str, Any]:
        """人工按表编辑知识（详情面板两块，只写知识字段）。

        - table_comment：表级注释（人工写入 → 权威 confirmed）；
        - column_comments：[{name, comment?, values?, example?}] 每列仅改给定字段，
          列不存在即报错（避免静默丢字段）；多字段任一改动即列 confirmed；
        - vector_text：向量化片段覆盖（'' 清空覆盖回落合成文本）。
        schema 镜像字段（列类型/PK/FK/DDL/表名）不可改写，type 恒 table_schema。
        编辑改变合成文本/向量 → 该表即时重嵌。
        """
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            raise KeyError(table)
        changed = False

        if table_comment is not None:
            if tk.comment != table_comment:
                changed = True
            tk.comment = table_comment
            # 人工写入 = 权威，覆盖 AI draft / none 状态
            tk.status = "confirmed" if table_comment else "none"

        if column_comments:
            for edit in column_comments:
                name = edit.get("name", "")
                ci = tk.columns.get(name)
                if ci is None:
                    raise KeyError(f"{table}.{name}")
                for field in ("comment", "values", "example"):
                    if edit.get(field) is not None and getattr(ci, field) != edit[field]:
                        setattr(ci, field, edit[field])
                        changed = True
                if ci.comment or ci.values or ci.example:
                    ci.status = "confirmed"

        if vector_text is not None:
            if tk.vector_override != vector_text:
                tk.vector_override = vector_text
                changed = True

        if changed:
            self._save_conn(conn_id)
            await self._reembed_tables(conn_id, [table])
        return {
            "changed": changed,
            "table": table,
            "vector_text": self._synthesize_table_text(conn_id, tk),
            "vector_override": tk.vector_override or None,
        }

    async def reject(self, conn_id: str, table: str, column: str | None = None) -> int:
        """拒绝草案注释（v2：按表/列撤下；旧 doc_id 版本随草稿文档退役）。"""
        return await self.reject_comment(conn_id, table, column)

    async def discard_drafts(self, conn_id: str) -> dict[str, int]:
        """放弃本轮全部草案（审阅弹窗「放弃」）：撤下 draft，保留历史已确认内容。

        - 列/表注释 status=="draft" → "none"（文本不清空，便于下次重建对照）；
          confirmed 一律不动；
        - draft 标签 → 移除并解绑（对齐 reject_tag 行为）；
        - LLM draft 边 → 删除并记墓碑（对齐 reject_graph_edges 语义，重建不复活）。
        返回 {columns, tables, tags, edges} 撤下计数。
        """
        tabs = self._tables.get(conn_id, {})
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
        # draft 标签移除并解绑（对齐 reject_tag）
        lib = self._tags.get(conn_id, {})
        draft_names = {n for n, v in lib.items() if v.get("status") == "draft"}
        for name in draft_names:
            del lib[name]
        if draft_names:
            for t, names in self._table_tags.get(conn_id, {}).items():
                if draft_names & set(names):
                    self._table_tags[conn_id][t] = [n for n in names if n not in draft_names]
        # LLM draft 边删除 + 记墓碑（去重，防重建复活）
        pending = self._llm_graph_edges.get(conn_id, [])
        n_edges = len(pending)
        if pending:
            tombstones = self._llm_edge_tombstones.setdefault(conn_id, [])
            existing_keys = {self._llm_edge_key(t) for t in tombstones}
            for e in pending:
                if self._llm_edge_key(e) not in existing_keys:
                    tombstones.append({
                        "from_table": e["from_table"], "from_col": e.get("from_col"),
                        "to_table": e["to_table"], "to_col": e.get("to_col"),
                    })
            self._llm_graph_edges[conn_id] = []
        if n_cols or n_tables or draft_names or n_edges:
            self._rebuild_vstore(conn_id)  # payload.draft_count 刷新（文本未入向量，无需重嵌）
            self._save_conn(conn_id)
        return {"columns": n_cols, "tables": n_tables, "tags": len(draft_names), "edges": n_edges}

    def has_confirmed_content(self, conn_id: str) -> bool:
        """库中是否存在任何 confirmed 内容（放弃后 kb_status 流转判定）：
        任一列/表注释 confirmed 或任一标签 confirmed 即视为有历史。"""
        if any(
            tk.status == "confirmed" or any(ci.status == "confirmed" for ci in tk.columns.values())
            for tk in self._tables.get(conn_id, {}).values()
        ):
            return True
        return any(v.get("status") == "confirmed" for v in self._tags.get(conn_id, {}).values())

    # ---------- 领域标签（每库一套，draft→人工确认） ----------
    def clear_tags(self, conn_id: str) -> int:
        """全量重构：清空该连接的标签库与表→标签绑定（残留 0 表标签不复活）。"""
        lib = self._tags.pop(conn_id, {})
        bound = self._table_tags.pop(conn_id, {})
        n = len(lib)
        if n or bound:
            self._save_conn(conn_id)
        return n

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
        """向量通道：问题向量 × 表级 chunk（与 retrieve 同一 chunk 集）→ top-K 表。

        语义召回不依赖标签覆盖率；离线 HashingEmbedder 只桥接表面重叠，
        因此叠加词面加权作为底线：表名/字段/注释与问题同词 → 加分
        （配 API 真语义 embedder 时语义分数自动更强）。
        """
        await self.reembed_if_needed(conn_id)  # 嵌入模型变化 → 向量重嵌（维度一致）
        tabs = self._tables.get(conn_id, {})
        if not tabs or not (question or "").strip():
            return []
        try:
            qvec = await self._emb.embed(question)
        except Exception:
            qvec = None
        ql = (question or "").lower()
        base_vec = self._vector_store(conn_id).scores_all(qvec, collection="table") if qvec is not None else {}
        scored: list[tuple[str, float]] = []
        for name, tk in tabs.items():
            score = base_vec.get(f"tbl-{name}", 0.0)
            # 词面加权：表名 / 中文词 / 字段名出现在问题或反之中
            blob = self._synthesize_table_text(conn_id, tk).lower()
            if name.lower() in ql:
                score += 0.6
            for tok in re.findall(r"[\u4e00-\u9fff]{2,}", ql):
                if tok in blob:
                    score += 0.35
            for tok in re.findall(r"[a-z_]{3,}", ql):
                if tok in blob:
                    score += 0.2
            if score > 0.1:
                scored.append((name, round(score, 4)))
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
        self.ensure_loaded(conn_id)
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
    def table_card(self, conn_id: str, table: str) -> dict[str, Any] | None:
        """单表知识卡（kb_read 单表查询用）：{table, text, payload}。"""
        self.ensure_loaded(conn_id)
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            return None
        return {
            "table": table,
            "text": self._synthesize_table_text(conn_id, tk),
            "payload": self._table_payload(conn_id, tk),
        }

    def table_cards(self, conn_id: str) -> list[dict[str, Any]]:
        """全部表知识卡（kb_read 全量查询用）。"""
        self.ensure_loaded(conn_id)
        out: list[dict[str, Any]] = []
        for name in self._tables.get(conn_id, {}):
            card = self.table_card(conn_id, name)
            if card:
                out.append(card)
        return out

    def overview(self, conn_id: str) -> dict[str, Any]:
        """人工审查页的数据（v2 按表组织）：表块含逐列注释/取值对照/示例与确认状态。"""
        snap = self._schema.get(conn_id, {})
        kind_by_table = {t["name"]: t.get("kind", "table") for t in snap.get("tables", [])}
        lib = self._tags.get(conn_id, {})

        def table_tags(table: str) -> list[dict[str, Any]]:
            return [
                {"name": n, "status": lib.get(n, {}).get("status", "draft")}
                for n in self._table_tags.get(conn_id, {}).get(table, []) if n in lib
            ]

        excluded = set(self.excluded_tables(conn_id))
        draft_count = 0
        tables_out: list[dict[str, Any]] = []
        for tk in self._tables.get(conn_id, {}).values():
            if tk.status == "draft":
                draft_count += 1
            cols = []
            for ci in tk.columns.values():
                if ci.status == "draft":
                    draft_count += 1
                cols.append({
                    "name": ci.name, "type": ci.type,
                    "pk": ci.pk, "fk": ci.fk,
                    "db_comment": ci.db_comment,
                    "comment": ci.comment,
                    "values": ci.values,
                    "example": ci.example,
                    "status": ci.status,
                })
            tables_out.append({
                "name": tk.name,
                "kind": kind_by_table.get(tk.name, "table"),
                "db_comment": tk.db_comment,
                "column_count": tk.column_count,
                "comment": tk.comment,
                "comment_status": tk.status,
                "tags": table_tags(tk.name),
                "excluded": tk.name in excluded,
                "ddl": tk.ddl,
                "columns": cols,
                "vector_text": self._synthesize_table_text(conn_id, tk),
                "vector_override": tk.vector_override or None,
            })

        return {
            "tables": tables_out,
            "graph": {
                "edges": self._graph.get(conn_id, {"edges": []}).get("edges", []),
                "excluded": sorted(excluded),
                "llm_draft_edges": self._llm_graph_edges.get(conn_id, []),
                "layout": self.graph_layout(conn_id),
            },
            "tags": self.tags(conn_id),
            "draft_count": draft_count,
            "tag_draft_count": sum(1 for v in lib.values() if v.get("status") == "draft"),
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

    def graph_layout(self, conn_id: str) -> dict[str, dict[str, Any]]:
        """2D 图布局坐标回读（表名 → {x,y}；仅含有坐标的表）。"""
        return {
            name: dict(tk.layout)
            for name, tk in self._tables.get(conn_id, {}).items() if tk.layout
        }

    def set_layout(self, conn_id: str, layout: dict[str, Any]) -> int:
        """写入 2D 图布局坐标（前端拖拽回传全量快照，spec §4 payload.layout）。

        仅接受已知表名与 {x,y} 数值点；未知表忽略。只改 TableKnowledge.layout
        并落盘快照——不动嵌入指纹，不触发重嵌（布局纯渲染态）。
        返回实际写入的表数。
        """
        tabs = self._tables.get(conn_id, {})
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
        if n:
            self._save_conn(conn_id)
        return n

    def add_graph_edge(self, conn_id: str, frm: str, to: str, kind: str,
                       frm_col: str | None = None, to_col: str | None = None,
                       weight: float | None = None,
                       cardinality: str = "n:1") -> dict[str, Any]:
        """新增一条图谱边（边 v2：字段级端点 + 基数；from 恒为多侧）。

        kind ∈ fk|overlap|user（user=手动连线）；cardinality ∈ n:1|1:1（默认 n:1）。
        去重按字段对（from/from_col/to/to_col/kind）全量匹配。
        """
        if kind not in ("fk", "overlap", "user"):
            raise ValueError("kind 必须是 fk|overlap|user")
        if cardinality not in ("n:1", "1:1"):
            raise ValueError("cardinality 必须是 n:1|1:1")
        tables = {t["name"] for t in self._schema.get(conn_id, {}).get("tables", [])}
        if frm not in tables or to not in tables:
            raise ValueError("未知表名")
        edges = self._graph.setdefault(conn_id, {"edges": []})["edges"]
        existing = next((e for e in edges
                         if e["from"] == frm and e["to"] == to and e.get("kind") == kind
                         and e.get("from_col") == frm_col and e.get("to_col") == to_col), None)
        if existing:
            return existing
        e = {"from": frm, "from_col": frm_col, "to": to, "to_col": to_col,
             "kind": kind, "weight": weight, "shared": None,
             "cardinality": cardinality,
             "reason": "" if kind != "user" else "人工连线"}
        edges.append(e)
        self._save_conn(conn_id)
        return e

    def remove_graph_edge(self, conn_id: str, frm: str, to: str, kind: str) -> int:
        """删除一条图谱边。删除结构/取值派生边时按字段对记入 tombstone，避免重建复活。"""
        edges = self._graph.get(conn_id, {"edges": []})["edges"]
        removed_edges = [e for e in edges
                         if e["from"] == frm and e["to"] == to and e.get("kind") == kind]
        edges[:] = [e for e in edges
                    if not (e["from"] == frm and e["to"] == to and e.get("kind") == kind)]
        removed = len(removed_edges)
        if removed and kind in ("overlap", "fk"):
            self._edge_tombstones.setdefault(conn_id, []).extend(
                {"from": e["from"], "from_col": e.get("from_col") or "",
                 "to": e["to"], "to_col": e.get("to_col") or ""}
                for e in removed_edges
            )
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
