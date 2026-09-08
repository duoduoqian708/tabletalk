"""受控计划 API：propose_plan 的人审确认/拒绝 + 链式分段执行。

语义（先审后动）：
- harness 模型提计划 → pending_plans（pending）→ plan_pending 事件 → 回合结束
- 用户确认 → 本端点执行"下一段"：顺序跑步骤，遇到需要人审的写确认卡（run_dml review）
  → 当前任务完成后挂起（剩余步骤写回 pending_plans），yield plan_awaiting
- 写确认本身仍走 DML confirm token 流（闸门 REVIEW 强制）；确认成功后前端再次调本端点续段
- 全部完成 → status=done + plan_done + 系统消息落库（模型下轮可见执行结果）
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.ai.dto import ChatMessage, ChatRequest
from app.ai.plan import TaskPlan, TaskSpec
from app.state import get_state

router = APIRouter(prefix="/api/v1/ai/plans", tags=["ai-plans"])

_ACTION_MAP = {"write": "write", "report": "query", "knowledge": "kb", "schedule": "schedule"}


def _steps_to_tasks(steps: list[dict]) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for i, s in enumerate(steps):
        action = _ACTION_MAP.get(str(s.get("action", "")), "query")
        target = {"instruction": str(s.get("description", ""))}
        if s.get("sql"):
            target["sql"] = str(s.get("sql"))
        tasks.append(TaskSpec(action=action, target=target, id=f"p{i + 1}"))
    return tasks


@router.post("/{plan_id}/confirm")
async def confirm_plan(plan_id: str, body: dict) -> StreamingResponse:
    state = get_state()
    row = state.chats.get_pending_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"计划不存在或已过期: {plan_id}")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"计划状态为 {row['status']}，不能确认")
    if (body.get("session_id") or "") != row["session_id"]:
        raise HTTPException(status_code=403, detail="会话不匹配")
    conn_id = state.chats.get_connection(row["session_id"])
    if not conn_id:
        raise HTTPException(status_code=409, detail="会话已失效（无归属数据源）")

    # 收尾段：所有步骤已执行、仅剩未决的写确认收尾 → 直接标记完成
    if not row["steps"]:
        state.chats.set_plan_status(plan_id, "done")
        state.chats.append_messages(row["session_id"], [{
            "role": "assistant", "kind": "system",
            "content": f"受控计划「{row['title']}」执行收尾完成（写操作确认已处理）。",
        }])

        async def _finalize():
            yield f"data: {json.dumps({'type': 'plan_started', 'plan_id': plan_id, 'title': row['title'], 'steps': 0}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'plan_done', 'plan_id': plan_id, 'completed': int(row.get('done_count') or 0)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_finalize(), media_type="text/event-stream")

    async def _segment():
        steps: list[dict] = row["steps"]
        tasks = _steps_to_tasks(steps)
        plan = TaskPlan(tasks=tasks)
        preq = ChatRequest(
            connection_id=conn_id,
            session_id=row["session_id"],
            messages=[ChatMessage(role="user", content=f"执行已确认的计划「{row['title']}」。")],
        )
        from app.ai.executor import execute_plan
        from app.ai.context_object import Context
        from app.ai.loop import task_runner

        ctx = Context(conn_id=conn_id, plan=plan, session_id=row["session_id"], include_data=False)
        completed = 0
        stop = False
        _base_done = int(row.get("done_count") or 0)  # 跨段累计进度
        yield f"data: {json.dumps({'type': 'plan_started', 'plan_id': plan_id, 'title': row['title'], 'steps': len(steps)}, ensure_ascii=False)}\n\n"
        try:
            async for ev in execute_plan(state, plan, ctx,
                                         lambda s, c, t: task_runner(s, preq, c, t)):
                t = ev.get("type")
                if t == "_commit":
                    # committed-turn：受控流内每轮落库（不下发前端）
                    try:
                        msgs = ev.get("messages") or []
                        if msgs:
                            state.chats.append_messages(row["session_id"], msgs)
                    except Exception:
                        pass
                    continue
                if t == "sql_card" and (ev.get("card") or {}).get("needs_confirm"):
                    stop = True  # 当前任务将挂起等人（写确认卡）
                if stop and t == "task_start":
                    break  # 不开始下一个任务
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if t == "task_done":
                    completed += 1
                    if stop:
                        break
            remaining = steps[completed:]
            if stop:
                # 写确认卡未决 → 必须挂起（无论是否还有剩余步骤；数据变更以 DML 确认为准）
                state.chats.create_pending_plan(
                    plan_id, row["session_id"], row["type"], row["title"] or "",
                    remaining, row["evidence"] or "", done_count=_base_done + completed)
                yield f"data: {json.dumps({'type': 'plan_awaiting', 'plan_id': plan_id, 'remaining': len(remaining)}, ensure_ascii=False)}\n\n"
            elif not remaining and completed >= len(steps):
                state.chats.set_plan_status(plan_id, "done")
                state.chats.append_messages(row["session_id"], [{
                    "role": "assistant", "kind": "system",
                    "content": f"受控计划「{row['title']}」已全部执行完成（{_base_done + completed} 步）。",
                }])
                yield f"data: {json.dumps({'type': 'plan_done', 'plan_id': plan_id, 'completed': _base_done + completed}, ensure_ascii=False)}\n\n"
            else:
                # 失败即停（plan_stopped）：计划标记失败，剩余不执行
                state.chats.set_plan_status(plan_id, "failed")
                state.chats.append_messages(row["session_id"], [{
                    "role": "assistant", "kind": "system",
                    "content": f"受控计划「{row['title']}」在第 {completed + 1} 步失败已停止（fail-stop），剩余步骤未执行。",
                }])
                yield f"data: {json.dumps({'type': 'plan_failed', 'plan_id': plan_id, 'completed': completed}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            state.chats.set_plan_status(plan_id, "failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_segment(), media_type="text/event-stream")


@router.post("/{plan_id}/reject")
async def reject_plan(plan_id: str, body: dict) -> dict[str, Any]:
    state = get_state()
    row = state.chats.get_pending_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"计划不存在或已过期: {plan_id}")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"计划状态为 {row['status']}，不能拒绝")
    if (body.get("session_id") or "") != row["session_id"]:
        raise HTTPException(status_code=403, detail="会话不匹配")
    state.chats.set_plan_status(plan_id, "rejected")
    state.chats.append_messages(row["session_id"], [{
        "role": "assistant", "kind": "system",
        "content": f"用户拒绝了计划「{row['title']}」。请根据用户反馈调整（可重新查证后再次提议）。",
    }])
    return {"ok": True, "plan_id": plan_id, "status": "rejected"}
