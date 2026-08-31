"""E4 任务循环执行器测试（设计 §12/§14）：顺序 / 失败即停 / 写前置校验 / 结果入 Context。"""
from __future__ import annotations

import pytest

from app.ai.context_object import Context
from app.ai.executor import (
    TaskResult,
    _summarize_task,
    execute_plan,
    should_stop,
    validate_write_precondition,
)
from app.ai.plan import TaskPlan, TaskSpec


def _fake_executor(events: list[dict] | None = None, ok: bool = True):
    """假任务执行器：产固定事件流（或空 + 标记失败）。"""
    evs = events or []

    async def _exec(state, ctx, task):
        if not ok:
            yield {"type": "error", "message": "boom"}
            return
        for e in evs:
            yield e
        if not any(e.get("type") == "sql_card" for e in evs):
            yield {"type": "text", "content": f"{task.id} 回答"}

    return _exec


async def _run_plan(plan, ctx, exec_fn, **kw):
    return [ev async for ev in execute_plan(object(), plan, ctx, exec_fn, **kw)]


# ---------- 顺序执行 + 结果入 Context ----------

async def test_sequential_tasks_results_shared():
    plan = TaskPlan(tasks=[
        TaskSpec(action="query", modality="answer", id="t1"),
        TaskSpec(action="query", modality="analyze", id="t2"),
    ])
    ctx = Context(conn_id="c1", plan=plan)
    events = await _run_plan(plan, ctx, _fake_executor(
        [{"type": "sql_card", "card": {"tier": "read", "verdict": "allow", "sql": "SELECT 1",
                                       "result": {"row_count": 5}}}]))
    # 顺序：task_start t1 → task_start t2
    starts = [e for e in events if e["type"] == "task_start"]
    assert [s["id"] for s in starts] == ["t1", "t2"]
    # 结果入 Context
    r1 = ctx.get_result("t1")
    assert r1 is not None and r1.type == "table" and r1.ok
    assert r1.data["row_count"] == 5
    # 编号结果
    results = [e for e in events if e["type"] == "task_result"]
    assert results[0]["index"] == 1 and results[1]["index"] == 2


# ---------- 失败即停（含写任务） ----------

async def test_failure_stops_before_write():
    """读任务失败 + 剩余含写任务 → 停止（plan_stopped）。"""
    plan = TaskPlan(tasks=[
        TaskSpec(action="query", id="t1"),
        TaskSpec(action="write", id="t2"),
    ])
    ctx = Context(conn_id="c1", plan=plan)
    events = await _run_plan(plan, ctx, _fake_executor(ok=False))
    stopped = [e for e in events if e["type"] == "plan_stopped"]
    assert stopped, events
    # t2 未执行（无 task_start t2）
    assert "t2" not in [e["id"] for e in events if e["type"] == "task_start"]
    # t2 结果未入 Context
    assert ctx.get_result("t2") is None


async def test_failure_continues_with_read_only():
    """读任务失败但剩余全为读 → 不停，继续执行。"""
    plan = TaskPlan(tasks=[
        TaskSpec(action="query", id="t1"),
        TaskSpec(action="query", id="t2"),
    ])
    ctx = Context(conn_id="c1", plan=plan)
    events = await _run_plan(plan, ctx, _fake_executor(ok=False))
    assert not any(e["type"] == "plan_stopped" for e in events)
    assert ctx.get_result("t1") is not None and ctx.get_result("t1").ok is False
    assert ctx.get_result("t2") is not None  # 后续读仍执行
    assert ctx.get_result("t2").ok is False  # F6：失败态验证（fake 恒失败）


# ---------- should_stop 纯函数 ----------

def test_should_stop_rules():
    p = TaskPlan(tasks=[TaskSpec(action="query"), TaskSpec(action="write")])
    assert should_stop(p, 0) is True   # 剩余含写 → 停
    p2 = TaskPlan(tasks=[TaskSpec(action="query"), TaskSpec(action="query")])
    assert should_stop(p2, 0) is False  # 剩余纯读 → 不停
    p3 = TaskPlan(tasks=[TaskSpec(action="query")])
    assert should_stop(p3, 0) is False  # 无剩余 → 不停


def test_should_stop_includes_ddl():
    """F1：剩余任务含 ddl（trust=ddl）→ 停（设计 §14 规则 3 含 write/ddl）。"""
    p = TaskPlan(tasks=[TaskSpec(action="query"), TaskSpec(action="ddl")])
    assert should_stop(p, 0) is True


# ---------- 写前置校验（依赖读成功） ----------

def test_write_precondition_requires_prior_success():
    plan = TaskPlan(tasks=[TaskSpec(action="query", id="t1"), TaskSpec(action="write", id="t2")])
    ctx = Context(conn_id="c1", plan=plan)
    # 前序读成功 → 放行
    ctx.set_result("t1", TaskResult(type="table", ok=True))
    assert validate_write_precondition(plan, plan.tasks[1], ctx) is None
    # 前序读失败 → 拒绝写
    ctx.set_result("t1", TaskResult(type="table", ok=False))
    err = validate_write_precondition(plan, plan.tasks[1], ctx)
    assert err and "未成功" in err
    # 无前序任务（首个即写）→ 放行
    plan2 = TaskPlan(tasks=[TaskSpec(action="write", id="w1")])
    assert validate_write_precondition(plan2, plan2.tasks[0], Context(conn_id="c1")) is None


async def test_write_blocked_by_failed_read():
    """F6：前序读失败 + 剩余写任务 → 计划停止（写不执行）；与纯 should_stop 用例区分：
    本用例断言写任务 t2 的 TaskResult 未被写入 Context（写根本没执行）。"""
    plan = TaskPlan(tasks=[TaskSpec(action="query", id="t1"), TaskSpec(action="write", id="t2")])
    ctx = Context(conn_id="c1", plan=plan)
    events = await _run_plan(plan, ctx, _fake_executor(ok=False))
    stopped = [e for e in events if e["type"] == "plan_stopped"]
    assert stopped, events
    # t2 从未执行（无 task_start）
    assert "t2" not in [e["id"] for e in events if e["type"] == "task_start"]
    assert ctx.get_result("t2") is None  # 写任务无结果（未执行）


# ---------- _summarize_task ----------

def test_summarize_sql_card_table():
    t = TaskSpec(action="query", modality="analyze", id="t1")
    evs = [{"type": "sql_card", "card": {"tier": "read", "verdict": "allow", "sql": "SELECT 1",
                                         "result": {"row_count": 3}}}]
    r = _summarize_task(t, evs, "query")
    assert r.type == "table" and r.ok and r.content == "SELECT 1"
    assert r.data["row_count"] == 3


def test_summarize_dml_confirm():
    t = TaskSpec(action="write", id="w1")
    evs = [{"type": "sql_card", "card": {"tier": "dml", "verdict": "review", "sql": "UPDATE t SET x=1",
                                         "needs_confirm": True}}]
    r = _summarize_task(t, evs, "write")
    assert r.type == "confirm" and r.data["needs_confirm"] is True


def test_summarize_text():
    t = TaskSpec(action="query", id="t1")
    evs = [{"type": "text", "content": "回答内容"}]
    r = _summarize_task(t, evs, "query")
    assert r.type == "text" and r.content == "回答内容"


def test_summarize_report():
    t = TaskSpec(action="query", modality="report", id="r1")
    evs = [{"type": "narration", "text": "报告正文"}]  # P1-2：事件契约 text（前端 ai.ts:129）
    r = _summarize_task(t, evs, "report")
    assert r.type == "report" and r.content == "报告正文"


def test_summarize_error():
    t = TaskSpec(action="query", id="t1")
    evs = [{"type": "error", "message": "boom"}]
    r = _summarize_task(t, evs, "query")
    assert r.ok is False and r.error == "boom"


def test_summarize_subtask_error_detected():
    """F1：工具失败事件（subtask_done status=error，chat_stream 实际失败信号）→ ok=False。"""
    t = TaskSpec(action="query", id="t1")
    evs = [
        {"type": "subtask_start", "id": "st_1", "tool": "run_query"},
        {"type": "subtask_done", "id": "st_1", "tool": "run_query", "status": "error",
         "detail": "安全闸门拦截"},
        {"type": "text", "content": "查询被拦截"},
    ]
    r = _summarize_task(t, evs, "query")
    assert r.ok is False
    assert "拦截" in (r.error or "")


def test_summarize_report_failure_not_swallowed():
    """F1：report 任务失败（error 事件）→ ok=False（不被 modality=report 分支吞掉）。"""
    t = TaskSpec(action="query", modality="report", id="r1")
    evs = [{"type": "error", "message": "章节规划失败"}]
    r = _summarize_task(t, evs, "report")
    assert r.ok is False and r.error == "章节规划失败"


def test_task_result_to_dict():
    r = TaskResult(type="table", content="SELECT 1", task_id="t1", skill_id="query",
                   action="query", data={"row_count": 2})
    d = r.to_dict()
    assert d["type"] == "table" and d["task_id"] == "t1" and d["data"]["row_count"] == 2