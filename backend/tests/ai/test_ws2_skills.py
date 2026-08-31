"""WS2 内置技能验收测试：refusal 零工具（铁律3）/ strict 离线拒答 /
query 地板只读 / 各技能工具集与设计文档 §4/§6 对齐。
"""
from __future__ import annotations

import pytest

from app.ai.skills.registry import skill_tool_schemas


def _names(skill_id: str) -> set[str]:
    return {t["function"]["name"] for t in skill_tool_schemas(skill_id)}


def test_refusal_skill_has_zero_tools():
    """T2.1/铁律3 · 禁止靠缺席：refusal 是注册技能但工具集为空 → 必须返回零工具，
    绝不能被历史"空→全量"回退吞掉（否则拒答模型还能调 run_dml）。"""
    assert _names("refusal") == set()


def test_query_skill_tools():
    """设计文档 §4/§6 + P2-12：query 地板常开，含知识只读面（kb_read/graph_read）。"""
    assert _names("query") == {"run_query", "get_schema", "query_audit", "ai_review", "kb_read", "graph_read"}


def test_write_skill_tools():
    """设计文档 §4/§6 + F4：write = run_dml, run_query, get_schema, ai_review（ddl 独立技能）。"""
    assert _names("write") == {"run_dml", "run_query", "get_schema", "ai_review"}


def test_ddl_skill_tools():
    """F4/§15.3：ddl = draft_ddl, get_schema, run_query（无 run_dml，墙1 隔离）。"""
    assert _names("ddl") == {"draft_ddl", "get_schema", "run_query"}


def test_knowledge_skill_tools():
    """设计文档 §4/§6：knowledge = kb_read, kb_write, graph_read, graph_write, get_schema, run_query。"""
    assert _names("knowledge") == {"kb_read", "kb_write", "graph_read", "graph_write", "get_schema", "run_query"}


def test_scheduler_skill_tools():
    """设计文档 §4/§6：scheduler = manage_task, run_query, get_schema。"""
    assert _names("scheduler") == {"manage_task", "run_query", "get_schema"}


def test_query_report_present_tools():
    """设计文档 §4/§6：query（地板常开）只读；report 同样只读。"""
    assert "run_dml" not in _names("query") and "draft_ddl" not in _names("query")
    assert _names("report") == {"run_query", "get_schema", "kb_read", "graph_read"}


@pytest.mark.asyncio
async def test_schema_intent_skips_vector_recall(app_state, conn_id, monkeypatch):
    """T2.2：纯结构问题（有哪些表）走 query 技能，skip_retrieval=True 时不触发向量召回。"""
    from app.ai.dto import ChatRequest
    from app.ai.loop import chat_stream
    from app.ai.preflight import PreflightResult

    req = ChatRequest(
        connection_id=conn_id,
        messages=[{"role": "user", "content": "有哪些表"}],
        provider="mock", include_data=False,
    )
    req.skill_id = "query"
    req._preflight = PreflightResult(intent="query", tags=[], degraded=True, is_followup=False, followup_tables=[], skip_retrieval=True)

    called = {"n": 0}

    async def fake_route(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(app_state.knowledge, "vector_route_tables", fake_route)
    events = [ev async for ev in chat_stream(app_state, req)]
    assert called["n"] == 0, "skip_retrieval=True 时不得触发向量召回"


@pytest.mark.asyncio
async def test_strict_offline_refusal(app_state, conn_id, monkeypatch):
    """T2.1：strict 完全离线档下 offtopic 不调任何模型，返回本地固定文案，且不出网清单。"""
    import app.ai.loop as loop
    from app.ai.dto import ChatRequest

    app_state.runtime.update({"privacy_mode": "strict"})

    async def boom(*a, **k):
        raise AssertionError("strict 档下 refusal 不应创建 provider 调用")

    monkeypatch.setattr(loop.gw, "build_provider", boom)
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "今天天气怎么样？"}])
    events = [ev async for ev in loop.stream(app_state, req)]
    texts = [ev.get("content") for ev in events if ev["type"] == "text"]
    assert texts == [loop._STRICT_REFUSAL_ZH]
    # 完全离线：无 manifest / 无 egress 事件
    assert not any(ev["type"] == "manifest" for ev in events)
    # 结尾 done
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_query_skill_refuses_out_of_scope_tool(app_state, conn_id, monkeypatch):
    """铁律3·执行层强制：query（只读）技能下，模型/mock 即使发出 run_dml 也被拒绝执行
    （only-narrow 不是对模型的建议，而是执行层的硬约束——杜绝只读技能误触发写）。"""
    import app.ai.loop as loop
    from app.ai.gateway import StreamChunk, ToolCall
    from app.ai.dto import ChatRequest
    from app.ai.preflight import PreflightResult

    executed = {"n": 0}

    async def fake_execute(state, name, args, conn_id, include_data=False):
        executed["n"] += 1
        raise AssertionError(f"query 只读技能不得执行 {name}")

    monkeypatch.setattr(loop, "execute_tool", fake_execute)

    emitted = {"n": 0}

    class _FakeProv:
        async def chat_stream(self, messages, tools, ctx=None):
            if emitted["n"] < 1:
                emitted["n"] += 1
                yield StreamChunk(tool_calls=[ToolCall(
                    id="tc1", name="run_dml",
                    arguments={"sql": "UPDATE products SET price = price * 1.1;"},
                )])
            else:
                yield StreamChunk(content="完成")

    monkeypatch.setattr(loop.gw, "build_provider", lambda *a, **k: _FakeProv())
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "涨价"}])
    req.skill_id = "query"
    req._preflight = PreflightResult(intent="query", tags=[], degraded=False, is_followup=False, followup_tables=[])

    events = [ev async for ev in loop.chat_stream(app_state, req)]
    assert executed["n"] == 0, "query 只读技能下 run_dml 必须被拒绝执行"
    thinks = [ev.get("text") for ev in events if ev["type"] == "think"]
    assert any("run_dml" in t and "拒绝" in t for t in thinks), f"应下发拒绝提示，got {thinks}"
    assert not any(ev["type"] == "sql_card" for ev in events), "越权 run_dml 不得产生执行卡"