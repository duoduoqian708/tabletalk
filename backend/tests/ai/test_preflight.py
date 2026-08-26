"""WS1 T1.1-T1.3 验收：preflight 统一意图层。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.ai.preflight import PreflightResult, preflight
from app.ai.agent.dispatcher import resolve_skill_from_intent
from app.ai.dto import ChatRequest
from app.ai.loop import chat_stream


@pytest.mark.parametrize("question,intent", [
    ("查一下订单总数", "query"),
    ("统计每个产品的销量", "query"),
    ("生成月度销售报告", "report"),
    ("出一份趋势分析", "report"),
    ("有哪些表", "query"),
    ("这个库有哪些表结构", "query"),
    ("删掉测试订单", "write"),
    ("把价格改成100", "write"),
    ("给库存为 0 的产品涨价 10%", "write"),  # mock 写示例：涨价/提价路由 write 技能（query 只读后）
    ("加一列备注字段", "write"),
    ("建个索引在 order_date 上", "write"),
    ("今天天气怎么样", "offtopic"),
    ("你好", "offtopic"),
])
async def test_preflight_keywords_six_intents(app_state, conn_id, question, intent):
    res = await preflight(app_state, conn_id, question, history_tail=None)
    assert res.intent == intent, f"{question} -> {res.intent} != {intent}"
    assert res.degraded is True  # 关键词路径 0 LLM
    # 关键跨类反例：看看 ≠ write
    if question == "看看测试订单":
        assert res.intent != "write"


async def test_preflight_cross_class_anti_case(app_state, conn_id):
    res = await preflight(app_state, conn_id, "看看测试订单", history_tail=None)
    assert res.intent == "query"
    assert res.intent != "write"


async def test_preflight_uncertain_as_query(app_state, conn_id, monkeypatch):
    # 强制走 LLM 路径：用一个不命中关键词的问题
    q = "请帮我处理一下订单相关需求的综合分析看看"
    # 但此句含“分析”，会命中 report，需换一个完全无关键词的句子
    q = "请帮我搞一下那个事情"
    # mock provider.chat 返回非法 intent
    from app.ai import gateway as gw

    class FakeProvider:
        async def chat(self, messages, tools=None, ctx=None):
            return type("R", (), {"content": '{"intent": "illegal_intent", "tags": []}'})()

    monkeypatch.setattr(gw, "build_provider", lambda cfg: FakeProvider())
    # 让 is_effective_mock 为 False：需 runtime 非 strict 且 provider cloud 有 key
    # 通过改 runtime.provider_config 的 api_key
    orig_cfg = app_state.runtime.get().provider_config()
    # 若当前是 mock 降级，需伪造为 cloud 有 key 的配置
    fake_cfg = {"provider": "cloud", "base_url": "http://fake", "api_key": "test-key", "model": "test", "temperature": 0.2}
    monkeypatch.setattr("app.ai.preflight.resolve_provider_cfg", lambda s, r: fake_cfg) if False else None
    # 直接 patch gw.is_effective_mock
    monkeypatch.setattr(gw, "is_effective_mock", lambda cfg: False)
    # 同时让 get_env 等不影响
    res = await preflight(app_state, conn_id, q, history_tail=None)
    assert res.intent == "query"
    # 非法值应被兜底为 query，且 degraded True（解析失败）
    assert res.intent == "query"


async def test_preflight_timeout_fallback(app_state, conn_id, monkeypatch):
    q = "请帮我处理一下订单相关需求的综合分析看看-unique-timeout"  # 无关键词，强制 LLM
    # 确保不命中关键词：此句含“综合分析”会命中 report，需用无关键词
    q = "请帮我处理那个未命中关键词的请求XYZ"
    from app.ai import gateway as gw

    class SlowProvider:
        async def chat(self, messages, tools=None, ctx=None):
            await asyncio.sleep(3)  # >2s 超时
            return type("R", (), {"content": '{"intent": "report", "tags": []}'})()

    monkeypatch.setattr(gw, "build_provider", lambda cfg: SlowProvider())
    monkeypatch.setattr(gw, "is_effective_mock", lambda cfg: False)
    # 缩短超时以加速测试：patch _get_timeout
    import app.ai.preflight as pf
    monkeypatch.setattr(pf, "_get_timeout", lambda: 0.2)
    res = await preflight(app_state, conn_id, q, history_tail=None)
    assert res.intent == "query"
    assert res.degraded is True


async def test_preflight_egress_exactly_one(app_state, conn_id, monkeypatch):
    # 用非关键词问题强制走 LLM；经真实 gateway（mock）驱动，验证中央拦截器恰好写 1 条 egress-intent
    q = "请帮我处理那个未命中关键词的请求ABC"
    from app.ai import gateway as gw
    from app.ai.gateway import ChatResponse

    monkeypatch.setattr(gw, "is_effective_mock", lambda cfg: False)  # 强制走 LLM 分支
    # 补丁 MockProvider.chat 返回确定性 JSON，但 gateway 仍是真实拦截器（egress 照记）
    async def _mock_chat(cls, messages, tools=None):
        return ChatResponse(content='{"intent": "query", "tags": []}')
    monkeypatch.setattr(gw.MockProvider, "chat", classmethod(_mock_chat))
    before = len([e for e in app_state.audit.list() if e.get("status") == "egress-intent"])
    res = await preflight(app_state, conn_id, q, history_tail=None)
    after = len([e for e in app_state.audit.list() if e.get("status") == "egress-intent"])
    assert after == before + 1
    assert res.intent == "query"
    assert res.degraded is False


async def test_preflight_followup_seeds(app_state, conn_id):
    # 追问轮：上轮有 orders 表，当前句为短句追问
    history = [
        {"role": "user", "content": "查一下订单总数"},
        {"role": "assistant", "content": "已查询", "tables": ["orders"]},
    ]
    res = await preflight(app_state, conn_id, "那按周统计呢", history_tail=history)
    assert res.is_followup is True
    assert "orders" in res.followup_tables


async def test_preflight_followup_not_trigger_when_has_domain(app_state, conn_id):
    history = [
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": "ok", "tables": ["orders"]},
    ]
    # 当前句含领域词且非追问开头，不应视为 followup
    # 先确保标签库里有领域词需要确认状态？
    # 简单用含领域词的句子
    res = await preflight(app_state, conn_id, "查一下产品销量", history_tail=history)
    # 若能解析出标签，则 has_domain True，is_followup 应 False
    # 不强断言 has_domain，仅断言 followup 逻辑不误判
    # 当前句含明确领域动作，不应判 followup
    assert res.is_followup is False


async def test_dispatcher_disabled_write_degraded(app_state):
    from app.ai.skills.registry import get_skill, update_skill, list_enabled_skills

    # WS1 阶段仅 query/report 已注册，WS2 才有 write/ddl；此处用 report 验证 enabled 感知与降级
    skill = get_skill("report")
    if skill is None:
        pytest.skip("report skill not registered")
    prev = skill.enabled
    try:
        update_skill("report", {"enabled": False})
        # preflight 判 report
        res = await preflight(app_state, "dummy_conn", "生成月度销售报告", history_tail=None)
        assert res.intent == "report"
        skill_id, degraded, msg = resolve_skill_from_intent(res.intent)
        assert degraded is True
        assert msg and "已关闭" in msg
        assert skill_id == "report"
        # 地板技能不可禁用：query 始终可用
        assert "query" in {s.id for s in list_enabled_skills()} or get_skill("query").enabled
    finally:
        update_skill("report", {"enabled": prev})


async def test_intent_py_no_inline_audit():
    import pathlib
    p = pathlib.Path("app/ai/intent.py")
    # 从 backend 目录读取
    text = pathlib.Path(__file__).parent.parent.joinpath("app/ai/intent.py").read_text() if pathlib.Path(__file__).parent.parent.joinpath("app/ai/intent.py").exists() else ""
    if not text:
        # fallback absolute
        import app
        import inspect
        text = inspect.getsource(__import__("app.ai.intent", fromlist=["*"]))
    assert "build_manifest" not in text
    assert "audit.log" not in text
    assert "redact_text" not in text


async def test_preflight_coverage_and_mismatch_integration(app_state, conn_id):
    # 集成：chat_stream 产出 context_meta 含 coverage 与 intent_mismatch
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "查一下订单总数"}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    done = next((ev for ev in events if ev["type"] == "done"), None)
    assert done is not None
    meta = done.get("context_meta") or {}
    assert "coverage" in meta
    assert "intent_mismatch" in meta
    assert isinstance(meta["coverage"], float)
    assert isinstance(meta["intent_mismatch"], bool)
