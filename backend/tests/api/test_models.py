"""多模型管理 + 能力探测 API 测试。"""
from __future__ import annotations

import pytest


async def test_settings_has_ai_models_list(client):
    """GET /settings 返回 ai_models 列表 + 默认模型。"""
    r = await client.get("/api/v1/settings")
    body = r.json()
    assert "ai_models" in body
    assert isinstance(body["ai_models"], list)
    assert "default_ai_model" in body
    # 不内置任何模型：默认无内置 mock、无内置 deepseek（列表可能为空，等用户自己配）
    ids = {m["id"] for m in body["ai_models"]}
    assert "llm_mock" not in ids
    assert "llm_deepseek" not in ids
    # api_key 脱敏：空或含 •••
    for m in body["ai_models"]:
        assert m["api_key"] == "" or "•••" in m["api_key"]


async def test_settings_has_embedding_models_list(client):
    """GET /settings 返回 embedding_models 列表（离线 Hash 为内部兜底，不再作为 UI 模型暴露）。"""
    r = await client.get("/api/v1/settings")
    body = r.json()
    assert "embedding_models" in body
    assert isinstance(body["embedding_models"], list)
    # 离线 Hash 向量模型不再出现在 UI 列表（知识库内部 HashingEmbedder 兜底）
    providers = {m["provider"] for m in body["embedding_models"]}
    assert "hash" not in providers


async def test_settings_legacy_fields_still_work(client):
    """向后兼容：旧单组字段 ai_provider/ai_model 等仍可读。"""
    r = await client.get("/api/v1/settings")
    body = r.json()
    assert "ai_provider" in body
    assert "ai_model" in body
    assert "ai_base_url" in body
    assert "embedding_provider" in body
    assert "embedding_model" in body


async def test_ai_test_mock_returns_capabilities(client):
    """/ai/test mock 模式返回能力探测结果。"""
    r = await client.post("/api/v1/ai/test", params={"provider": "mock"})
    body = r.json()
    assert body["ok"] is True
    assert "capabilities" in body
    caps = body["capabilities"]
    assert caps["connectivity"] is True
    assert caps["function_calling"] is True
    assert caps["streaming"] is True
    assert "reasoning" in caps


async def test_ai_test_bad_url_returns_not_ok(client):
    """/ai/test 无效 URL 返回 ok=False。"""
    r = await client.post("/api/v1/ai/test", params={
        "provider": "cloud",
        "base_url": "http://127.0.0.1:19999/v1",
        "model": "test",
    })
    body = r.json()
    assert body["ok"] is False
    assert "error" in body


def _fake_stream(monkeypatch, sse_lines: list[str]):
    """把 httpx.AsyncClient 替换成返回给定 SSE 内容的桩。"""
    import httpx

    payload = "".join(sse_lines).encode()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def fake(*a, **k):
        k["transport"] = transport
        return orig(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", fake)


async def test_detect_reasoning_streaming_finds_reasoning(monkeypatch):
    """流式响应含 reasoning_content → 判定支持推理。"""
    import app.api.ai as ai

    _fake_stream(monkeypatch, [
        'data: {"choices":[{"delta":{"reasoning_content":"let me think"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"2"}}]}\n\n',
        "data: [DONE]\n\n",
    ])
    cfg = {"provider": "cloud", "base_url": "http://x/v1", "api_key": "", "model": "some-model"}
    assert await ai._detect_reasoning_streaming(cfg) is True


async def test_detect_reasoning_streaming_no_reasoning(monkeypatch):
    """无 reasoning 字段 → 判定不支持。"""
    import app.api.ai as ai

    _fake_stream(monkeypatch, [
        'data: {"choices":[{"delta":{"content":"2"}}]}\n\n',
        "data: [DONE]\n\n",
    ])
    cfg = {"provider": "cloud", "base_url": "http://x/v1", "api_key": "", "model": "plain"}
    assert await ai._detect_reasoning_streaming(cfg) is False


async def test_detect_reasoning_keyword_override():
    """o 系列等关键词硬覆盖为 True（API 不外露推理内容）。"""
    import app.api.ai as ai

    assert await ai._detect_reasoning({"provider": "cloud", "base_url": "", "api_key": "", "model": "gpt-5"}) is True
    assert await ai._detect_reasoning({"provider": "cloud", "base_url": "", "api_key": "", "model": "plain-llm"}) is False


async def test_fetch_context_window_from_models(monkeypatch):
    """未知模型名但 /models 端点含 context_length → 兜底取回。"""
    import httpx
    import app.api.ai as ai

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "my-model", "context_length": 99999}]})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def fake(*a, **k):
        k["transport"] = transport
        return orig(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    cfg = {"provider": "cloud", "base_url": "http://x/v1", "api_key": "", "model": "my-model"}
    assert await ai._fetch_context_window(cfg) == 99999
    assert await ai._resolve_context_window(cfg) == 99999


async def test_resolve_context_window_falls_through_to_none(monkeypatch):
    """名称与 /models 都查不到 → None（不误标）。"""
    import httpx
    import app.api.ai as ai

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "other", "context_length": 123}]})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def fake(*a, **k):
        k["transport"] = transport
        return orig(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    cfg = {"provider": "cloud", "base_url": "http://x/v1", "api_key": "", "model": "mystery"}
    assert await ai._resolve_context_window(cfg) is None


async def test_embedding_test_hash(client):
    """/ai/embedding/test hash 模式正常。"""
    r = await client.post("/api/v1/ai/embedding/test", params={"provider": "hash"})
    body = r.json()
    assert body["ok"] is True
    assert body["dimensions"] == 64


async def test_settings_put_ai_models_full_replace(client):
    """PUT /settings 传 ai_models 全量替换列表（不再注入内置模型）。"""
    r = await client.put("/api/v1/settings", json={
        "ai_models": [
            {"id": "llm_custom1", "name": "我的模型", "provider": "cloud",
             "base_url": "https://api.example.com/v1", "model": "gpt-test", "api_key": "sk-xxx"},
            {"id": "llm_mock", "name": "Mock", "provider": "mock"},
        ],
        "default_ai_model": "llm_custom1",
    })
    body = r.json()
    ids = [m["id"] for m in body["ai_models"]]
    assert "llm_custom1" in ids
    assert "llm_deepseek" not in ids  # 不再注入内置
    assert body["default_ai_model"] == "llm_custom1"
    # 默认模型的 provider 兼容字段应更新
    assert body["ai_provider"] == "cloud"
    assert body["ai_model"] == "gpt-test"
    # api_key 脱敏：sk-xxx 6 位 ≤ 8 → 退化为 •••
    m1 = next(m for m in body["ai_models"] if m["id"] == "llm_custom1")
    assert m1["api_key"] == "•••"


async def test_settings_legacy_put_updates_default_model(app_state):
    """旧格式 PUT ai_base_url 等字段应更新默认模型（向后兼容）。
    列表为空时旧格式 PUT 会新建一条默认模型并落到它上面。"""
    # 直接通过 store 测（避免 HTTP 路由的序列化问题）
    store = app_state.runtime
    store.update({
        "ai_provider": "cloud",
        "ai_base_url": "https://legacy.example.com/v1",
        "ai_model": "legacy-model",
    })
    after = store.get()
    # 旧格式 PUT 后必有默认模型（空列表则新建一条）
    assert after.default_ai_model
    m = next(x for x in after.ai_models if x.id == after.default_ai_model)
    assert m.base_url == "https://legacy.example.com/v1"
    assert m.model == "legacy-model"
    assert m.provider == "cloud"


async def test_settings_migration_from_legacy(app_state, tmp_path):
    """从旧格式 settings.json 加载时自动迁移为 ai_models 列表。"""
    import json
    from app.core.settings import SettingsStore
    # 模拟旧格式 settings.json
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "ai_provider": "local",
        "ai_base_url": "http://old.example.com/v1",
        "ai_model": "old-model",
        "ai_temperature": 0.7,
        "embedding_provider": "api",
        "embedding_model": "bge-legacy",
        "gate_review_threshold": 500,
    }), encoding="utf-8")

    store = SettingsStore(tmp_path)
    rs = store.get()
    assert len(rs.ai_models) >= 1
    # 迁移过来的模型应保留旧值
    migrated = next((m for m in rs.ai_models if m.model == "old-model"), None)
    assert migrated is not None
    assert migrated.provider == "local"
    assert migrated.temperature == 0.7
    # 嵌入也迁移了
    emb_migrated = next((m for m in rs.embedding_models if m.model == "bge-legacy"), None)
    assert emb_migrated is not None
    assert emb_migrated.provider == "api"
    # 闸门参数也保留
    assert rs.gate_review_threshold == 500


async def test_masked_key_not_overwritten(tmp_path):
    """全量写回时掩码 '•••' 不覆盖真实 api_key。"""
    from app.core.settings import SettingsStore
    store = SettingsStore(tmp_path)
    # 写入带真实 key 的模型
    store.update({"ai_models": [{
        "id": "llm_sec", "name": "密钥模型", "provider": "cloud",
        "base_url": "https://x.example.com/v1", "api_key": "real-secret-123",
        "model": "gpt-test",
    }], "default_ai_model": "llm_sec"})
    # 读取时脱敏：保留开头 / 结尾各 3 位
    pub = store.get().public()
    m = next(x for x in pub["ai_models"] if x["id"] == "llm_sec")
    assert m["api_key"] == "rea•••123"
    # 前端把掩码列表原样写回 → 真实 key 必须保留
    store.update({"ai_models": pub["ai_models"]})
    after = store.get()
    m2 = next(x for x in after.ai_models if x.id == "llm_sec")
    assert m2.api_key == "real-secret-123"
    # 未掩码的 key 正常写入
    store.update({"ai_models": [{
        "id": "llm_new", "name": "新", "provider": "cloud",
        "base_url": "https://y/v1", "api_key": "new-key", "model": "m",
    }]})
    m3 = next(x for x in store.get().ai_models if x.id == "llm_new")
    assert m3.api_key == "new-key"


def test_mask_key_edge_cases():
    """脱敏展示：保留首尾 3 位；过短 / 为空时退化为 ••• 或空。"""
    from app.core.settings import _mask_key, _is_masked

    assert _mask_key("real-secret-123") == "rea•••123"
    assert _mask_key("sk-abcdefghijklmnop") == "sk-•••nop"
    assert _mask_key("short") == "•••"          # 过短不给可辨识片段
    assert _mask_key("") == ""
    assert _mask_key(None) == ""
    assert _is_masked("rea•••123") is True
    assert _is_masked("real-secret-123") is False
    assert _is_masked("") is False


async def test_gateway_chat_stream_mock_yields_tool_call():
    """mock provider 流式：意图命中 → 流末一次性产出 tool_calls。"""
    import app.ai.gateway as gw

    g = gw.LLMGateway({"provider": "mock"})
    chunks = [c async for c in g.chat_stream([{"role": "user", "content": "查退货率最高的商品"}])]
    assert any(c.tool_calls for c in chunks)


async def test_gateway_chat_stream_real_yields_deltas(monkeypatch):
    """真实 cloud 端点：SSE 逐 token → 多个 delta 块。"""
    import app.ai.gateway as gw
    import httpx

    lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        "data: [DONE]",
    ]

    class _FakeStream:
        def __init__(self, ls):
            self.ls = ls
            self.status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for l in self.ls:
                yield l

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            return _FakeStream(lines)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    g = gw.LLMGateway({"provider": "cloud", "base_url": "http://x/v1", "api_key": "k", "model": "m"})
    chunks = [c async for c in g.chat_stream([{"role": "user", "content": "hi"}])]
    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["你", "好"]
