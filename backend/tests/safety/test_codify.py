"""T6.4 codify 单测：生成/还原往返、同表同码、冲突递增、列级、双语 sensitive。"""
from __future__ import annotations

import pathlib

import pytest


def test_codify_table_stable(tmp_path):
    from app.safety.codify import codify_table, decodify_text

    p = tmp_path
    p.mkdir(exist_ok=True)
    conn = "c1"
    code1 = codify_table(p, conn, "customers")
    code2 = codify_table(p, conn, "customers")
    assert code1 == code2
    assert code1.startswith("t_")
    # 还原
    assert decodify_text(p, conn, f"SELECT * FROM {code1}") == "SELECT * FROM customers"


def test_codify_table_conflict_increment(monkeypatch, tmp_path):
    from app.safety import codify as mod

    p = tmp_path
    p.mkdir(exist_ok=True)
    conn = "c2"
    # 强制让两个不同表 hash 相同：patch _hash 返回固定值
    monkeypatch.setattr(mod, "_hash", lambda s: 42)
    code_a = mod.codify_table(p, conn, "table_a")
    code_b = mod.codify_table(p, conn, "table_b")
    assert code_a != code_b
    assert code_a == "t_42"
    assert code_b == "t_43"  # 冲突递增
    # 同表再次请求应稳定
    assert mod.codify_table(p, conn, "table_a") == code_a


def test_codify_column_and_decodify(tmp_path):
    from app.safety.codify import codify_column, decodify_text

    p = tmp_path
    p.mkdir(exist_ok=True)
    conn = "c3"
    # 先 codify 表以建立映射
    from app.safety.codify import codify_table
    codify_table(p, conn, "customers")
    code_col = codify_column(p, conn, "customers", "phone")
    assert code_col.startswith("c_")
    # 列还原：decodify_text 会把 c_* 换回列名
    assert decodify_text(p, conn, f"SELECT {code_col} FROM t_xx") == "SELECT phone FROM t_xx"
    # 同列同码
    assert codify_column(p, conn, "customers", "phone") == code_col


def test_codify_schema_sensitive(tmp_path):
    from app.safety.codify import codify_schema, codify_table

    p = tmp_path
    p.mkdir(exist_ok=True)
    conn = "c4"
    schema = {
        "connection": "test",
        "dialect": "sqlite",
        "tables": [{"name": "customers", "comment": "客户表"}, {"name": "orders", "comment": ""}],
        "columns": [
            {"table": "customers", "name": "phone", "type": "TEXT", "comment": "手机号"},
            {"table": "orders", "name": "amount", "type": "INTEGER", "comment": ""},
        ],
        "foreign_keys": [{"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"}],
    }
    # 仅 customers 敏感
    out = codify_schema(p, conn, schema, {"customers"})
    # 敏感表名应被代号
    assert out["tables"][0]["name"] != "customers"
    assert out["tables"][0]["name"].startswith("t_")
    assert out["tables"][1]["name"] == "orders"
    # 敏感列名应被代号
    assert out["columns"][0]["name"].startswith("c_")
    assert out["columns"][1]["name"] == "amount"
    # 注释剥离
    assert out["tables"][0]["comment"] == ""
    # FK 代号化
    assert out["foreign_keys"][0]["ref_table"] != "customers"


def test_is_sensitive_table_bilingual(tmp_path):
    from app.safety.codify import is_sensitive_table

    p = tmp_path
    p.mkdir(exist_ok=True)
    # 构造最小 state mock
    class FakeKB:
        def tags(self, conn_id):
            return {
                "library": [
                    {"name": "sensitive", "status": "confirmed"},
                    {"name": "敏感", "status": "confirmed"},
                    {"name": "draft_tag", "status": "draft"},
                ],
                "tables": {
                    "customers": ["sensitive"],
                    "orders": ["敏感"],
                    "products": ["draft_tag"],
                },
            }

    class FakeState:
        knowledge = FakeKB()

    s = FakeState()
    assert is_sensitive_table(s, "any", "customers") is True
    assert is_sensitive_table(s, "any", "orders") is True
    assert is_sensitive_table(s, "any", "products") is False
    assert is_sensitive_table(s, "any", "unknown") is False


def test_codify_persistence_across_reload(tmp_path):
    from app.safety.codify import codify_table, _CACHE

    p = tmp_path
    p.mkdir(exist_ok=True)
    conn = "c5"
    code1 = codify_table(p, conn, "sensitive_table")
    # 清缓存模拟重启
    _CACHE.clear()
    code2 = codify_table(p, conn, "sensitive_table")
    assert code1 == code2


def test_end_to_end_sensitive_query_via_tools(tmp_path, app_state, conn_id):
    """端到端：敏感表 AI 查询不再报 no such table: t_xx（T6.1 往返不变式）。"""
    import asyncio

    from app.core.connections import ConnectionRegistry
    from app.safety.codify import codify_table

    # 构造敏感标签：通过 knowledge 直接打标（模拟已确认 sensitive）
    # 为简化，直接用 codify_table 生成代号，再构造代号 SQL，走 _run_query 应能执行
    from app.ai.tools.sql import _run_query
    from app.config import get_env

    dd = get_env().data_dir
    # 真实表为演示库的 orders（确保存在）
    real = "orders"
    code = codify_table(dd, conn_id, real)
    # 模型产出的代号 SQL
    codified_sql = f"SELECT COUNT(*) AS cnt FROM {code}"
    # 执行应成功（_decodify 会还原）
    async def _run():
        res = await _run_query(app_state, {"sql": codified_sql}, conn_id, include_data=True)
        assert res.result.get("ok") is True
        assert res.card.get("sql") == f"SELECT COUNT(*) AS cnt FROM {real}"
        # T6.2：若列名敏感，columns 应为代号；此处 orders 非敏感，保持原文
        assert "cnt" in res.result.get("columns", [])

    asyncio.run(_run())
