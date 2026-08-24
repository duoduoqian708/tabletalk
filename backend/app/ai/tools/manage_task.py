"""manage_task 工具：定时任务完整 CRUD。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState


async def _manage_task(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    from app.ai.tasks.storage import TaskStore
    from app.config import get_env

    store = TaskStore(get_env().data_dir)
    action = (args or {}).get("action", "").strip()

    if action == "create":
        name = (args or {}).get("name", "").strip()
        cron = (args or {}).get("cron", "").strip()
        sql = (args or {}).get("sql", "").strip() or None
        natural_query = (args or {}).get("natural_query", "").strip() or None
        if not name or not cron:
            return ToolOutcome(result={"ok": False, "error": "创建任务需要 name 和 cron"})
        task = store.create(name, cron, conn_id, sql=sql, natural_query=natural_query)
        return ToolOutcome(
            result={"ok": True, "task": task, "message": f"任务 '{name}' 已创建，cron={cron}"},
            think=f"已创建定时任务：{name}。",
        )

    elif action == "list":
        tasks = store.list_all()
        return ToolOutcome(
            result={"ok": True, "tasks": tasks, "count": len(tasks)},
            think=f"共 {len(tasks)} 个定时任务。",
        )

    elif action == "get":
        task_id = (args or {}).get("task_id", "").strip()
        if not task_id:
            return ToolOutcome(result={"ok": False, "error": "查看任务需要 task_id"})
        task = store.get(task_id)
        if not task:
            return ToolOutcome(result={"ok": False, "error": f"任务 {task_id} 不存在"})
        runs = store.get_runs(task_id)
        return ToolOutcome(result={"ok": True, "task": task, "recent_runs": runs})

    elif action == "update":
        task_id = (args or {}).get("task_id", "").strip()
        if not task_id:
            return ToolOutcome(result={"ok": False, "error": "修改任务需要 task_id"})
        updates = {}
        for key in ("name", "cron", "sql", "natural_query", "enabled"):
            val = (args or {}).get(key)
            if val is not None:
                updates[key] = val
        task = store.update(task_id, **updates)
        if task:
            return ToolOutcome(result={"ok": True, "task": task, "message": "任务已更新。"})
        return ToolOutcome(result={"ok": False, "error": f"任务 {task_id} 不存在"})

    elif action == "delete":
        task_id = (args or {}).get("task_id", "").strip()
        if not task_id:
            return ToolOutcome(result={"ok": False, "error": "删除任务需要 task_id"})
        ok = store.delete(task_id)
        if ok:
            return ToolOutcome(result={"ok": True, "message": f"任务 {task_id} 已删除。"})
        return ToolOutcome(result={"ok": False, "error": f"任务 {task_id} 不存在"})

    elif action == "run":
        task_id = (args or {}).get("task_id", "").strip()
        if not task_id:
            return ToolOutcome(result={"ok": False, "error": "手动执行需要 task_id"})
        task = store.get(task_id)
        if not task:
            return ToolOutcome(result={"ok": False, "error": f"任务 {task_id} 不存在"})
        # 走 runner 校验链路（assess_sql → SELECT-only → 执行 → 审计），不成为旁路
        from app.ai.tasks.runner import run_task
        result = await run_task(state, task_id)
        if result.get("ok"):
            return ToolOutcome(
                result={"ok": True, "task": task, "run_result": result, "message": f"任务 '{task['name']}' 执行完成：{result.get('summary', '')}"},
                think=f"任务 {task['name']} 执行完成。",
            )
        return ToolOutcome(
            result={"ok": False, "error": result.get("summary", "执行失败"), "status": result.get("status")},
            think=f"任务 {task['name']} 执行失败：{result.get('summary', '')}",
        )

    return ToolOutcome(result={"ok": False, "error": f"未知操作: {action}"})


def register() -> None:
    register_tool(
        "manage_task",
        "管理定时任务：创建/查看/修改/删除/手动触发。",
        {
            "action": {"type": "string", "enum": ["create", "list", "get", "update", "delete", "run"], "description": "操作类型"},
            "task_id": {"type": "string", "description": "任务ID（get/update/delete/run时必填）"},
            "name": {"type": "string", "description": "任务名称（创建时必填）"},
            "cron": {"type": "string", "description": "cron表达式（创建/修改时）"},
            "sql": {"type": "string", "description": "执行的SQL"},
            "natural_query": {"type": "string", "description": "自然语言查询（与sql二选一）"},
            "enabled": {"type": "boolean", "description": "启用/禁用（修改时）"},
        },
        ["action"],
        _manage_task,
        trust="mutating",
        confirm="card",
    )
