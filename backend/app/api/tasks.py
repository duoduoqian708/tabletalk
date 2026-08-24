"""定时任务 API：/api/v1/tasks"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    name: str
    cron: str
    connection_id: str
    sql: str | None = None
    natural_query: str | None = None
    skill: str = "query"


class TaskUpdate(BaseModel):
    name: str | None = None
    cron: str | None = None
    sql: str | None = None
    natural_query: str | None = None
    enabled: bool | None = None


def _get_store(request):
    from app.config import get_env
    from app.ai.tasks.storage import TaskStore
    return TaskStore(get_env().data_dir)


@router.get("")
async def list_tasks(request) -> dict[str, Any]:
    store = _get_store(request)
    tasks = store.list_all()
    return {"ok": True, "tasks": tasks, "count": len(tasks)}


@router.post("")
async def create_task(body: TaskCreate, request) -> dict[str, Any]:
    store = _get_store(request)
    task = store.create(body.name, body.cron, body.connection_id, sql=body.sql, natural_query=body.natural_query, skill=body.skill)
    return {"ok": True, "task": task}


@router.get("/{task_id}")
async def get_task(task_id: str, request) -> dict[str, Any]:
    store = _get_store(request)
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")
    runs = store.get_runs(task_id)
    return {"ok": True, "task": task, "recent_runs": runs}


@router.put("/{task_id}")
async def update_task(task_id: str, body: TaskUpdate, request) -> dict[str, Any]:
    store = _get_store(request)
    updates = body.model_dump(exclude_none=True)
    task = store.update(task_id, **updates)
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")
    return {"ok": True, "task": task}


@router.delete("/{task_id}")
async def delete_task(task_id: str, request) -> dict[str, Any]:
    store = _get_store(request)
    ok = store.delete(task_id)
    if not ok:
        raise HTTPException(404, f"Task {task_id} not found")
    return {"ok": True, "message": f"Task {task_id} deleted"}


@router.post("/{task_id}/run")
async def run_task(task_id: str, request) -> dict[str, Any]:
    store = _get_store(request)
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")
    from app.state import get_state
    from app.ai.tasks.runner import run_task as _run_task
    state = get_state()
    result = await _run_task(state, task_id)
    return result
