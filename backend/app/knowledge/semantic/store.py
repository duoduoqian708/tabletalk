"""L2 语义层：表知识/标签/文档"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.timeutil import utcnow_iso
from app.knowledge.docs import KnowledgeDoc

logger = logging.getLogger(__name__)

# 向量文本体系版本：进 emb 指纹，改动此值 → 全库自动重嵌（reembed_if_needed）
SYNTH_VERSION = "v3"
# 向量文本零代码拼接（2026-09 裁决）：唯一来源优先级
#   vector_override（人工覆盖）> vector_profile（AI 表画像，审核确认后）> 空。
# 空 = 该表本轮不生成新向量（保留旧向量继续可用），检索走词法+图谱通道。


class SemanticStore:
    """语义存储：负责表知识、标签、文档等"""

    def __init__(self) -> None:
        # 语义相关状态
        self._tables: dict[str, dict[str, Any]] = {}  # conn -> 表名 -> TableKnowledge
        self._tags: dict[str, dict[str, dict[str, Any]]] = {}  # conn -> tag名 -> {description,status}
        self._table_tags: dict[str, dict[str, list[str]]] = {}  # conn -> table -> [tag名]
        self._samples: dict[str, dict[str, dict[str, list[Any]]]] = {}  # conn -> table -> column -> [values]
        self._schema: dict[str, dict[str, Any]] = {}  # conn -> 表/列/外键快照
        self._user: dict[str, list[KnowledgeDoc]] = {}  # conn -> 用户手写文档
        # 本轮构建对比区（2026-09 审核重构，内存态；确认启用/放弃时清除）：
        # baseline = 旧版表知识（对比层"旧"侧）；diff = 结构增删改；tags_new = 全量划分的标签全集
        self._round: dict[str, dict[str, Any]] = {}

    # ---------- 本轮对比区（审核页数据源） ----------
    def set_round(self, conn_id: str, **kv: Any) -> None:
        self._round.setdefault(conn_id, {}).update(kv)

    def get_round(self, conn_id: str) -> dict[str, Any]:
        return self._round.get(conn_id, {})

    def clear_round(self, conn_id: str) -> None:
        self._round.pop(conn_id, None)

    def round_table_baseline(self, conn_id: str, table: str) -> dict[str, Any] | None:
        """单表旧版知识（对比层"旧"侧）：{comment, ddl, columns: {col: {comment, values, example}}}。"""
        base = (self.get_round(conn_id).get("baseline") or {}).get("tables", {}).get(table)
        return dict(base) if base else None

    def snapshot_round_baseline(self, conn_id: str) -> None:
        """构建发起时快照当前生效知识（对比基线）。必须在 _sync_table_shells/提案覆盖之前调用。"""
        tabs = self._tables.get(conn_id, {})
        if not tabs:
            return  # 首建无旧版 → baseline 为空，对比层全走 new
        tables: dict[str, Any] = {}
        for name, tk in tabs.items():
            tables[name] = {
                "comment": tk.comment,
                "ddl": tk.ddl,
                "columns": {cn: {"comment": ci.comment, "values": ci.values, "example": ci.example}
                            for cn, ci in tk.columns.items()},
            }
        self.set_round(conn_id, baseline={"tables": tables})

    def apply_round_tags(self, conn_id: str, keep_old: list[str], adopt_new: list[str],
                         save_conn_fn: Any = None) -> dict[str, int]:
        """标签版本制应用（审核页两栏混选，2026-09 语义修订）：

        应用后标签库 = 用户勾选结果：新版勾选（adopt_new）∪ 旧版勾选（keep_old），其余全部淘汰。
        - adopt_new：按新版划分生效——不存在则创建；**同名已存在也采用新版描述**（该怎样就是怎样，
          与旧集合无纠缠）；成员表按 round 划分覆盖绑定；
        - keep_old：旧 confirmed 勾选保留（纯对比辅助，勾选才回归）；
        - 未被勾选的一切（旧 confirmed + 本轮 draft）→ 删除 + 全表解绑。
        """
        round_new = {t.get("name"): t for t in (self.get_round(conn_id).get("tags_new") or [])}
        lib = self._tags.setdefault(conn_id, {})
        binds = self._table_tags.setdefault(conn_id, {})
        keep = set(keep_old) | set(adopt_new)
        for nm in adopt_new:
            desc = (round_new.get(nm) or {}).get("description", "")
            if nm in lib:
                lib[nm]["status"] = "confirmed"
                if desc:
                    lib[nm]["description"] = desc  # 同名也采用新版描述（按新结果应用）
            else:
                lib[nm] = {"description": desc, "status": "confirmed"}
            # 表绑定按新划分覆盖：members 之外的旧挂点解绑，members 内补挂
            members = set((round_new.get(nm) or {}).get("tables") or [])
            for tbl in list(binds.keys()):
                if nm in binds[tbl] and tbl not in members:
                    binds[tbl] = [x for x in binds[tbl] if x != nm]
            for tbl in members:
                lst = binds.setdefault(tbl, [])
                if nm not in lst:
                    lst.append(nm)
        removed = 0
        for nm in [n for n, v in list(lib.items()) if v.get("status") == "confirmed" and n not in keep]:
            del lib[nm]
            removed += 1
        for nm in [n for n, v in list(lib.items()) if v.get("status") == "draft" and n not in set(adopt_new)]:
            del lib[nm]
        for tbl in list(binds.keys()):
            binds[tbl] = [t for t in binds[tbl] if t in lib]
        if save_conn_fn:
            save_conn_fn(conn_id)
        return {"adopted": len(adopt_new), "kept_old": len([k for k in keep_old if k in lib]), "removed": removed}

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
        from app.knowledge.store import ColumnInfo, TableKnowledge  # noqa: PLC0415
        from app.knowledge.ddl_context import ddls_from_schema  # noqa: PLC0415

        tabs = self._tables.setdefault(conn_id, {})
        for t in (drop or set()):
            tabs.pop(t, None)
        tables_idx = {t["name"]: t for t in schema.get("tables", [])}
        cols_by_table: dict[str, list[dict[str, Any]]] = {}
        for c in schema.get("columns", []):
            cols_by_table.setdefault(c["table"], []).append(c)

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

    # ---------- 一表一 chunk（spec §4 向量合一；文本零代码拼接） ----------
    def table_vector_text(self, conn_id: str, tk: Any) -> str:
        """表向量文本（embedding 唯一来源）：人工覆盖 > AI 画像 > 空。

        空 = 无向量文本（画像未生成且无人工覆盖）：调用方跳过嵌入、保留旧向量。
        """
        return tk.vector_override or tk.vector_profile or ""

    def _table_payload(self, conn_id: str, tk: Any, synced_at: str = "") -> dict[str, Any]:
        """表级 chunk payload（spec §4）：结构化信息（渲染在 T4，此处仅存储透传）。"""
        # 2026-09 修订：待确认数 = 本轮提案数（当前生效内容不计入）
        draft_count = (1 if tk.has_proposal else 0) + sum(
            1 for ci in tk.columns.values() if ci.has_proposal
        )
        return {
            "ddl": tk.ddl,
            "tags": list(self._table_tags.get(conn_id, {}).get(tk.name, [])),
            "layout": tk.layout,
            "draft_count": draft_count,
            "updated_at": synced_at,
        }

    # ---------- AI 草案落库 + 人工确认（v2：写 TableKnowledge/ColumnInfo） ----------
    def annotate_drafts(self, conn_id: str, items: list[dict[str, Any]], save_conn_fn: Any = None) -> int:
        """AI 注释提案入库（2026-09 修订：写 proposed_*，当前生效值不动）。

        items: [{table, column|None, comment, values?, example?, profile?}]。
        - 列项 -> ColumnInfo.proposed_*；表级 -> TableKnowledge.proposed_comment / proposed_profile；
        - 当前 comment/status 完全不受影响（对比按钮/保持当前的基础）；
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
                ci.proposed_comment = comment
                if it.get("core"):
                    ci.core = True  # AI 提名关键列（保留到确认后；重提案按当轮输出刷新）
                new_values = str(it.get("values") or "").strip()
                new_example = str(it.get("example") or "").strip()[:60]
                # 只在有产出时覆盖提案；无产出保留空（apply 时空项不动当前值）
                if new_values:
                    ci.proposed_values = new_values
                if new_example:
                    ci.proposed_example = new_example
            else:
                tk.proposed_comment = comment
                new_profile = str(it.get("profile") or "").strip()
                if new_profile:
                    tk.proposed_profile = new_profile  # AI 表画像提案（confirm 时提升为 vector_profile）
            applied += 1
        if applied and save_conn_fn:
            save_conn_fn(conn_id)
        return applied

    async def confirm(self, conn_id: str, table: str | None = None, column: str | None = None,
                      save_conn_fn: Any = None, reembed_fn: Any = None,
                      table_only: bool = False,
                      collect_affected: list[str] | None = None) -> int:
        """确认（2026-09 修订：有提案 -> 提案提升为当前；无提案 -> 当前定稿）。

        column 指定 -> 单列；只给 table -> 该表及其全部列；都不给 -> 全库。
        table_only=true -> 仅表级提案（审核页表描述 radio 独立裁决，列提案不动）。
        提案提升改变合成文本 -> 收集受影响表一次性重嵌。
        collect_affected：调用方传入 list，就地填充实际发生变化的表名（按需嵌入用）。
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
                if ci is None:
                    continue
                if ci.has_proposal:
                    ci.apply_proposal()
                    n += 1
                    changed = True
                if ci.status == "draft":
                    n += 1  # 遗留 draft 状态迁移（旧数据）
                    changed = True
                if ci.status != "confirmed":
                    ci.status = "confirmed"
                    changed = True
                if changed:
                    affected.append(tk.name)
                continue
            if tk.has_proposal:
                tk.apply_proposal()
                n += 1
                changed = True
            elif tk.status == "draft":
                n += 1  # 遗留 draft 状态迁移
                changed = True
            if tk.status != "confirmed":
                tk.status = "confirmed"
                changed = True
            if table_only:
                # 仅表级（列提案留给字段逐项裁决）
                if changed:
                    affected.append(tk.name)
                continue
            for ci in tk.columns.values():
                if ci.has_proposal:
                    ci.apply_proposal()
                    n += 1
                    changed = True
                elif ci.status == "draft":
                    n += 1  # 遗留 draft 状态迁移
                    changed = True
                if ci.status != "confirmed":
                    ci.status = "confirmed"
                    changed = True
            if changed:
                affected.append(tk.name)
        if n and save_conn_fn:
            save_conn_fn(conn_id)
        if affected and reembed_fn:
            await reembed_fn(conn_id, affected)
        if collect_affected is not None:
            collect_affected.extend(affected)
        return n

    def clear(self, conn_id: str) -> None:
        """取消构建/失败后清理半成品内存（不落盘）。"""
        for d in (self._user, self._samples, self._tags,
                  self._table_tags, self._tables, self._schema):
            d.pop(conn_id, None)

    def clear_round_proposals(self, conn_id: str, tables: set[str] | None = None) -> int:
        """清空上轮字段提案（重建/增量前调用，防上轮提案残留被误确认）。

        只清 proposed_*（当前 comment/values 不动）；tables=None = 全部表。
        返回清掉的提案数（表级 + 列级）。
        """
        tabs = self._tables.get(conn_id, {})
        names = [t for t in tabs if tables is None or t in tables]
        n = 0
        for name in names:
            tk = tabs[name]
            if tk.clear_proposal():
                n += 1
            for ci in tk.columns.values():
                if ci.clear_proposal():
                    n += 1
        return n

    # ---------- 领域标签（每库一套，draft→人工确认） ----------
    def clear_tags(self, conn_id: str, save_conn_fn: Any = None) -> int:
        """全量重构：清空该连接的标签库与表→标签绑定（版本制下重建从零，无新旧共存）。"""
        lib = self._tags.pop(conn_id, {})
        bound = self._table_tags.pop(conn_id, {})
        n = len(lib)
        if n or bound:
            if save_conn_fn:
                save_conn_fn(conn_id)
        return n

    def upsert_tags(self, conn_id: str, tags: list[dict[str, Any]], save_conn_fn: Any = None) -> int:
        """AI 提案的标签入库（新标签为 draft，已存在不重复）。tags: [{name, description}]"""
        lib = self._tags.setdefault(conn_id, {})
        added = 0
        for t in tags:
            name = (t.get("name") or "").strip()
            if not name or name in lib:
                continue
            lib[name] = {"description": (t.get("description") or "").strip(), "status": "draft"}
            added += 1
        if added and save_conn_fn:
            save_conn_fn(conn_id)
        return added

    def create_tag(self, conn_id: str, name: str, description: str = "", color: str = "",
                   save_conn_fn: Any = None) -> bool:
        """人工新建标签（直接 confirmed，立即可用可路由）。color 为空 → 前端回退哈希色板。"""
        name = (name or "").strip()
        lib = self._tags.setdefault(conn_id, {})
        if not name or name in lib:
            return False
        lib[name] = {
            "description": (description or "").strip(),
            "status": "confirmed",
            "color": (color or "").strip(),
        }
        if save_conn_fn:
            save_conn_fn(conn_id)
        return True

    def update_tag(self, conn_id: str, old_name: str, new_name: str | None = None,
                   description: str | None = None, color: str | None = None,
                   save_conn_fn: Any = None) -> bool:
        """人工编辑标签：改名（同步所有表绑定）/改描述/改颜色。"""
        lib = self._tags.setdefault(conn_id, {})
        if old_name not in lib:
            return False
        nn = (new_name or "").strip() if new_name is not None else old_name
        if nn and nn != old_name and nn in lib:
            raise ValueError(f"标签 {nn} 已存在")
        if description is not None:
            lib[old_name]["description"] = description.strip()
        if color is not None:
            lib[old_name]["color"] = (color or "").strip()
        if nn and nn != old_name:
            lib[nn] = lib.pop(old_name)
            for t, names in self._table_tags.get(conn_id, {}).items():
                self._table_tags[conn_id][t] = [nn if n == old_name else n for n in names]
        if save_conn_fn:
            save_conn_fn(conn_id)
        return True

    def assign_table_tags(self, conn_id: str, table: str, names: list[str],
                          save_conn_fn: Any = None, merge: bool = False) -> int:
        """把标签绑定到表（去重保序）；库中不存在的标签自动补为 draft。

        merge=True（AI 重建提案用）：与既有绑定**并集**（已确认标签的
        绑定保留，新的提案标签追加）——重建不冲掉当前生效的标签绑定。
        merge=False（用户手动调整）：整体替换。
        """
        proposed = [n for n in dict.fromkeys(names) if n]
        lib = self._tags.setdefault(conn_id, {})
        for n in proposed:
            if n not in lib:
                lib[n] = {"description": "", "status": "draft"}
        current = self._table_tags.get(conn_id, {}).get(table, [])
        if merge:
            # 保留既有绑定中仍在库的标签（已确认 + 历史 draft），追加新提案
            old_ok = [n for n in current if n in lib]
            keep = old_ok + [n for n in proposed if n not in old_ok]
        else:
            keep = proposed
        self._table_tags.setdefault(conn_id, {})[table] = keep
        if save_conn_fn:
            save_conn_fn(conn_id)
        return len(keep)

    def confirm_tag(self, conn_id: str, name: str, save_conn_fn: Any = None) -> bool:
        """人工确认标签 → 进入可路由标签库。"""
        lib = self._tags.get(conn_id, {})
        if name not in lib:
            return False
        lib[name]["status"] = "confirmed"
        if save_conn_fn:
            save_conn_fn(conn_id)
        return True

    def reject_tag(self, conn_id: str, name: str, save_conn_fn: Any = None) -> bool:
        """拒绝标签：从库移除 + 从所有表上解除。"""
        lib = self._tags.get(conn_id, {})
        if name not in lib:
            return False
        del lib[name]
        for t, names in self._table_tags.get(conn_id, {}).items():
            if name in names:
                self._table_tags[conn_id][t] = [n for n in names if n != name]
        if save_conn_fn:
            save_conn_fn(conn_id)
        return True

    def tags(self, conn_id: str) -> dict[str, Any]:
        lib = self._tags.get(conn_id, {})
        return {
            "library": [
                {
                    "name": n,
                    "description": v.get("description", ""),
                    "status": v.get("status", "draft"),
                    "color": v.get("color", ""),
                }
                for n, v in lib.items()
            ],
            "tables": self._table_tags.get(conn_id, {}),
        }

    def confirmed_tags(self, conn_id: str) -> list[str]:
        return [n for n, v in self._tags.get(conn_id, {}).items() if v.get("status") == "confirmed"]

    def pending_counts(self, conn_id: str) -> dict[str, int]:
        """待确认数（确认闸 UI 用）：draft 注释（表+列）+ draft 标签。"""
        n_comments = 0
        for tk in self._tables.get(conn_id, {}).values():
            if tk.has_proposal:
                n_comments += 1
            n_comments += sum(1 for ci in tk.columns.values() if ci.has_proposal)
        return {
            "draft_docs": n_comments,
            "draft_tags": sum(1 for v in self._tags.get(conn_id, {}).values() if v.get("status") == "draft"),
        }

    def has_confirmed_content(self, conn_id: str) -> bool:
        """库中是否存在任何 confirmed 内容（放弃后 kb_status 流转判定）：
        任一列/表注释 confirmed 或任一标签 confirmed 即视为有历史。"""
        if any(
            tk.status == "confirmed" or any(ci.status == "confirmed" for ci in tk.columns.values())
            for tk in self._tables.get(conn_id, {}).values()
        ):
            return True
        return any(v.get("status") == "confirmed" for v in self._tags.get(conn_id, {}).values())

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
        save_conn_fn: Any = None,
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
            updated_at=utcnow_iso(),
        )
        self._user.setdefault(conn_id, []).append(doc)
        if save_conn_fn:
            save_conn_fn(conn_id)
        return doc

    def _docs(self, conn_id: str, auto: dict[str, list[KnowledgeDoc]] | None = None) -> list[KnowledgeDoc]:
        """全部活跃文档（过滤 archived）——仅剩结构文档 + 用户手写笔记（列表/审计用，
        不再进向量检索；检索统一走表级知识卡）。"""
        auto_docs = (auto or {}).get(conn_id, [])
        return [
            d for d in (
                auto_docs
                + self._user.get(conn_id, [])
            )
            if not d.archived
        ]

    def list_docs(self, conn_id: str, table: str | None = None, ensure_loaded_fn: Any = None,
                  auto: dict[str, list[KnowledgeDoc]] | None = None) -> list[KnowledgeDoc]:
        if ensure_loaded_fn:
            ensure_loaded_fn(conn_id)
        docs = self._docs(conn_id, auto)
        if table:
            docs = [d for d in docs if d.table == table]
        return docs

    def delete_user_doc(self, conn_id: str, doc_id: str, save_conn_fn: Any = None) -> bool:
        """删除一条用户手写文档（仅 usr- 前缀可删）。"""
        if not doc_id.startswith("usr-"):
            return False
        users = self._user.get(conn_id, [])
        before = len(users)
        self._user[conn_id] = [d for d in users if d.id != doc_id]
        if len(self._user[conn_id]) != before:
            if save_conn_fn:
                save_conn_fn(conn_id)
            return True
        return False

    def table_card(self, conn_id: str, table: str, ensure_loaded_fn: Any = None,
                   synced_at: str = "") -> dict[str, Any] | None:
        """单表知识卡（kb_read 单表查询用）：{table, text, payload}。"""
        if ensure_loaded_fn:
            ensure_loaded_fn(conn_id)
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            return None
        return {
            "table": table,
            "text": self.table_vector_text(conn_id, tk),
            "payload": self._table_payload(conn_id, tk, synced_at),
        }

    def table_cards(self, conn_id: str, ensure_loaded_fn: Any = None,
                    synced_at: str = "") -> list[dict[str, Any]]:
        """全部表知识卡（kb_read 全量查询用）。"""
        if ensure_loaded_fn:
            ensure_loaded_fn(conn_id)
        out: list[dict[str, Any]] = []
        for name in self._tables.get(conn_id, {}):
            card = self.table_card(conn_id, name, synced_at=synced_at)
            if card:
                out.append(card)
        return out

    def samples(self, conn_id: str) -> dict[str, dict[str, list[Any]]]:
        return self._samples.get(conn_id, {})

    def field_history(self, conn_id: str, table: str, column: str, storage_fn: Any = None) -> list[dict[str, Any]]:
        """字段历史版本（倒序，供「版本回溯」）。"""
        if not storage_fn:
            return []
        try:
            return storage_fn(conn_id).field_history(table, column)
        except Exception:
            return []

    def apply_field_history(self, conn_id: str, table: str, column: str, history_id: int,
                            storage_fn: Any = None, save_conn_fn: Any = None) -> bool:
        """用历史版本覆盖当前字段（comment/values/example，状态不变）。"""
        if not storage_fn:
            return False
        try:
            row = storage_fn(conn_id).archive_row(history_id)
        except Exception:
            row = None
        if not row or row["kind"] != "column" or row["table"] != table or row["column"] != column:
            return False
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            return False
        ci = tk.columns.get(column)
        if ci is None:
            return False
        p = row["payload"]
        ci.comment = p.get("comment", "")
        ci.values = p.get("values", "")
        ci.example = p.get("example", "")
        if save_conn_fn:
            save_conn_fn(conn_id)
        return True

    async def edit_table_knowledge(
        self, conn_id: str, table: str,
        table_comment: str | None = None,
        column_comments: list[dict[str, Any]] | None = None,
        vector_text: str | None = None,
        save_conn_fn: Any = None,
        reembed_fn: Any = None,
    ) -> dict[str, Any]:
        """人工按表编辑知识（详情面板两块，只写知识字段）。

        - table_comment：表级注释（人工写入 → 权威 confirmed）；
        - column_comments：[{name, comment?, values?, example?}] 每列仅改给定字段，
          列不存在即报错（避免静默丢字段）；多字段任一改动即列 confirmed；
        - vector_text：向量化片段覆盖（'' 清空覆盖，回落 AI 画像）。
        schema 镜像字段（列类型/PK/FK/DDL/表名）不可改写，type 恒 table_schema。
        编辑改变向量文本 → 该表即时重嵌（无文本表跳过、保留旧向量）。
        """
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            raise KeyError(table)
        changed = False

        if table_comment is not None:
            if tk.comment != table_comment:
                changed = True
            tk.comment = table_comment
            # 人工写入 = 权威（覆盖注释提案 -> 清提案防"编辑后还挂一个旧提案"）；
            # 画像提案（proposed_profile）不动——注释编辑不代表否决画像
            tk.proposed_comment = ""
            tk.status = "confirmed" if table_comment else "none"

        if column_comments:
            for edit in column_comments:
                name = edit.get("name", "")
                ci = tk.columns.get(name)
                if ci is None:
                    raise KeyError(f"{table}.{name}")
                ci.clear_proposal()
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
            if save_conn_fn:
                save_conn_fn(conn_id)
            if reembed_fn:
                await reembed_fn(conn_id, [table])
        return {
            "changed": changed,
            "table": table,
            "vector_text": self.table_vector_text(conn_id, tk),
            "vector_override": tk.vector_override or None,
            "vector_profile": tk.vector_profile or None,
            "proposed_profile": tk.proposed_profile or None,
        }

    async def reject(self, conn_id: str, table: str, column: str | None = None,
                     save_conn_fn: Any = None, reembed_fn: Any = None) -> int:
        """拒绝草案 = 保持当前（委托 reject_comment：清提案不动当前值）。"""
        return await self.reject_comment(conn_id, table, column, save_conn_fn, reembed_fn)

    async def reject_comment(self, conn_id: str, table: str, column: str | None = None,
                             save_conn_fn: Any = None, reembed_fn: Any = None) -> int:
        """保持当前（2026-09 修订：清除提案，当前生效值不动）。

        column 指定 -> 单列清提案；不给 column -> 全表清提案（含全部列的提案）。
        提案不入合成文本 -> 清除无需重嵌。
        """
        tk = self._tables.get(conn_id, {}).get(table)
        if tk is None:
            return 0
        n = 0
        if column:
            ci = tk.columns.get(column)
            if ci and ci.clear_proposal():
                n += 1
        else:
            if tk.clear_proposal():
                n += 1
            for ci in tk.columns.values():
                if ci.clear_proposal():
                    n += 1
        if n and save_conn_fn:
            save_conn_fn(conn_id)
        return n
