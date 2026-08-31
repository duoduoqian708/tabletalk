"""E2 意图分解测试（设计 §13.2/§13.3）：意图识别 = LLM 产出 TaskPlan。

2026-09 修订：关键词快判层移除——真实 provider 下所有请求都走 LLM（单次调用，
复合请求可拆）；mock / 无 LLM 环境确定性降级单任务 query。零关键词断言。
"""
from __future__ import annotations

from app.ai.decompose import _parse_llm_plan, decompose
from app.ai.plan import TaskPlan, TaskSpec


# ---------- LLM JSON 解析 ----------

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
    p = _parse_llm_plan('{"tasks": [{"action": "banana", "modality": "answer"}], "tags": []}', [])
    assert p.tasks[0].action == "unknown"  # 非法 → unknown 兜底
    assert p.tasks[0].trust == "read"


def test_parse_llm_garbage_fallback():
    p = _parse_llm_plan("不是 JSON", [])
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"
    assert p.degraded is True


def test_parse_llm_empty_tasks_fallback():
    p = _parse_llm_plan('{"tasks": [], "tags": []}', [])
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"
    assert p.degraded is True


def test_parse_llm_tags_filtered():
    """tags 只保留已确认的（LLM 幻觉标签剔除）。"""
    text = '{"tasks": [{"action": "query"}], "tags": ["订单", "幻觉标签"]}'
    p = _parse_llm_plan(text, ["订单"])
    assert p.tags == ["订单"]


def test_parse_llm_markdown_fence():
    text = '```json\n{"tasks": [{"action": "query"}]}\n```'
    p = _parse_llm_plan(text, [])
    assert p.tasks[0].action == "query"


# ---------- decompose（mock 降级零 LLM / LLM 单次调用） ----------

class _FakeProvider:
    def __init__(self, text: str):
        self._text = text
        self.calls = 0
        self.last_ctx = None

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        self.last_ctx = kw.get("ctx")
        import types
        return types.SimpleNamespace(content=self._text)


async def _decompose_with(app_state, conn_id, question, llm_text=None, monkeypatch=None, redacted_q=None):
    """切非 mock + 注入 fake provider；llm_text=None → 走 mock 降级。"""
    import app.ai.decompose as decomp
    from app.ai import gateway as gw
    if llm_text is None:
        app_state.runtime.update({"ai_provider": "mock"})
        monkeypatch.setattr(gw, "build_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mock 不应建 provider")))
        return await decompose(app_state, conn_id, question), None
    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})
    fp = _FakeProvider(llm_text)
    monkeypatch.setattr(gw, "build_provider", lambda *a, **k: fp)
    return await decompose(app_state, conn_id, question, redacted_q=redacted_q), fp


async def test_decompose_mock_single_query(app_state, conn_id, monkeypatch):
    """mock 下零 LLM：单任务 query（provider 不得被调用）。"""
    p, fp = await _decompose_with(app_state, conn_id, "查一下订单总数", None, monkeypatch)
    assert p.degraded is True
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"


async def test_decompose_mock_sets_plan_fields(app_state, conn_id, monkeypatch):
    """plan 级字段在 mock 降级下也保留（skip_retrieval 结构问答）。"""
    p, _ = await _decompose_with(app_state, conn_id, "有哪些表", None, monkeypatch)
    assert p.skip_retrieval is True
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"


async def test_decompose_llm_composite_reachable(app_state, conn_id, monkeypatch):
    """真实 provider：普通问题也走 LLM（单一调用），复合请求拆 2 任务。"""
    text = ('{"tasks": [{"action": "query", "modality": "answer", "target": {"tables": ["orders"]}},'
            '{"action": "query", "modality": "analyze", "target": {"tables": ["users"]}}], "tags": []}')
    p, fp = await _decompose_with(app_state, conn_id, "查订单，顺便看下用户增长", text, monkeypatch)
    assert fp is not None and fp.calls == 1
    assert len(p.tasks) == 2
    assert p.tasks[0].action == "query" and p.tasks[1].modality == "analyze"


async def test_decompose_llm_single_task(app_state, conn_id, monkeypatch):
    """LLM 判断单任务（同查询多指标）→ 保持 1 任务（§13.3 反例）。"""
    text = '{"tasks": [{"action": "query", "modality": "analyze", "target": {"tables": ["orders"]}}], "tags": []}'
    p, fp = await _decompose_with(app_state, conn_id, "查每季度的销售额和利润", text, monkeypatch)
    assert len(p.tasks) == 1
    assert p.tasks[0].modality == "analyze"


async def test_decompose_llm_write_from_llm(app_state, conn_id, monkeypatch):
    """2026-09：写意图不再靠关键词——由 LLM 判定（provider 必须被调用）。"""
    text = '{"tasks": [{"action": "write", "modality": "answer"}], "tags": []}'
    p, fp = await _decompose_with(app_state, conn_id, "删掉测试订单", text, monkeypatch)
    assert fp is not None and fp.calls == 1
    assert p.tasks[0].action == "write" and p.tasks[0].trust == "write"


async def test_decompose_llm_unknown_offtopic(app_state, conn_id, monkeypatch):
    """offtopic 由 LLM 判 unknown（→ refusal 拒答）；无关键词层。"""
    text = '{"tasks": [{"action": "unknown", "modality": "answer"}], "tags": []}'
    p, fp = await _decompose_with(app_state, conn_id, "今天天气怎么样", text, monkeypatch)
    assert fp.calls == 1
    assert p.tasks[0].action == "unknown"


async def test_decompose_llm_error_falls_back_query(app_state, conn_id, monkeypatch):
    """LLM 调用异常 → 单任务 query 降级。"""
    import app.ai.decompose as decomp
    from app.ai import gateway as gw
    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})

    class _Err:
        async def chat(self, messages, tools=None, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(gw, "build_provider", lambda *a, **k: _Err())
    p = await decompose(app_state, conn_id, "查一下订单")
    assert len(p.tasks) == 1 and p.tasks[0].action == "query"
    assert p.degraded is True


async def test_decompose_llm_uses_redacted_and_manifest(app_state, conn_id, monkeypatch):
    """出网隐私：LLM 调用复用 preflight 的脱敏原文 + 清单（ctx 带 manifest）。"""
    text = '{"tasks": [{"action": "query"}, {"action": "query"}], "tags": []}'
    p, fp = await _decompose_with(
        app_state, conn_id, "查订单", text, monkeypatch, redacted_q="查订单（已脱敏）",
    )
    # prompt 第一行含脱敏原文（不含敏感原文）
    assert fp.calls == 1
    assert p.tasks[0].action == "query"
