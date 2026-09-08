"""内置供应商清单 / 上游模型列表代理端点测试。"""
from __future__ import annotations

from app.ai.providers import builtin_embedding_providers, builtin_providers, resolve_adapter_name


async def test_builtin_providers_list(client):
    r = await client.get("/api/v1/ai/providers")
    assert r.status_code == 200
    ps = r.json()["providers"]
    assert len(ps) == 8  # 7 家 + 火山双路
    displays = [p["display"] for p in ps]
    assert displays.count("火山（按量）") == 1
    assert displays.count("火山（Agent Plan）") == 1
    for p in ps:
        assert p["adapter"] == resolve_adapter_name(p["name"])
    plan = next(p for p in ps if p["display"] == "火山（Agent Plan）")
    assert plan["base_url"].endswith("/api/plan/v3")


async def test_builtin_embedding_providers_list(client):
    r = await client.get("/api/v1/ai/embedding/providers")
    assert r.status_code == 200
    ps = r.json()["providers"]
    assert [p["display"] for p in ps] == [
        "智谱GLM", "通义千问", "火山（按量）", "火山（Agent Plan）", "Ollama", "自定义（OpenAI 兼容）",
    ]
    for p in ps:
        assert p["adapter"] == resolve_adapter_name(p["name"])
    zp = next(p for p in ps if p["display"] == "智谱GLM")
    assert zp["models"] == "embedding-3" and zp["dimensions"] == "2048"
    plan = next(p for p in ps if p["display"] == "火山（Agent Plan）")
    assert plan["base_url"].endswith("/api/plan/v3") and plan["dimensions"] == "2048"
    ol = next(p for p in ps if p["display"] == "Ollama")
    assert ol["base_url"] == "http://127.0.0.1:11434/v1" and ol["models"] == "bge-m3"


def test_builtin_providers_module_level():
    assert len(builtin_providers()) == 8
    assert len(builtin_embedding_providers()) == 6


async def test_upstream_models_success(client, monkeypatch):
    """只替换 app.api.ai 命名空间内的 httpx（不能全局 patch AsyncClient.get，
    否则会连测试客户端自己一起劫持）。"""
    import httpx

    from app.api import ai as ai_mod

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "a-model"}]}

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

    class _FakeHttpx:
        AsyncClient = _FakeClient
        HTTPError = httpx.HTTPError

    monkeypatch.setattr(ai_mod, "httpx", _FakeHttpx)
    r = await client.get(
        "/api/v1/ai/upstream/models",
        params={"base_url": "https://x.example/v1/", "api_key": "sk-test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["models"] == ["a-model", "b-model"]  # 去重 + 排序
    assert captured["url"] == "https://x.example/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


async def test_upstream_models_unsupported(client, monkeypatch):
    """上游不支持 /models（如方舟）→ ok=false，前端回退自由输入。"""
    import httpx

    from app.api import ai as ai_mod

    class _Resp:
        status_code = 404

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **kw):
            return _Resp()

    class _FakeHttpx:
        AsyncClient = _FakeClient
        HTTPError = httpx.HTTPError

    monkeypatch.setattr(ai_mod, "httpx", _FakeHttpx)
    r = await client.get(
        "/api/v1/ai/upstream/models",
        params={"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["supported"] is False


async def test_upstream_models_bad_base_url(client):
    r = await client.get("/api/v1/ai/upstream/models", params={"base_url": "ftp://x"})
    assert r.status_code == 422
