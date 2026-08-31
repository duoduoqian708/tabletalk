"""安全参数 / 通用 运行时设置的持久化与真值端点测试。"""
from __future__ import annotations

import pytest


async def test_update_settings_triggers_pool_rebuild(client, monkeypatch):
    import app.api.settings as settings_mod

    called = {}

    async def fake_rebuild(self):
        called["rebuild"] = True

    monkeypatch.setattr("app.core.pool.PoolManager.rebuild", fake_rebuild)
    r = await client.put("/api/v1/settings", json={"pool_size": 5})
    assert r.status_code == 200
    assert called.get("rebuild") is True


async def test_settings_persists_query_max_rows_and_pool_size(client):
    r = await client.put("/api/v1/settings", json={"query_max_rows": 50, "pool_size": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["query_max_rows"] == 50
    assert body["pool_size"] == 7


async def test_settings_runtime_section_present(client):
    r = await client.get("/api/v1/settings")
    body = r.json()
    assert "runtime" in body
    assert body["runtime"]["port"] == 8777
    assert body["runtime"]["auth"] == "X-TableTalk-Token · 本机"
    assert isinstance(body["runtime"]["data_dir"], str) and body["runtime"]["data_dir"]


async def test_settings_rejects_nonpositive_pool_size(client):
    # 先写入合法值
    await client.put("/api/v1/settings", json={"pool_size": 7})
    # 再发非法值（负数经 Pydantic 仍为 int，可到达守卫）
    r = await client.put("/api/v1/settings", json={"pool_size": -5})
    assert r.status_code == 200
    # 非法值不写入，保留上次的合法值
    assert r.json()["pool_size"] == 7


async def test_pool_for_reads_runtime_pool_size(monkeypatch):
    from app.core.pool import PoolManager
    from app.core.settings import RuntimeSettings

    class FakeRegistry:
        def get(self, conn_id):
            class Cfg:
                dialect = "postgres"

            return Cfg()

    rs = RuntimeSettings(pool_size=9)

    class FakeRuntime:
        def get(self):
            return rs

    class FakeState:
        runtime = FakeRuntime()

    monkeypatch.setattr("app.state.get_state", lambda: FakeState())
    pm = PoolManager(FakeRegistry())
    pool = pm._pool_for("c1")
    assert pool.max_size == 9


def test_no_auto_cap_execution_layer():
    """S1：执行层不再自动注入 LIMIT（_auto_cap 已移除，LIMIT 纪律在提示词由 LLM 织入）。"""
    import importlib
    from app.core import query as core_query
    importlib.reload(core_query)
    assert not hasattr(core_query, "_auto_cap")


async def test_execute_uses_runtime_query_max_rows(app_state, conn_id):
    from app.core.query import execute
    # 把运行时行数上限压到 5，验证 execute 据此截断
    app_state.runtime.update({"query_max_rows": 5})
    res = await execute(app_state, conn_id, "SELECT * FROM orders")
    # 注入 LIMIT 6，序列化截到 5，truncated=True
    assert res["truncated"] is True
    assert len(res["rows"]) <= 5


async def test_pool_rebuild_clears_pools(monkeypatch):
    from app.core.pool import PoolManager
    from app.core.settings import RuntimeSettings

    captured = {}

    class FakeRegistry:
        def get(self, conn_id):
            class Cfg:
                dialect = "postgres"

            return Cfg()

    class FakeRuntime:
        def get(self):
            return RuntimeSettings(pool_size=captured.get("size", 3))

    class FakeState:
        runtime = FakeRuntime()

    monkeypatch.setattr("app.state.get_state", lambda: FakeState())
    pm = PoolManager(FakeRegistry())
    pm._pool_for("c1")
    assert "c1" in pm._pools
    await pm.rebuild()
    assert pm._pools == {}
    # 重建后取池应使用新 runtime size
    captured["size"] = 9
    pool = pm._pool_for("c1")
    assert pool.max_size == 9
