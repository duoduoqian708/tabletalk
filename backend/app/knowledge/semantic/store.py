"""L2 语义层：表知识/标签/文档"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.timeutil import utcnow_iso
from app.knowledge.docs import KnowledgeDoc

logger = logging.getLogger(__name__)


class SemanticStore:
    """语义存储：负责表知识、标签、文档等"""

    def __init__(self) -> None:
        # 语义相关状态
        self._tables: dict[str, dict[str, Any]] = {}  # conn -> 表名 -> TableKnowledge
        self._tags: dict[str, dict[str, dict[str, Any]]] = {}  # conn -> tag名 -> {description,status}
        self._table_tags: dict[str, dict[str, list[str]]] = {}  # conn -> table -> [tag名]
        self._samples: dict[str, dict[str, dict[str, list[Any]]]] = {}  # conn -> table -> column -> [values]
        self._schema: dict[str, dict[str, Any]] = {}  # conn -> 表/列/外键快照
        self._supplemented: dict[str, set[str]] = {}  # conn -> 本次注释补充了取值知识的表
        self._user: dict[str, list[KnowledgeDoc]] = {}  # conn -> 用户手写文档

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

    # ---------- 一表一 chunk（spec §4 向量合一） ----------
    def _synthesize_table_text(self, conn_id: str, tk: Any) -> str:
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

    def _table_payload(self, conn_id: str, tk: Any, synced_at: str = "") -> dict[str, Any]:
        """表级 chunk payload（spec §4）：结构化信息（渲染在 T4，此处仅存储透传）。"""
        draft_count = (1 if tk.status == "draft" else 0) + sum(
            1 for ci in tk.columns.values() if ci.status == "draft"
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
        """AI 注释草案入库（status=draft），待人工确认。

        items: [{table, column|None, comment, values?, example?}]。
        - 列项 → ColumnInfo(comment/values/example)；表级 → TableKnowledge.comment；
        - 已确认（confirmed）内容不被草案覆盖：comment 保留原样，但**缺失的
          values/example 会被补充**（带样本重建时 AI 新知识进得来）；
        - 补充的表记入 _supplemented，由调用方取走重嵌（合成文本已变化）；
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
                new_values = str(it.get("values") or "").strip()
                new_example = str(it.get("example") or "").strip()[:60]
                if ci.status == "confirmed":
                    # 已确认：不覆盖注释/状态；缺失的取值知识补充（样本授权重建时）
                    if new_values and not ci.values:
                        ci.values = new_values
                        self._supplemented.setdefault(conn_id, set()).add(table)
                    if new_example and not ci.example:
                        ci.example = new_example
                        self._supplemented.setdefault(conn_id, set()).add(table)
                    continue
                ci.comment = comment
                # values/example 只在 AI 有新产出时更新；无样本构建不返回 →
                # 保留已有取值知识，防止"未勾选确认的重建"清空历史成果
                if new_values:
                    ci.values = new_values
                if new_example:
                    ci.example = new_example
                ci.status = "draft"
            else:
                if tk.status == "confirmed":
                    continue  # 已确认内容不被草案覆盖
                tk.comment = comment
                tk.status = "draft"
            applied += 1
        if applied or self._supplemented.get(conn_id):
            if save_conn_fn:
                save_conn_fn(conn_id)
        return applied

    def take_supplemented(self, conn_id: str) -> list[str]:
        """取走本次注释中补充了取值知识的表（合成文本已变 → 调用方重嵌）。"""
        return list(self._supplemented.pop(conn_id, set()))

    async def confirm(self, conn_id: str, table: str | None = None, column: str | None = None,
                      save_conn_fn: Any = None, reembed_fn: Any = None) -> int:
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
        if n and save_conn_fn:
            save_conn_fn(conn_id)
        if affected and reembed_fn:
            await reembed_fn(conn_id, affected)
        return n

    def clear(self, conn_id: str) -> None:
        """取消构建/失败后清理半成品内存（不落盘）。"""
        for d in (self._user, self._samples, self._tags,
                  self._table_tags, self._tables, self._schema, self._supplemented):
            d.pop(conn_id, None)

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
                          save_conn_fn: Any = None) -> int:
        """把标签绑定到表（去重保序）；库中不存在的标签自动补为 draft（否则 overview 不可见、无法确认）。"""
        keep = [n for n in dict.fromkeys(names) if n]
        lib = self._tags.setdefault(conn_id, {})
        for n in keep:
            if n not in lib:
                lib[n] = {"description": "", "status": "draft"}
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
            if tk.status == "draft":
                n_comments += 1
            n_comments += sum(1 for ci in tk.columns.values() if ci.status == "draft")
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
            "text": self._synthesize_table_text(conn_id, tk),
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
            if save_conn_fn:
                save_conn_fn(conn_id)
            if reembed_fn:
                await reembed_fn(conn_id, [table])
        return {
            "changed": changed,
            "table": table,
            "vector_text": self._synthesize_table_text(conn_id, tk),
            "vector_override": tk.vector_override or None,
        }

    async def reject(self, conn_id: str, table: str, column: str | None = None,
                     save_conn_fn: Any = None, reembed_fn: Any = None) -> int:
        """拒绝草案注释（v2：按表/列撤下；旧 doc_id 版本随草稿文档退役）。"""
        return await self.reject_comment(conn_id, table, column, save_conn_fn, reembed_fn)

    async def reject_comment(self, conn_id: str, table: str, column: str | None = None,
                             save_conn_fn: Any = None, reembed_fn: Any = None) -> int:
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
            if save_conn_fn:
                save_conn_fn(conn_id)
            if reembed_fn:
                await reembed_fn(conn_id, [table])
        return n
