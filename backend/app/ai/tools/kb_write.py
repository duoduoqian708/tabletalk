"""kb_write 工具：维护知识库文档和标签（draft → confirm 流程）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _kb_write(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    action = (args or {}).get("action", "").strip()
    target = (args or {}).get("target", "doc")  # doc | tag

    if not action:
        return ToolOutcome(result={"ok": False, "error": "缺少 action 参数（create/update/confirm/reject）"})

    if target == "tag":
        return _handle_tag(state, conn_id, args)

    # 文档操作（统一走 state.knowledge 门面：与构建产物同一存储/同一 id 体系）
    kb = state.knowledge
    if action == "create":
        table_name = (args or {}).get("table_name", "").strip()
        content = (args or {}).get("content", "").strip()
        if not table_name or not content:
            return ToolOutcome(result={"ok": False, "error": "创建文档需要 table_name 和 content"})
        doc = kb.annotate(conn_id, table_name, None, content)
        return ToolOutcome(
            result={"ok": True, "doc": doc.to_dict(), "message": f"文档已创建（id={doc.id}）。"},
            think=f"已为 {table_name} 创建文档。",
        )

    elif action == "update":
        doc_id = (args or {}).get("doc_id")
        content = (args or {}).get("content", "").strip()
        if not doc_id or not content:
            return ToolOutcome(result={"ok": False, "error": "更新文档需要 doc_id 和 content"})
        doc_id = str(doc_id)
        found = next((d for d in kb.list_docs(conn_id) if d.id == doc_id), None)
        if found is None:
            return ToolOutcome(result={"ok": False, "error": f"文档 {doc_id} 不存在。"})
        kb.delete_user_doc(conn_id, doc_id)
        doc = kb.annotate(conn_id, found.table, None, content)
        return ToolOutcome(result={"ok": True, "doc": doc.to_dict(), "message": "文档已更新。"})

    elif action == "confirm":
        doc_id = (args or {}).get("doc_id")
        table_name = (args or {}).get("table_name", "").strip()
        column = (args or {}).get("column", "").strip() or None
        if table_name:
            # P2-10/§15.3：确认表/列级 AI 注释草案（TableKnowledge/ColumnInfo status=draft）
            n = await kb.confirm(conn_id, table_name, column)
            if n:
                return ToolOutcome(result={"ok": True, "message": f"已确认 {table_name} 的注释草案（{n} 条），将参与检索。"})
            return ToolOutcome(result={"ok": False, "error": f"{table_name} 无待确认草案。"})
        if not doc_id:
            return ToolOutcome(result={"ok": False, "error": "确认文档需要 doc_id 或 table_name"})
        doc_id = str(doc_id)
        found = next((d for d in kb.list_docs(conn_id) if d.id == doc_id), None)
        if found is None:
            return ToolOutcome(result={"ok": False, "error": f"文档 {doc_id} 不存在。"})
        # 用户笔记/结构文档均已是权威态（confirmed）；如实告知，无需变更
        return ToolOutcome(result={"ok": True, "doc": found.to_dict(),
                                   "message": "文档已确认，将参与检索。"})

    elif action == "reject":
        doc_id = (args or {}).get("doc_id")
        if not doc_id:
            return ToolOutcome(result={"ok": False, "error": "拒绝文档需要 doc_id"})
        ok = kb.delete_user_doc(conn_id, str(doc_id))
        if ok:
            return ToolOutcome(result={"ok": True, "message": "文档已删除。"})
        return ToolOutcome(result={"ok": False, "error": f"文档 {doc_id} 不存在。"})

    return ToolOutcome(result={"ok": False, "error": f"未知操作: {action}"})


def _handle_tag(state, conn_id: str, args: dict | None) -> ToolOutcome:
    action = (args or {}).get("action", "").strip()
    name = (args or {}).get("name", "").strip()
    kb = state.knowledge

    if action == "create":
        tables = (args or {}).get("tables", [])
        if not name:
            return ToolOutcome(result={"ok": False, "error": "创建标签需要 name"})
        kb.create_tag(conn_id, name, description="", color="")
        for t in tables or []:
            kb.assign_table_tags(conn_id, t, [name])
        return ToolOutcome(
            result={"ok": True, "tag": {"name": name, "status": "draft", "tables": tables or []},
                    "message": f"标签 '{name}' 草稿已创建，等待确认。"},
            think=f"已创建标签草稿 {name}。",
        )

    elif action == "confirm":
        if not name:
            return ToolOutcome(result={"ok": False, "error": "确认标签需要 name"})
        ok = kb.confirm_tag(conn_id, name)
        if ok:
            return ToolOutcome(result={"ok": True, "message": f"标签 '{name}' 已确认，将参与路由。"})
        return ToolOutcome(result={"ok": False, "error": f"标签 '{name}' 不存在。"})

    elif action == "reject":
        if not name:
            return ToolOutcome(result={"ok": False, "error": "拒绝标签需要 name"})
        ok = kb.reject_tag(conn_id, name)
        if ok:
            return ToolOutcome(result={"ok": True, "message": f"草稿标签 '{name}' 已删除。"})
        return ToolOutcome(result={"ok": False, "error": f"标签 '{name}' 不存在或非草稿状态。"})

    return ToolOutcome(result={"ok": False, "error": f"未知标签操作: {action}"})


def register() -> None:
    register_tool(
        "kb_write",
        "维护知识库：创建/更新/确认/拒绝文档草稿，或创建/确认/拒绝领域标签。所有写入走 draft→confirm 流程。",
        {
            "action": {"type": "string", "enum": ["create", "update", "confirm", "reject"], "description": "操作类型"},
            "target": {"type": "string", "enum": ["doc", "tag"], "description": "操作对象（默认 doc）"},
            "table_name": {"type": "string", "description": "表名（创建文档时必填）"},
            "content": {"type": "string", "description": "文档内容（创建/更新时必填）"},
            "doc_id": {"type": "integer", "description": "文档ID（更新/确认/拒绝时必填）"},
            "column": {"type": "string", "description": "列名（确认列级注释草案时配合 table_name）"},
            "name": {"type": "string", "description": "标签名称（标签操作时必填）"},
            "tables": {"type": "array", "items": {"type": "string"}, "description": "标签关联的表列表（创建标签时）"},
        },
        [],
        _kb_write,
        trust="mutating",
        confirm="card",
    )
