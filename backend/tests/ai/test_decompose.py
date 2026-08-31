"""E2 意图分解测试（设计 §13.2/§13.3）：LLM TaskPlan / 关键词快判 / mock 降级。"""
from __future__ import annotations

import pytest

from app.ai.decompose import (
    KEYWORD_PLAN_SPECS,
    _keyword_plan,
    _parse_llm_plan,
    decompose,
)
from app.ai.plan import TaskPlan, TaskSpec


# ---------- 关键词快判 ----------

def test_keyword_single_query():
    p = _keyword_plan("查一下订单总数")
    assert len(p.tasks) == 1
    assert p.tasks[0].action == "query"
    assert p.tasks[0].modality == "answer"


def test_keyword_report():
    p = _keyword_plan("生成销售趋势报告")
    assert p.tasks[0].action == "query"
    assert p.tasks[0].modality == "report"


def test_keyword_write():
    p = _keyword_plan("删掉测试订单")
    assert p.tasks[0].action == "write"


def test_keyword_ddl_forms():
    """P2-9：DDL 类关键词（建表/加列/索引）→ ddl action（对齐 LLM 路径 → 独立 ddl 技能）。"""
    for q in ("帮我建表", "加一列备注", "创建索引", "alter table 加字段", "建一个索引", "删除订单表"):
        p = _keyword_plan(q)
        assert p.tasks[0].action == "ddl", q


def test_keyword_kb():
    p = _keyword_plan("帮我加个注释")
    assert p.tasks[0].action == "kb"


def test_keyword_scheduler():
    p = _keyword_plan("每天9点跑一次统计")
    assert p.tasks[0].action == "schedule"


def test_keyword_offtopic():
    p = _keyword_plan("今天天气怎么样")
    assert p.tasks[0].action == "unknown"  # offtopic → unknown（general 承接拒答）


def test_keyword_hello_with_query_is_query():
    """边界反例：'你好，查一下订单' 是 query 不是 offtopic（高特异性优先）。"""
    p = _keyword_plan("你好，查一下订单")
    assert p.tasks[0].action == "query"


def test_keyword_look_is_not_write():
    """边界反例：'看看测试订单' 不算 write（无强写词）。"""
    p = _keyword_plan("看看测试订单")
    assert p.tasks[0].action == "query"


def test_keyword_write_boundary_degrade():
    """F6：write 规则命中但含'看看'且无强写词 → 降 query（真实触达降级分支）。"""
    p = _keyword_plan("看看加个索引")
    assert p.tasks[0].action == "query"  # "看看"+"索引"（弱写词）→ 降 query
    p2 = _keyword_plan("看看建表语句")
    assert p2.tasks[0].action == "query"
    # 强写词命中 → 保持 write
    p3 = _keyword_plan("看看删掉这张表")
    assert p3.tasks[0].action == "write"


def test_empty_question_default():
    p = _keyword_plan("")
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"


# ---------- LLM 解析 ----------

def test_parse_llm_single_task():
    text = '{"tasks": [{"action": "query", "modality": "analyze", "target": {"tables": ["orders"]}}], "tags": ["订单"]}'
    p = _parse_llm_plan(text, ["订单"])
    assert len(p.tasks) == 1
    assert p.tasks[0].action == "query" and p.tasks[0].modality == "analyze"
    assert p.tasks[0].target.get("tables") == ["orders"]
    assert p.tags == ["订单"]


def test_parse_llm_composite():
    """复合请求：2 个独立任务（独立操作才拆）。"""
    text = ('{"tasks": [{"action": "query", "modality": "answer", "target": {"tables": ["orders"]}},'
            '{"action": "query", "modality": "report", "target": {"tables": ["users"]}}], "tags": []}')
    p = _parse_llm_plan(text, [])
    assert len(p.tasks) == 2
    assert p.tasks[1].modality == "report"


def test_parse_llm_invalid_action_fallback():
    text = '{"tasks": [{"action": "banana", "modality": "answer"}], "tags": []}'
    p = _parse_llm_plan(text, [])
    assert p.tasks[0].action == "unknown"  # 非法 → unknown 兜底
    assert p.tasks[0].trust == "read"


def test_parse_llm_garbage_fallback():
    p = _parse_llm_plan("不是 JSON", [])
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"
    assert p.degraded is True


def test_parse_llm_empty_tasks_fallback():
    p = _parse_llm_plan('{"tasks": [], "tags": []}', [])
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"
    assert p.degraded is True  # F6：空任务回退标记降级（与 garbage 回退一致）


def test_parse_llm_tags_filtered():
    """tags 只保留已确认的（LLM 幻觉标签剔除）。"""
    text = '{"tasks": [{"action": "query"}], "tags": ["订单", "幻觉标签"]}'
    p = _parse_llm_plan(text, ["订单"])
    assert p.tags == ["订单"]


def test_parse_llm_markdown_fence():
    text = '```json\n{"tasks": [{"action": "query"}]}\n```'
    p = _parse_llm_plan(text, [])
    assert p.tasks[0].action == "query"


# ---------- 完整 decompose（mock 降级零 LLM） ----------

async def test_decompose_mock_single_query(app_state, conn_id, monkeypatch):
    """mock 下 decompose 零 LLM：单任务 query（spy 证明 provider 未被调用）。"""
    import app.ai.decompose as decomp
    from app.ai import gateway as gw

    async def boom(*a, **k):
        raise AssertionError("mock 下不应创建 provider 调用")

    monkeypatch.setattr(gw, "build_provider", boom)
    p = await decompose(app_state, conn_id, "查一下订单总数")
    assert p.degraded is True
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"


async def test_decompose_mock_report_keyword(app_state, conn_id):
    p = await decompose(app_state, conn_id, "生成月度分析报告")
    assert p.tasks[0].modality == "report"


async def test_decompose_sets_plan_fields(app_state, conn_id):
    """plan 级辅助字段：followup/skip_retrieval 保留（结构问答跳过检索）。"""
    p = await decompose(app_state, conn_id, "有哪些表")
    assert p.skip_retrieval is True
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"


# ---------- F3：语义分解可达（LLM 复合拆解） ----------

class _FakeDecompProvider:
    """返回复合 TaskPlan 的 fake provider（真实 LLM 路径代理）。"""

    def __init__(self, text: str):
        self._text = text
        self.calls = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        import types
        return types.SimpleNamespace(content=self._text)


async def _decompose_with_provider(app_state, conn_id, question, llm_text, monkeypatch):
    """切到非 mock + 注入 fake provider → 走真实 LLM 分解路径。"""
    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})
    import app.ai.decompose as decomp
    from app.ai import gateway as gw
    fp = _FakeDecompProvider(llm_text)
    monkeypatch.setattr(gw, "build_provider", lambda *a, **k: fp)
    return await decompose(app_state, conn_id, question), fp


async def test_decompose_llm_composite_reachable(app_state, conn_id, monkeypatch):
    """F3：query 类关键词（查/统计）不短路 LLM——复合请求可拆 2 任务（§13.3）。"""
    text = ('{"tasks": [{"action": "query", "modality": "answer", "target": {"tables": ["orders"]}},'
            '{"action": "query", "modality": "analyze", "target": {"tables": ["users"]}}], "tags": []}')
    p, fp = await _decompose_with_provider(app_state, conn_id, "查订单，顺便看下用户增长", text, monkeypatch)
    assert fp.calls == 1  # 确实调了 LLM
    assert len(p.tasks) == 2
    assert p.tasks[0].action == "query" and p.tasks[1].modality == "analyze"


async def test_decompose_llm_single_task_keeps_answer(app_state, conn_id, monkeypatch):
    """LLM 判断单任务（同查询多指标）→ 保持 1 任务（不拆，§13.3 反例）。"""
    text = '{"tasks": [{"action": "query", "modality": "analyze", "target": {"tables": ["orders"]}}], "tags": []}'
    p, fp = await _decompose_with_provider(app_state, conn_id, "查每季度的销售额和利润", text, monkeypatch)
    assert len(p.tasks) == 1
    assert p.tasks[0].modality == "analyze"


async def test_decompose_high_specificity_still_shortcuts(app_state, conn_id, monkeypatch):
    """F3：高特异度关键词（write/kb/schedule/report）仍短路零 LLM。"""
    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})
    import app.ai.decompose as decomp
    from app.ai import gateway as gw

    async def boom(*a, **k):
        raise AssertionError("高特异度不应调 LLM")

    monkeypatch.setattr(gw, "build_provider", boom)
    p = await decompose(app_state, conn_id, "删掉测试订单")
    assert p.tasks[0].action == "write" and p.degraded is True
    p2 = await decompose(app_state, conn_id, "帮我加个注释")
    assert p2.tasks[0].action == "kb"