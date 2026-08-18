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
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.knowledge.docs import KnowledgeDoc
from app.knowledge.embedding import Embedder, HashingEmbedder, cosine, make_embedder

if TYPE_CHECKING:
    from app.core.settings import SettingsStore


class KnowledgeBase:
    def __init__(self, data_dir: Path, runtime: "SettingsStore | None" = None) -> None:
        self._user_path = data_dir / "knowledge.json"
        self._runtime = runtime
        self._emb: Embedder = HashingEmbedder()
        self._auto: dict[str, list[KnowledgeDoc]] = {}
        self._user: dict[str, list[KnowledgeDoc]] = {}
        self._drafts: dict[str, list[KnowledgeDoc]] = {}
        self._samples: dict[str, dict[str, dict[str, list[Any]]]] = {}  # conn -> table -> column -> [values]
        self._graph: dict[str, dict[str, Any]] = {}                       # conn -> {edges}
        self._vec: dict[str, dict[str, list[float]]] = {}                 # conn -> doc_id -> 向量
        self._tags: dict[str, dict[str, dict[str, Any]]] = {}             # conn -> tag名 -> {description,status}
        self._table_tags: dict[str, dict[str, list[str]]] = {}            # conn -> table -> [tag名]
        self._schema: dict[str, dict[str, Any]] = {}                      # conn -> 表/列/外键快照（审查视图用）
        self._lock = threading.Lock()
        self._load()

    # ---------- 持久化（用户标注） ----------
    def _load(self) -> None:
        if not self._user_path.exists():
            return
        try:
            data = json.loads(self._user_path.read_text(encoding="utf-8"))
            for conn_id, docs in data.items():
                self._user[conn_id] = [KnowledgeDoc(**d) for d in docs]
        except Exception:
            pass

    def _save(self) -> None:
        with self._lock:
            self._user_path.write_text(
                json.dumps(
                    {k: [d.to_dict() for d in v] for k, v in self._user.items()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    # ---------- 自动抽取 ----------
    def _from_schema(self, schema: dict[str, Any]) -> list[KnowledgeDoc]:
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
    def _build_graph(self, conn_id: str, schema: dict[str, Any], samples: dict[str, dict[str, list[Any]]]) -> dict[str, Any]:
        edges: list[dict[str, Any]] = []
        # FK 边
        for fk in schema.get("foreign_keys", []):
            edges.append({
                "from": fk["table"], "from_col": fk["column"],
                "to": fk["ref_table"], "to_col": fk["ref_column"],
                "kind": "fk", "weight": 1.0,
            })
        # 值重叠边：同一样本值出现在两列 → 疑似 join（无 FK 也能连）
        cols_by_table: dict[str, dict[str, set[Any]]] = {}
        for t, cols in samples.items():
            cols_by_table[t] = {c: {str(v) for v in vals if v is not None} for c, vals in cols.items()}
        tables = list(cols_by_table.keys())
        seen: set[tuple[str, str]] = set()
        for i in range(len(tables)):
            for j in range(i + 1, len(tables)):
                ta, tb = tables[i], tables[j]
                for ca, sa in cols_by_table[ta].items():
                    if not sa:
                        continue
                    for cb, sb in cols_by_table[tb].items():
                        # 启发式去噪：同名主键 id↔id 的重叠无意义（不同表的自增 id 天然同范围）
                        if ca.lower() == cb.lower() == "id":
                            continue
                        inter = sa & sb
                        if not inter:
                            continue
                        key = tuple(sorted([f"{ta}.{ca}", f"{tb}.{cb}"]))
                        if key in seen:
                            continue
                        seen.add(key)
                        weight = round(len(inter) / max(1, min(len(sa), len(sb))), 3)
                        edges.append({
                            "from": ta, "from_col": ca,
                            "to": tb, "to_col": cb,
                            "kind": "overlap", "weight": weight,
                            "shared": len(inter),
                        })
        return {"edges": edges}

    # ---------- 构建 ----------
    def _embedder(self) -> Embedder:
        if self._runtime is not None:
            s = self._runtime.get()
            return make_embedder(s.embedding_provider, s.embedding_base_url, s.embedding_model, s.embedding_api_key)
        return HashingEmbedder()

    async def build(
        self,
        conn_id: str,
        schema: dict[str, Any],
        samples: dict[str, dict[str, list[Any]]] | None = None,
    ) -> dict[str, Any]:
        # TODO: 测试后删除
        from app.debuglog import dbg
        dbg("[kb.build] conn=", conn_id, "tables=", len(schema.get("tables", [])))
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
        self._graph[conn_id] = self._build_graph(conn_id, schema, self._samples.get(conn_id, {}))
        self._emb = self._embedder()
        await self._embed_docs(conn_id, self._auto[conn_id])
        self._persist_artifact(conn_id)
        return {
            "docs": len(self._auto[conn_id]),
            "graph_edges": len(self._graph.get(conn_id, {}).get("edges", [])),
            "sample_cols": sum(
                len(cols) for cols in self._samples.get(conn_id, {}).values()
            ),
        }

    async def _embed_docs(self, conn_id: str, docs: list[KnowledgeDoc]) -> None:
        vecs: dict[str, list[float]] = {}
        text_by_id = {d.id: self._doc_text(d) for d in docs}
        for did, text in text_by_id.items():
            try:
                vecs[did] = await self._emb.embed(text)
            except Exception:
                vecs[did] = [0.0]
        self._vec[conn_id] = {**self._vec.get(conn_id, {}), **vecs}

    # ---------- 持久化 artifact ----------
    def _artifact_path(self, conn_id: str) -> Path:
        return self._user_path.parent / f"knowledge-{conn_id}.json"

    def _persist_artifact(self, conn_id: str) -> None:
        try:
            data = {
                "auto": [d.to_dict() for d in self._auto.get(conn_id, [])],
                "drafts": [d.to_dict() for d in self._drafts.get(conn_id, [])],
                "samples": self._samples.get(conn_id, {}),
                "graph": self._graph.get(conn_id, {"edges": []}),
                "vec": self._vec.get(conn_id, {}),
                "tags": self._tags.get(conn_id, {}),
                "table_tags": self._table_tags.get(conn_id, {}),
                "schema": self._schema.get(conn_id, {}),
            }
            self._artifact_path(conn_id).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def load_artifact(self, conn_id: str) -> None:
        """启动时恢复构建过的知识（向量/图谱/草案），无需重新采样。"""
        p = self._artifact_path(conn_id)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._auto[conn_id] = [KnowledgeDoc(**d) for d in data.get("auto", [])]
            self._drafts[conn_id] = [KnowledgeDoc(**d) for d in data.get("drafts", [])]
            self._samples[conn_id] = data.get("samples", {})
            self._graph[conn_id] = data.get("graph", {"edges": []})
            self._vec[conn_id] = data.get("vec", {})
            self._tags[conn_id] = data.get("tags", {})
            self._table_tags[conn_id] = data.get("table_tags", {})
            self._schema[conn_id] = data.get("schema", {})
        except Exception:
            pass

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
        return (
            self._auto.get(conn_id, [])
            + self._drafts.get(conn_id, [])
            + self._user.get(conn_id, [])
        )

    def _neighbors(self, conn_id: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for e in self._graph.get(conn_id, {}).get("edges", []):
            out.setdefault(e["from"].lower(), set()).add(e["to"].lower())
            out.setdefault(e["to"].lower(), set()).add(e["from"].lower())
        return out

    def is_built(self, conn_id: str) -> bool:
        return conn_id in self._auto or self._artifact_path(conn_id).exists()

    async def retrieve(self, conn_id: str, query: str = "", table: str | None = None, k: int = 10) -> list[KnowledgeDoc]:
        if conn_id not in self._auto and self._artifact_path(conn_id).exists():
            self.load_artifact(conn_id)  # 重启后未重新构建也能用上次的知识
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

        base: dict[str, float] = {}
        for d in docs:
            s = kw_score(d)
            if qvec is not None:
                s += 2.0 * cosine(qvec, vecs.get(d.id, qvec))
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
            self._persist_artifact(conn_id)
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
            self._persist_artifact(conn_id)
        return len(targets)

    def reject(self, conn_id: str, doc_id: str) -> bool:
        drafts = self._drafts.get(conn_id, [])
        before = len(drafts)
        self._drafts[conn_id] = [d for d in drafts if d.id != doc_id]
        if len(self._drafts[conn_id]) != before:
            self._persist_artifact(conn_id)
            return True
        return False

    def reject_comment(self, conn_id: str, table: str, column: str | None = None) -> int:
        """按表/列拒绝草案注释（审查页用）。"""
        drafts = self._drafts.get(conn_id, [])
        targets = [d for d in drafts if d.table == table and d.column == column]
        kept = [d for d in drafts if d not in targets]
        if len(kept) != len(drafts):
            self._drafts[conn_id] = kept
            self._persist_artifact(conn_id)
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
            self._persist_artifact(conn_id)
        return added

    def assign_table_tags(self, conn_id: str, table: str, names: list[str]) -> int:
        """把标签绑定到表（去重保序）。"""
        keep = [n for n in dict.fromkeys(names) if n]
        self._table_tags.setdefault(conn_id, {})[table] = keep
        self._persist_artifact(conn_id)
        return len(keep)

    def confirm_tag(self, conn_id: str, name: str) -> bool:
        """人工确认标签 → 进入可路由标签库。"""
        lib = self._tags.get(conn_id, {})
        if name not in lib:
            return False
        lib[name]["status"] = "confirmed"
        self._persist_artifact(conn_id)
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
        self._persist_artifact(conn_id)
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

    def route_tables(self, conn_id: str, tag_names: list[str], hops: int = 2) -> dict[str, Any]:
        """意图→标签→候选表：打这些标签的表 + 沿 FK 多跳覆盖的表 → 候选子图。

        这是检索的核心：避免全表扫描，准确圈定表范围。
        只认已确认的标签——draft 标签不参与路由（不变式）。
        """
        confirmed = set(self.confirmed_tags(conn_id))
        tag_set = {t for t in tag_names if t in confirmed}
        # TODO: 测试后删除
        from app.debuglog import dbg
        dbg("[kb.route] conn=", conn_id, "req_tags=", tag_names, "confirmed_hit=", sorted(tag_set))
        if not tag_set:
            return {"tables": [], "edges": [], "seeded": 0}
        picks: set[str] = set()
        for table, names in self._table_tags.get(conn_id, {}).items():
            if set(names) & tag_set:
                picks.add(table)

        # FK 邻接表
        adj: dict[str, set[str]] = {}
        for e in self._graph.get(conn_id, {}).get("edges", []):
            if e["kind"] != "fk":
                continue
            adj.setdefault(e["from"], set()).add(e["to"])
            adj.setdefault(e["to"], set()).add(e["from"])

        frontier = set(picks)
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for t in frontier:
                nxt |= adj.get(t, set())
            nxt -= picks
            if not nxt:
                break
            picks |= nxt
            frontier = nxt

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
        self._save()
        return doc

    def list_docs(self, conn_id: str, table: str | None = None) -> list[KnowledgeDoc]:
        docs = self._docs(conn_id)
        if table:
            docs = [d for d in docs if d.table == table]
        return docs

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
            "graph": self._graph.get(conn_id, {"edges": []}),
            "tags": self.tags(conn_id),
            "draft_count": len(self._drafts.get(conn_id, [])),
            "tag_draft_count": sum(1 for v in self._tags.get(conn_id, {}).values() if v.get("status") == "draft"),
            "sample_cols": sum(len(cols) for cols in self._samples.get(conn_id, {}).values()),
            "embedding_provider": (
                self._runtime.get().embedding_provider if self._runtime else "hash"
            ),
        }

    def graph(self, conn_id: str) -> dict[str, Any]:
        return self._graph.get(conn_id, {"edges": []})

    def samples(self, conn_id: str) -> dict[str, dict[str, list[Any]]]:
        return self._samples.get(conn_id, {})

    def _neighbors_for(self, conn_id: str, table: str) -> list[dict[str, Any]]:
        """某表的图谱邻居（供图谱可视化高亮）。"""
        t = table.lower()
        return [
            e for e in self._graph.get(conn_id, {}).get("edges", [])
            if e["from"].lower() == t or e["to"].lower() == t
        ]
