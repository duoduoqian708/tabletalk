"""供应商适配层单测：统一方言归一、各家思考参数映射、降级矩阵、思考链回填。

caps 依据：2026-09 四家官方文档（DeepSeek/智谱/千问/火山方舟）+ 实测校准。
"""
from __future__ import annotations

import pytest

from app.ai.providers import (
    ADAPTERS,
    builtin_providers,
    get_adapter,
    resolve_adapter_name,
)
from app.ai.providers.base import normalize_thinking


# ---------------- 统一方言归一 ----------------

@pytest.mark.parametrize("raw,expect", [
    (True, "high"),
    (False, "auto"),      # 旧布尔 False = 不控档位（模型默认），非显式关闭
    (None, "auto"),
    ("", "auto"),
    ("thinking", "auto"),  # 旧 KB 特判值
    ("high", "high"),
    ("HIGH", "high"),
    ("bogus", "auto"),     # 未知值兜底模型自主
])
def test_normalize_thinking(raw, expect):
    assert normalize_thinking(raw) == expect


# ---------------- 注册表与别名归一 ----------------

def test_resolve_aliases():
    assert resolve_adapter_name("火山方舟") == "volcano"
    assert resolve_adapter_name("volcano") == "volcano"
    assert resolve_adapter_name("GLM") == "zhipu"
    assert resolve_adapter_name("智谱") == "zhipu"
    assert resolve_adapter_name("deepseek") == "deepseek"
    assert resolve_adapter_name("通义千问") == "qwen"
    assert resolve_adapter_name("ollama") == "ollama"
    assert resolve_adapter_name("openai") == "openai"


def test_unknown_provider_falls_back_to_custom():
    assert resolve_adapter_name("随便什么") == "custom"
    assert resolve_adapter_name(None) == "custom"
    assert resolve_adapter_name("") == "custom"


def test_builtin_providers_listing():
    ps = builtin_providers()
    # 7 家适配器 + 火山按量/Agent Plan 双路条目 = 8 项
    assert [p["display"] for p in ps] == [
        "OpenAI", "DeepSeek", "火山（按量）", "火山（Agent Plan）",
        "智谱GLM", "通义千问", "Ollama", "自定义（OpenAI 兼容）",
    ]
    assert set(ADAPTERS) >= {p["adapter"] for p in ps}
    assert {p["name"] for p in ps} == set(ADAPTERS) - {"custom"} | {"custom"}


# ---------------- 火山方舟：effort 全档直传（官方文档背书） ----------------

def test_ark_effort_passthrough():
    a = get_adapter("火山方舟")
    payload: dict = {}
    assert a.apply_thinking(payload, "low", "deepseek-v4-flash", False) == []
    assert payload == {"reasoning_effort": "low"}


def test_ark_off_maps_to_none():
    a = get_adapter("volcano")
    payload: dict = {}
    a.apply_thinking(payload, "off", "x", False)
    assert payload == {"reasoning_effort": "none"}


def test_ark_auto_uses_thinking_type():
    a = get_adapter("volcano")
    payload: dict = {}
    a.apply_thinking(payload, "auto", "x", False)
    assert payload == {"thinking": {"type": "auto"}}


# ---------------- DeepSeek：effort + thinking.enabled 同发；off 显式关 ----------------

def test_deepseek_medium_passthrough_server_maps():
    """官方映射表：medium→high 由服务端处理，客户端直传不降级。"""
    a = get_adapter("deepseek")
    payload: dict = {}
    assert a.apply_thinking(payload, "medium", "deepseek-v4-pro", False) == []
    assert payload == {"reasoning_effort": "medium", "thinking": {"type": "enabled"}}


def test_deepseek_off_disables():
    a = get_adapter("deepseek")
    payload: dict = {}
    a.apply_thinking(payload, "off", "x", False)
    assert payload == {"thinking": {"type": "disabled"}}


# ---------------- GLM：按模型族分派（5.3 强制思考） ----------------

def test_glm_53_off_forced_thinking_degrades():
    a = get_adapter("glm")
    payload: dict = {}
    notes = a.apply_thinking(payload, "off", "glm-5.3-flash", False)
    assert payload == {"thinking": {"type": "enabled"}}
    assert notes and "强制思考" in notes[0].reason


def test_glm_53_medium_downgrades_to_low():
    """5.3 仅 max/high/low：medium 只降不升 → low。"""
    a = get_adapter("zhipu")
    payload: dict = {}
    notes = a.apply_thinking(payload, "medium", "glm-5.3", False)
    assert payload == {"reasoning_effort": "low"}
    assert notes


def test_glm_non_forced_off_disables():
    a = get_adapter("zhipu")
    payload: dict = {}
    assert a.apply_thinking(payload, "off", "glm-4.6", False) == []
    assert payload == {"thinking": {"type": "disabled"}}


def test_glm_non_forced_medium_passthrough():
    """5.2 全档：medium 服务端映射 high，直传。"""
    a = get_adapter("zhipu")
    payload: dict = {}
    assert a.apply_thinking(payload, "medium", "glm-5.2", False) == []
    assert payload == {"reasoning_effort": "medium", "thinking": {"type": "enabled"}}


# ---------------- Qwen：enable_thinking 开关 + effort；永不发 budget ----------------

def test_qwen_effort_with_switch():
    a = get_adapter("qwen")
    payload: dict = {}
    assert a.apply_thinking(payload, "medium", "qwen3.8-max", False) == []
    assert payload == {"enable_thinking": True, "reasoning_effort": "medium"}


def test_qwen_off_disables():
    a = get_adapter("qwen")
    payload: dict = {}
    a.apply_thinking(payload, "off", "x", False)
    assert payload == {"enable_thinking": False}


def test_qwen_never_sends_budget():
    """effort 与 thinking_budget 互斥（同传 400）→ 任何档位都不出现 budget。"""
    a = get_adapter("qwen")
    for t in ("low", "medium", "high", "auto", "off"):
        payload: dict = {}
        a.apply_thinking(payload, t, "x", False)
        assert "thinking_budget" not in payload


# ---------------- Ollama：bool 开关 + 档位不可细分降级 ----------------

def test_ollama_bool_switch():
    a = get_adapter("ollama")
    payload: dict = {}
    notes = a.apply_thinking(payload, "high", "qwen3:32b", False)
    assert payload == {"think": True}
    assert notes  # 档位不可细分 → 降级说明


def test_ollama_off():
    a = get_adapter("ollama")
    payload: dict = {}
    a.apply_thinking(payload, "off", "x", False)
    assert payload == {"think": False}


# ---------------- OpenAI / custom：直传，off/auto 不发 ----------------

def test_openai_passthrough_and_noop():
    a = get_adapter("openai")
    payload: dict = {}
    assert a.apply_thinking(payload, "high", "o3", False) == []
    assert payload == {"reasoning_effort": "high"}
    payload = {}
    a.apply_thinking(payload, "auto", "gpt-4o", False)
    assert payload == {}
    payload = {}
    a.apply_thinking(payload, "off", "gpt-4o", False)
    assert payload == {}


def test_openai_effort_model_family_gate():
    """reasoning_effort 仅推理模型接受：o/gpt-5 系发参数，gpt-4o 系不发（防 400）+ 降级说明。"""
    a = get_adapter("openai")
    payload: dict = {}
    notes = a.apply_thinking(payload, "low", "o4-mini", False)
    assert payload == {"reasoning_effort": "low"} and notes == []
    payload = {}
    notes = a.apply_thinking(payload, "low", "gpt-5", False)
    assert payload == {"reasoning_effort": "low"}
    payload = {}
    notes = a.apply_thinking(payload, "low", "gpt-4o-mini", False)
    assert payload == {} and len(notes) == 1 and "非 OpenAI 推理模型" in notes[0].reason


def test_custom_effort_ungated():
    """custom 不做模型族门控（vllm 私有命名等），effort 直传。"""
    a = get_adapter("custom")
    payload: dict = {}
    assert a.apply_thinking(payload, "low", "my-private-model", False) == []
    assert payload == {"reasoning_effort": "low"}


def test_custom_never_errors_on_any_value():
    a = get_adapter("custom")
    for t in ("off", "low", "medium", "high", "auto"):
        payload: dict = {}
        a.apply_thinking(payload, t, "any-model", False)  # 不抛异常即可


# ---------------- DeepSeek 思考链回填（思考+tools 缺回传 400） ----------------

def test_deepseek_backfills_missing_reasoning():
    a = get_adapter("deepseek")
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
    ]
    notes = a.patch_messages(msgs, "low", True, "上一轮思考链")
    assert msgs[1]["reasoning_content"] == "上一轮思考链"
    assert notes


def test_backfill_skipped_when_off_or_no_tools_or_no_reasoning():
    a = get_adapter("deepseek")
    base = [{"role": "assistant", "tool_calls": [{"id": "1"}]}]
    assert a.patch_messages([dict(m) for m in base], "off", True, "r") == []
    assert a.patch_messages([dict(m) for m in base], "low", False, "r") == []
    assert a.patch_messages([dict(m) for m in base], "low", True, None) == []


def test_backfill_only_other_adapters_noop():
    """非强制回传厂商不做消息修补。"""
    a = get_adapter("openai")
    msgs = [{"role": "assistant", "tool_calls": [{"id": "1"}]}]
    assert a.patch_messages(msgs, "low", True, "r") == []
    assert "reasoning_content" not in msgs[0]


# ---------------- gateway 集成：payload 构造委托 adapter ----------------

def test_gateway_request_uses_adapter(monkeypatch):
    from app.ai.gateway import LLMGateway

    gw = LLMGateway({
        "provider": "火山方舟", "base_url": "https://ark.example.com",
        "api_key": "k", "model": "deepseek-v4-flash", "reasoning": "low",
    })
    url, payload, headers = gw._request([{"role": "user", "content": "hi"}], None, stream=False)
    assert url == "https://ark.example.com/chat/completions"
    assert payload["reasoning_effort"] == "low"
    assert headers["Authorization"] == "Bearer k"
    assert gw._degrade_notes == []


def test_gateway_stream_accumulates_reasoning_for_backfill():
    """流中累积 reasoning → 下一次请求回填 assistant 消息（DeepSeek）。"""
    from app.ai.gateway import LLMGateway

    gw = LLMGateway({
        "provider": "deepseek", "base_url": "https://api.deepseek.com",
        "api_key": "k", "model": "deepseek-v4-pro", "reasoning": "low",
    })
    gw._last_reasoning = "第一轮思考"
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
    ]
    _, payload, _ = gw._request(messages, [{"type": "function", "function": {"name": "x"}}], stream=True)
    assert payload["messages"][1]["reasoning_content"] == "第一轮思考"
