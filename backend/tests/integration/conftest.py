"""Docker 门控集成测试：验证 PG/MySQL 适配器的真实行为。

运行方式（需 docker）：
    docker compose -f docker-compose.integration.yml up -d
    TABLETALK_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v

不设 TABLETALK_INTEGRATION 时整目录跳过（日常全量测试不受影响）。
连接参数可用 TABLETALK_TEST_*_* 环境变量覆盖，默认指向 compose 里的实例。
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _require_docker():
    """未设 TABLETALK_INTEGRATION 时跳过全部集成测试（docker 未起时不碰真实库）。"""
    if not os.environ.get("TABLETALK_INTEGRATION"):
        pytest.skip("需要 TABLETALK_INTEGRATION=1 与 docker 数据库（见 docker-compose.integration.yml）")


@pytest.fixture
def pg_cfg() -> dict:
    return {
        "host": os.environ.get("TABLETALK_TEST_PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("TABLETALK_TEST_PG_PORT", "5432")),
        "user": os.environ.get("TABLETALK_TEST_PG_USER", "tabletalk"),
        "password": os.environ.get("TABLETALK_TEST_PG_PASSWORD", "tabletalk"),
        "database": os.environ.get("TABLETALK_TEST_PG_DB", "tabletalk_test"),
    }


@pytest.fixture
def mysql_cfg() -> dict:
    return {
        "host": os.environ.get("TABLETALK_TEST_MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("TABLETALK_TEST_MYSQL_PORT", "3306")),
        "user": os.environ.get("TABLETALK_TEST_MYSQL_USER", "tabletalk"),
        "password": os.environ.get("TABLETALK_TEST_MYSQL_PASSWORD", "tabletalk"),
        "database": os.environ.get("TABLETALK_TEST_MYSQL_DB", "tabletalk_test"),
    }
