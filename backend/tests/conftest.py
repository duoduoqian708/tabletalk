"""测试 fixtures：每个测试独立数据目录 + SQLite demo 库 + HTTP 客户端。"""
from __future__ import annotations

import os

import pytest

# 必须在任何 app.* 导入之前设置：sidecar token 鉴权中间件需要它
os.environ.setdefault("CLEARED_SIDECAR_TOKEN", "test-token")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: Docker 门控集成测试（需 CLEARED_INTEGRATION=1）"
    )


@pytest.fixture(autouse=True)
async def _isolate(tmp_path):
    """每个测试前重建应用状态（独立数据目录）；测试后关闭连接池，避免 aiosqlite 线程挂起进程退出。

    注意：不调用 get_state()（它会初始化默认 ~/.cleared 并触发 kb_status 迁移写盘）。
    """
    from app.state import _state, reset_state

    prev = _state
    if prev is not None and prev.pools:
        await prev.pools.close_all()
    st = reset_state(tmp_path / "data")
    yield st
    await st.pools.close_all()


@pytest.fixture
def app_state():
    from app.state import get_state

    return get_state()


@pytest.fixture
def demo_db(tmp_path):
    from scripts.seed_demo_db import build_demo_db

    p = tmp_path / "demo.db"
    build_demo_db(p)
    return p


@pytest.fixture
def conn_id(app_state, demo_db) -> str:
    c = app_state.connections.create(
        {"name": "demo", "dialect": "sqlite", "file": str(demo_db)}
    )
    # 既有测试假定连接可直接使用：默认放行知识库状态机（状态机专属测试自管状态）
    app_state.connections.set_kb_status(c.id, "ready")
    return c.id


@pytest.fixture
async def client(app_state, conn_id):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    headers = {"X-Cleared-Token": os.environ["CLEARED_SIDECAR_TOKEN"]}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c
