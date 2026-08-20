"""方言接入可扩展性：新增数据库只需注册 adapter，上层 sqlglot 方言映射自动生效，无需改白名单。"""

import pytest

from app.core.dialects.base import DialectAdapter
from app.core.dialects.registry import register_dialect, registry


class _FakeAdapter(DialectAdapter):
    name = "fake_dialect"
    sqlglot_name = "fakeglot"

    async def connect(self, cfg): return object()
    async def close(self, conn): pass
    async def is_healthy(self, conn): return True
    async def execute(self, conn, sql): raise NotImplementedError
    async def list_tables(self, conn): return []
    async def list_columns(self, conn, table): return []
    async def list_foreign_keys(self, conn): return []
    async def count_rows(self, conn, table): return 0
    def quote_ident(self, name): return f'"{name}"'
    def quote_literal(self, value): return str(value)


@pytest.fixture
def _fake_dialect():
    register_dialect(_FakeAdapter)
    yield
    registry._adapters.pop("fake_dialect", None)   # 清理，避免污染其它测试


def test_sqlglot_dialect_for_resolves_registered(_fake_dialect):
    from app.safety.gate import sqlglot_dialect_for

    assert sqlglot_dialect_for("fake_dialect") == "fakeglot"
    assert sqlglot_dialect_for("postgres") == "postgres"   # 既有方言不变


def test_query_sqlglot_dialect_resolves_registered(_fake_dialect):
    from app.core.query import _sqlglot_dialect

    assert _sqlglot_dialect("fake_dialect") == "fakeglot"
    assert _sqlglot_dialect("不存在的方言") == "sqlite"      # 未注册兜底
