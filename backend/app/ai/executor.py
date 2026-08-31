"""E4 任务循环执行器（设计 §12/§14）：Plan-and-Execute 的外层。

- TaskPlan 顺序执行：每任务 route(action, modality) → skill → 任务执行器（ReAct）
- 结果统一 TaskResult 块（§19），写 Context.task_results（§18 共享）
- 失败即停：当前任务失败且剩余任务含写意图 → 停；读任务失败不阻塞后续读
- 写任务前置校验：依赖的读任务必须先成功
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable

from app.ai.context_object import Context
from app.ai.plan import TaskPlan, TaskSpec
from app.ai.skills.route import route_effective as route

if TYPE_CHECKING:
    from app.state import AppState

logger = logging.getLogger("ai.executor")

# 任务结果块统一结构（§19）
RESULT_TYPES = {"table", "report", "confirm", "text"}


@dataclass
class TaskResult:
    type: str = "text"                # table | report | confirm | text
    content: str = ""
    task_id: str = ""
    skill_id: str = ""
    action: str = ""
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)  # 附加（rows/columns/确认信息...）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "content": self.content,
                             "task_id": self.task_id, "skill_id": self.skill_id,
                             "action": self.action, "ok": self.ok}
        if self.data:
            d["data"] = self.data
        if self.error:
            d["error"] = self.error
        return d


# 任务执行器签名：state, ctx, task → AsyncGenerator[dict, None]（SSE 事件流）
TaskExecutor = Callable[..., AsyncGenerator[dict[str, Any], None]]


def should_stop(plan: TaskPlan, failed_idx: int) -> bool:
    """失败即停判定（§14 规则 3）：

    - 当前任务失败且剩余任务含写/ddl 意图 → 停（写依赖读，读没成功写不能继续）
    - 当前任务失败但剩余全为读 → 不停（读失败不阻塞后续读）
    """
    remaining = plan.tasks[failed_idx + 1:]
    if not remaining:
        return False
    if any(t.trust in ("write", "ddl") for t in remaining):
        return True
    return False


def validate_write_precondition(plan: TaskPlan, task: TaskSpec, ctx: Context) -> str | None:
    """写任务前置校验：该写任务之前的所有任务必须成功（否则写基于不完整读，拒绝）。"""
    if task.trust != "write":
        return None
    idx = next((i for i, t in enumerate(plan.tasks) if t.id == task.id), None)
    if idx is None:
        return None
    for t in plan.tasks[:idx]:
        res = ctx.get_result(t.id)
        if res is None or not getattr(res, "ok", True):
            return f"写任务依赖的前序任务 {t.id or t.action} 未成功，拒绝执行写操作（失败即停）"
    return None


async def execute_plan(
    state: "AppState",
    plan: TaskPlan,
    ctx: Context,
    executor: TaskExecutor,
    on_task_start: Callable[[TaskSpec, str], dict[str, Any]] | None = None,
    on_task_result: Callable[[TaskSpec, TaskResult], dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """任务循环：顺序执行 TaskPlan，产 SSE 事件流。

    executor：单任务执行器（ReAct），签名 (state, ctx, task) → 事件流；
    事件流内应含 task_done（由本函数统一发出 task_start/task_done/task_result）。
    """
    if not plan.tasks:
        yield {"type": "task_done", "id": "none", "ok": False, "error": "空任务计划"}
        return

    for idx, task in enumerate(plan.tasks):
        task_id = task.id or f"t{idx + 1}"
        task.id = task_id
        skill_id = route(task.action, task.modality)
        ctx.current_task = task
        ctx.current_skill = skill_id

        # 写任务前置校验（依赖读成功）
        pre_err = validate_write_precondition(plan, task, ctx)
        if pre_err:
            res = TaskResult(type="confirm", task_id=task_id, skill_id=skill_id,
                             action=task.action, ok=False, error=pre_err)
            ctx.set_result(task_id, res)
            logger.warning("[executor] %s %s", pre_err, task_id)
            yield {"type": "task_start", "id": task_id, "skill": skill_id, "action": task.action}
            yield {"type": "task_result", "index": idx + 1, "id": task_id,
                   "result_type": "confirm", "content": "", "ok": False,
                   "error": pre_err, "data": {}}
            yield {"type": "task_done", "id": task_id, "ok": False, "error": pre_err}
            # 写任务被拒 → 后续写任务也停（含写意图）
            if should_stop(plan, idx):
                yield {"type": "plan_stopped", "reason": pre_err}
                return
            continue

        # 发任务开始（允许调用方扩展事件）
        yield {"type": "task_start", "id": task_id, "skill": skill_id, "action": task.action}
        if on_task_start:
            extra = on_task_start(task, skill_id)
            if extra:
                yield extra

        # 任务执行器跑 ReAct（事件透传）
        task_events: list[dict[str, Any]] = []
        async for ev in executor(state, ctx, task):
            task_events.append(ev)
            yield ev

        # 汇总任务结果（从事件流提取：sql_card/最后 text/confirm）
        result = _summarize_task(task, task_events, skill_id)
        ctx.set_result(task_id, result)
        if on_task_result:
            extra = on_task_result(task, result)
            if extra:
                yield extra
        _rd = result.to_dict()
        yield {"type": "task_result", "index": idx + 1, "id": task_id,
               "result_type": _rd.get("type", "text"),
               "content": _rd.get("content", ""),
               "ok": _rd.get("ok", True),
               "error": _rd.get("error"),
               "data": _rd.get("data") or {}}
        yield {"type": "task_done", "id": task_id, "ok": result.ok,
               "error": result.error, "result_type": result.type}

        # 失败即停
        if not result.ok and should_stop(plan, idx):
            logger.info("[executor] 任务 %s 失败，后续含写任务，停止计划", task_id)
            yield {"type": "plan_stopped", "reason": f"任务 {task_id} 失败（含写任务，停止）"}
            return


def _summarize_task(task: TaskSpec, events: list[dict[str, Any]], skill_id: str) -> TaskResult:
    """从任务事件流汇总 TaskResult（§19 结果块）：

    - 工具失败（error 事件 或 subtask_done status=error）→ ok=False（先判，防被吞）
    - 有 sql_card：type=table/confirm（dml review → confirm），content=sql
    - report 任务成功：type=report，content=narration 事件文本
    - 其余：type=text，content=最后一段文本
    """
    # 失败优先：error 事件 或 工具失败（chat_stream 实际失败信号）
    for e in events:
        if e.get("type") == "error":
            return TaskResult(type="text", content="", task_id=task.id, skill_id=skill_id,
                              action=task.action, ok=False, error=str(e.get("message", "任务失败")))
        if e.get("type") == "subtask_done" and e.get("status") == "error":
            detail = str(e.get("detail") or "工具执行失败")
            return TaskResult(type="text", content="", task_id=task.id, skill_id=skill_id,
                              action=task.action, ok=False, error=detail)
    cards = [e["card"] for e in events if e.get("type") == "sql_card" and e.get("card")]
    if cards:
        card = cards[-1]
        tier = card.get("tier", "")
        if tier == "dml":
            return TaskResult(type="confirm", content=card.get("sql", ""),
                              task_id=task.id, skill_id=skill_id, action=task.action,
                              ok=True, data={"verdict": card.get("verdict"),
                                             "needs_confirm": bool(card.get("needs_confirm"))})
        return TaskResult(type="table", content=card.get("sql", ""),
                          task_id=task.id, skill_id=skill_id, action=task.action,
                          ok=True, data={"verdict": card.get("verdict"),
                                         "row_count": (card.get("result") or {}).get("row_count")})
    if task.modality == "report":
        # F1/F5：report 成功 → content 取 narration 事件文本（事件契约键 = text，见前端 ai.ts）
        narr = [e.get("text", "") for e in events
                if e.get("type") == "narration" and e.get("text")]
        return TaskResult(type="report", content=narr[-1] if narr else "",
                          task_id=task.id, skill_id=skill_id, action=task.action, ok=True)
    texts = [e["content"] for e in events if e.get("type") == "text" and e.get("content")]
    return TaskResult(type="text", content=texts[-1] if texts else "",
                      task_id=task.id, skill_id=skill_id, action=task.action, ok=True)