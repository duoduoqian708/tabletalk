"""E7 引擎集成测试：loop.stream 任务循环冒烟（设计 §12）。

- 单任务：事件流含 task_start/task_result/task_done（增量），原有事件保留
- report 任务：modality=report → report 技能执行器
- 复合任务（显式 plan）：顺序执行 + 编号结果
"""
from __future__ import annotations

import pytest

from app.ai.dto import ChatRequest
from app.ai.plan import TaskPlan, TaskSpec


def _req(conn_id, q: str, **kw) -> ChatRequest:
    return ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": q}], **kw)


async def _collect(app_state, conn_id, req):
    from app.ai.loop import stream
    return [ev async for ev in stream(app_state, req)]


async def _ensure_built(app_state, conn_id):
    from app.core.schema import get_schema
    if conn_id not in app_state.knowledge._auto:
        await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                        enable_ai_annotation=False)


async def test_single_query_task_flow(app_state, conn_id):
    """单任务 query：task_start → 原事件 → task_result → task_done → done（顺序断言）。"""
    await _ensure_built(app_state, conn_id)
    events = await _collect(app_state, conn_id, _req(conn_id, "查上个月退货率最高的 10 个商品"))
    types = [e["type"] for e in events]
    # 顺序：task_start 在前、task_result 在 task_done 前、done 最后
    i_start = types.index("task_start")
    assert "task_result" in types and "task_done" in types and types[-1] == "done"
    i_result = types.index("task_result")
    i_done = types.index("task_done")
    assert i_start < i_result < i_done, types
    # 原有事件保留（前端协议兼容）
    assert "sql_card" in types or "text" in types
    # task_result 形状：index=1 + result_type
    tr = [e for e in events if e["type"] == "task_result"][0]
    assert tr["index"] == 1 and tr["result_type"] in ("table", "text", "confirm", "report")
    done = [e for e in events if e["type"] == "task_done"][-1]
    assert done["ok"] is True


async def test_plan_flow_sequential_tasks(app_state, conn_id):
    """显式复合计划：2 任务顺序执行，编号 task_result。"""
    from app.core.schema import get_schema
    from app.ai.context_object import Context
    from app.ai.executor import execute_plan
    from app.ai.loop import task_runner

    await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                    enable_ai_annotation=False)
    plan = TaskPlan(tasks=[
        TaskSpec(action="query", modality="answer", id="t1"),
        TaskSpec(action="query", modality="answer", id="t2"),
    ])
    ctx = Context(conn_id=conn_id, plan=plan)
    req = _req(conn_id, "查一下订单")
    events = [ev async for ev in execute_plan(app_state, plan, ctx,
                                              lambda s, c, t: task_runner(s, req, c, t))]
    starts = [e["id"] for e in events if e["type"] == "task_start"]
    assert starts == ["t1", "t2"]
    results = [e for e in events if e["type"] == "task_result"]
    assert [r["index"] for r in results] == [1, 2]
    # 结果共享 Context
    assert ctx.get_result("t1") is not None and ctx.get_result("t2") is not None


async def test_report_task_uses_report_skill(app_state, conn_id):
    """F6：modality=report → task_runner 设 skill_id=report + mode=report + report 专属事件。"""
    from app.ai.loop import task_runner
    from app.ai.skills.route import route
    from app.ai.context_object import Context
    from app.ai.dto import ChatRequest

    assert route("query", "report") == "report"
    t = TaskSpec(action="query", modality="report", id="r1")
    plan = TaskPlan(tasks=[t])
    ctx = Context(conn_id=conn_id, plan=plan)
    req = ChatRequest(connection_id=conn_id,
                      messages=[{"role": "user", "content": "出一份报告"}], provider="mock")
    events = [ev async for ev in task_runner(app_state, req, ctx, t)]
    # 任务执行后 req 被 runner 改写：skill_id=report + mode=report
    assert req.skill_id == "report"
    from app.ai.intent import MODE_REPORT
    assert req.mode == MODE_REPORT
    types = [e["type"] for e in events]
    # report 专属事件（report_start 或 narration/plan）
    assert any(t in ("report_start", "narration", "plan", "clarify") for t in types), types


async def test_block_read_stops_followup_write(app_state, conn_id, monkeypatch):
    """P1-5：读任务图校验打回（verdict=block）→ 失败信号 → 后续写任务停止执行。"""
    from app.ai.tools.registry import ToolOutcome
    from app.ai import loop as loop_mod
    from app.ai.loop import task_runner
    from app.ai.context_object import Context
    from app.ai.executor import execute_plan

    async def _block_tool(state, name, args, conn_id, include_data=False):
        return ToolOutcome(
            result={"ok": False, "verdict": "block", "reason": "JOIN 不在知识库图内", "error": "x"},
            card={"tier": "read", "verdict": "block", "sql": (args or {}).get("sql", "")},
            think="图校验未通过：该 JOIN 不在知识库图内，提示模型重写。",
        )

    monkeypatch.setattr(loop_mod, "execute_tool", _block_tool)
    plan = TaskPlan(tasks=[
        TaskSpec(action="query", modality="answer", id="t1"),
        TaskSpec(action="write", modality="answer", id="t2"),
    ])
    ctx = Context(conn_id=conn_id, plan=plan)
    req = _req(conn_id, "退货率", provider="mock")  # mock 关键词 → 产 run_query 工具调用
    events = [ev async for ev in execute_plan(app_state, plan, ctx,
                                              lambda s, c, t: task_runner(s, req, c, t))]
    types = [e["type"] for e in events]
    assert "plan_stopped" in types, types
    # t1 失败（task_result ok=False），t2 从未启动
    tr = [e for e in events if e["type"] == "task_result"][0]
    assert tr["ok"] is False, tr
    assert "t2" not in [e.get("id") for e in events if e.get("type") == "task_start"]


async def test_plan_with_unknown_task_uses_general(app_state, conn_id):
    """F5：复合计划含 unknown 任务 → route 到 general 技能执行（只读兜底可达）。"""
    from app.ai.context_object import Context
    from app.ai.executor import execute_plan
    from app.ai.loop import task_runner
    from app.ai.dto import ChatRequest

    await _ensure_built(app_state, conn_id)
    plan = TaskPlan(tasks=[
        TaskSpec(action="query", modality="answer", id="t1"),
        TaskSpec(action="unknown", modality="answer", id="t2"),
    ])
    ctx = Context(conn_id=conn_id, plan=plan)
    req = ChatRequest(connection_id=conn_id,
                      messages=[{"role": "user", "content": "查一下订单"}])
    events = [ev async for ev in execute_plan(app_state, plan, ctx,
                                              lambda s, c, t: task_runner(s, req, c, t))]
    t2_start = [e for e in events if e["type"] == "task_start" and e["id"] == "t2"]
    assert t2_start and t2_start[0]["skill"] == "general", events