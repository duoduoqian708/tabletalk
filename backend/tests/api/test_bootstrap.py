"""bootstrap 引导端点 + 静态托管：免鉴权、token 发放、受保护端点回归、SPA 首页放行。"""
from __future__ import annotations

import os

import httpx


def _anon_client() -> httpx.AsyncClient:
    from httpx import ASGITransport

    from app.main import app

    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_bootstrap_issues_token_without_auth():
    async with _anon_client() as c:
        r = await c.get("/api/v1/bootstrap")
        assert r.status_code == 200
        body = r.json()
        assert body["token"] == os.environ["CLEARED_SIDECAR_TOKEN"]
        assert body["dataDir"]


async def test_health_and_bootstrap_exempt_but_others_401():
    async with _anon_client() as c:
        assert (await c.get("/api/v1/health")).status_code == 200
        assert (await c.get("/api/v1/bootstrap")).status_code == 200
        # 受保护端点仍要求 token（回归）
        assert (await c.get("/api/v1/connections")).status_code == 401
        assert (await c.post("/api/v1/query", json={"connection_id": "x", "sql": "SELECT 1"})).status_code == 401


async def test_root_served_without_token():
    """SPA 静态首页（或未构建占位）不要求 token，且为 HTML。"""
    async with _anon_client() as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "").lower()


def test_ensure_demo_db_seeds(tmp_path, monkeypatch):
    """原 Electron ensureDemoDb 迁入后端：首次启动在 data_dir 播种 demo.db。"""
    import app.config as config

    from app.main import _ensure_demo_db

    s = config.Settings.from_env()
    s.data_dir = tmp_path
    monkeypatch.setattr(config, "_settings", s)
    _ensure_demo_db()
    assert (tmp_path / "demo.db").exists()
