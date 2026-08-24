"""kb_write 工具：维护知识库文档和标签（draft → confirm 流程）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _kb_write(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    from app.ai.knowledge.store import KnowledgeStore
    from app.config import get_env

    store = KnowledgeStore(get_env().data_dir)
    action = (args or {}).get("action", "").strip()
    target = (args or {}).get("target", "doc")  # doc | tag

    if not action:
        return ToolOutcome(result={"ok": False, "error": "缺少 action 参数（create/update/confirm/reject）"})

    if target == "tag":
        return _handle_tag(store, conn_id, args)

    # 文档操作
    if action == "create":
        table_name = (args or {}).get("table_name", "").strip()
        content = (args or {}).get("content", "").strip()
        if not table_name or not content:
            return ToolOutcome(result={"ok": False, "error": "创建文档需要 table_name 和 content"})
        doc = store.create_doc(conn_id, table_name, content)
        return ToolOutcome(
            result={"ok": True, "doc": doc, "message": f"文档草稿已创建（id={doc['id']}），等待人工确认。"},
            think=f"已为 {table_name} 创建文档草稿。",
        )

    elif action == "update":
        doc_id = (args or {}).get("doc_id")
        content = (args or {}).get("content", "").strip()
        if not doc_id or not content:
            return ToolOutcome(result={"ok": False, "error": "更新文档需要 doc_id 和 content"})
        doc = store.update_doc(conn_id, int(doc_id), content)
        if doc:
            return ToolOutcome(result={"ok": True, "doc": doc, "message": "文档已更新。"})
        return ToolOutcome(result={"ok": False, "error": f"文档 {doc_id} 不存在。"})

    elif action == "confirm":
        doc_id = (args or {}).get("doc_id")
        if not doc_id:
            return ToolOutcome(result={"ok": False, "error": "确认文档需要 doc_id"})
        doc = store.confirm_doc(conn_id, int(doc_id))
        if doc:
            return ToolOutcome(result={"ok": True, "doc": doc, "message": "文档已确认，将参与路由。"})
        return ToolOutcome(result={"ok": False, "error": f"文档 {doc_id} 不存在。"})

    elif action == "reject":
        doc_id = (args or {}).get("doc_id")
        if not doc_id:
            return ToolOutcome(result={"ok": False, "error": "拒绝文档需要 doc_id"})
        ok = store.reject_doc(conn_id, int(doc_id))
        if ok:
            return ToolOutcome(result={"ok": True, "message": "草稿文档已删除。"})
        return ToolOutcome(result={"ok": False, "error": f"文档 {doc_id} 不存在或非草稿状态。"})

    return ToolOutcome(result={"ok": False, "error": f"未知操作: {action}"})


def _handle_tag(store, conn_id: str, args: dict | None) -> ToolOutcome:
    action = (args or {}).get("action", "").strip()
    name = (args or {}).get("name", "").strip()

    if action == "create":
        tables = (args or {}).get("tables", [])
        if not name:
            return ToolOutcome(result={"ok": False, "error": "创建标签需要 name"})
        tag = store.create_tag(conn_id, name, tables)
        return ToolOutcome(
            result={"ok": True, "tag": tag, "message": f"标签 '{name}' 草稿已创建，等待确认。"},
            think=f"已创建标签草稿 {name}。",
        )

    elif action == "confirm":
        if not name:
            return ToolOutcome(result={"ok": False, "error": "确认标签需要 name"})
        ok = store.confirm_tag(conn_id, name)
        if ok:
            return ToolOutcome(result={"ok": True, "message": f"标签 '{name}' 已确认，将参与路由。"})
        return ToolOutcome(result={"ok": False, "error": f"标签 '{name}' 不存在。"})

    elif action == "reject":
        if not name:
            return ToolOutcome(result={"ok": False, "error": "拒绝标签需要 name"})
        ok = store.reject_tag(conn_id, name)
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
            "name": {"type": "string", "description": "标签名称（标签操作时必填）"},
            "tables": {"type": "array", "items": {"type": "string"}, "description": "标签关联的表列表（创建标签时）"},
        },
        [],
        _kb_write,
        trust="mutating",
        confirm="card",
    )
