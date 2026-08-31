"""E3 Context 层间契约测试（设计 §18）。"""
from __future__ import annotations

from app.ai.context_object import Context
from app.ai.plan import TaskPlan, TaskSpec


def test_context_basic_fields():
    c = Context(conn_id="c1", session_id="s1", include_data=True,
                session_vars={"current_tenant": "7"})
    assert c.conn_id == "c1" and c.session_id == "s1"
    assert c.include_data is True
    assert c.session_vars == {"current_tenant": "7"}


def test_context_task_results_share_across_tasks():
    """前序任务结果对后续任务可见（§14 共享 Context）。"""
    c = Context(conn_id="c1")
    c.set_result("t1", {"type": "table", "content": "orders 数据", "rows": 10})
    assert c.get_result("t1")["rows"] == 10
    assert c.get_result("t2") is None


def test_context_current_trust_derived():
    c = Context(conn_id="c1")
    assert c.current_trust == "read"  # 无任务默认 read
    c.current_task = TaskSpec(action="write")
    assert c.current_trust == "write"


def test_context_carries_plan():
    c = Context(conn_id="c1", plan=TaskPlan(
        tasks=[TaskSpec(action="query", id="t1"), TaskSpec(action="write", id="t2")]))
    assert len(c.plan.tasks) == 2
    assert c.plan.has_write is True


def test_context_selected_tables_shared():
    c = Context(conn_id="c1")
    c.selected_tables = ["orders", "users"]
    assert c.selected_tables == ["orders", "users"]


def test_context_prior_result_injected_to_later_task():
    """F6：前序任务结果对后续任务可见（§18 层间共享核心语义）。"""
    from app.ai.executor import TaskResult
    c = Context(conn_id="c1")
    c.set_result("t1", TaskResult(type="table", content="SELECT 1", task_id="t1",
                                  skill_id="query", action="query", ok=True,
                                  data={"row_count": 5}))
    r = c.get_result("t1")
    assert r is not None and r.ok and r.data["row_count"] == 5
    # current_skill/current_task 随任务循环推进
    c.current_task = TaskSpec(action="write", id="t2")
    c.current_skill = "write"
    assert c.current_trust == "write"
    assert c.current_skill == "write"

async def test_context_shared_fields_populated_by_runner(app_state, conn_id):
    """P2-13/§18：task_runner 填充 selected_tables（target+追问表）与 session_vars（连接配置）。"""
    from app.ai.loop import task_runner
    from app.ai.context_object import Context
    from app.ai.dto import ChatRequest
    from app.ai.plan import TaskPlan, TaskSpec

    try:
        app_state.connections.get(conn_id).session_vars = {"current_tenant": 7}
    except Exception:
        pass
    assert app_state.connections.get(conn_id).session_vars == {"current_tenant": 7}
    t = TaskSpec(action="query", modality="answer", id="t1",
                 target={"tables": ["orders", "customers"]})
    ctx = Context(conn_id=conn_id, plan=TaskPlan(tasks=[t]))
    req = ChatRequest(connection_id=conn_id,
                      messages=[{"role": "user", "content": "退货率"}], provider="mock")
    events = [ev async for ev in task_runner(app_state, req, ctx, t)]
    assert "orders" in ctx.selected_tables and "customers" in ctx.selected_tables
    assert ctx.session_vars.get("current_tenant") == 7
    assert any(e["type"] in ("turn_start", "scene_start") for e in events)
