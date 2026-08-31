"""S2-1：表级过滤器 AI 语义确认——启发式只作预标记，AI 裁决为权威知识。

- soft_delete（值域 0/1/NULL + high 置信）→ 自动 confirmed（低风险）
- tenant / exempt → draft（人工确认）
- 失败静默：不影响构建
"""
from __future__ import annotations

import pytest

from app.knowledge.annotator import (
    _mock_filter_verdicts,
    _normalize_filter_verdicts,
    _soft_delete_values_ok,
    annotate_filters,
)


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 4},
            {"name": "system_config", "kind": "table", "comment": "", "column_count": 1},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False},
            {"table": "orders", "name": "is_deleted", "type": "int", "pk": False, "fk": False},
            {"table": "orders", "name": "tenant_id", "type": "int", "pk": False, "fk": False},
            {"table": "orders", "name": "total_amount", "type": "real", "pk": False, "fk": False},
            {"table": "customers", "name": "id", "type": "int", "pk": True, "fk": False},
            {"table": "customers", "name": "is_deleted", "type": "int", "pk": False, "fk": False},
            {"table": "system_config", "name": "key", "type": "text", "pk": False, "fk": False},
        ],
        "foreign_keys": [],
    }


def test_normalize_filter_verdicts():
    out = _normalize_filter_verdicts('''{"filters": [
        {"table": "orders", "verdict": "soft_delete", "column": "is_deleted",
         "predicate": "is_deleted = 0", "confidence": "high", "reason": "值域 0/1"},
        {"table": "orders", "verdict": "tenant", "column": "tenant_id",
         "predicate": "tenant_id = :current_tenant", "confidence": "medium", "reason": "租户列"},
        {"table": "system_config", "verdict": "exempt", "confidence": "high", "reason": "字典表"},
        {"table": "ghost", "verdict": "tenant", "column": "x", "predicate": "x = 1", "confidence": "high"}
    ]}''', ["orders", "system_config"])
    assert len(out) == 3  # ghost 表丢弃
    assert out[0]["verdict"] == "soft_delete"
    assert out[2]["verdict"] == "exempt"


def test_normalize_bad_json_empty():
    assert _normalize_filter_verdicts("not json", ["orders"]) == []
    assert _normalize_filter_verdicts('{"filters": "x"}', ["orders"]) == []


def test_soft_delete_values_ok():
    assert _soft_delete_values_ok({"t": {"f": [0, 1, None]}}, "t", "f") is True
    assert _soft_delete_values_ok({"t": {"f": [0, 1, 2]}}, "t", "f") is False  # 值域外 → 不自动
    assert _soft_delete_values_ok({}, "t", "f") is False  # 无采样 → 不自动（保守）


def test_mock_filter_verdicts_from_heuristics():
    from app.knowledge.build import BuildService
    cands = BuildService()._detect_filter_candidates(_schema())
    hints = {(c["table"], c["hint"]) for c in cands}
    assert ("orders", "soft_delete") in hints
    assert ("orders", "tenant") in hints
    out = _mock_filter_verdicts(_schema(), cands)
    assert any(v["verdict"] == "soft_delete" and v["confidence"] == "high" for v in out)
    assert any(v["verdict"] == "tenant" and v["confidence"] == "medium" for v in out)


async def test_annotate_filters_mock_auto_confirms_soft_delete(app_state, conn_id):
    """mock 路径：仅软删表（customers）自动 confirmed；含租户表（orders）整表 draft。"""
    kb = app_state.knowledge
    kb.semantic_store._schema[conn_id] = _schema()
    kb.semantic_store._samples[conn_id] = {
        "orders": {"is_deleted": [0, 1], "tenant_id": [7, 9]},
        "customers": {"is_deleted": [0, 1]},
    }
    r = await annotate_filters(app_state, conn_id, _schema())
    assert r["auto_confirmed"] >= 1, r
    fs = kb.filter_store
    assert fs._filters[conn_id]["customers"].status == "confirmed"  # 仅软删 → 自动
    assert fs._filters[conn_id]["orders"].status == "draft"          # 含租户 → 人工
    assert "tenant_id = :current_tenant" in fs._filters[conn_id]["orders"].predicate
    # 值域含 2 → 不自动确认
    kb.semantic_store._samples[conn_id] = {"customers": {"is_deleted": [0, 1, 2]}}
    kb.filter_store._filters.pop(conn_id, None)
    r2 = await annotate_filters(app_state, conn_id, _schema())
    assert r2["auto_confirmed"] == 0, r2
    assert fs._filters[conn_id]["customers"].status == "draft"


async def test_annotate_filters_llm_adjudication(app_state, conn_id, monkeypatch):
    """LLM 路径：LLM 裁决豁免表 → exempt draft；软删高置信 → 自动 confirmed。"""
    from app.ai import gateway as gw

    kb = app_state.knowledge
    kb.semantic_store._schema[conn_id] = _schema()
    kb.semantic_store._samples[conn_id] = {"orders": {"is_deleted": [0, 1]}}

    class _FakeResp:
        content = '{"filters": [{"table": "orders", "verdict": "soft_delete", "column": "is_deleted", "predicate": "is_deleted = 0", "confidence": "high", "reason": "值域 0/1"}, {"table": "system_config", "verdict": "exempt", "confidence": "high", "reason": "参数表"}]}'

    class _FakeProvider:
        async def chat(self, messages, tools=None, ctx=None):
            return _FakeResp()

    def _fake_build(cfg):
        return _FakeProvider()

    async def _fake_chat_with_beat(provider, messages, tools, ctx, **kw):
        return await provider.chat(messages, tools=tools, ctx=ctx)

    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})
    monkeypatch.setattr(gw, "build_provider", _fake_build)
    import app.knowledge.annotator as ann
    monkeypatch.setattr(ann, "_chat_with_beat", _fake_chat_with_beat)
    r = await annotate_filters(app_state, conn_id, _schema())
    assert r["auto_confirmed"] == 1, r
    fs = kb.filter_store
    assert fs._filters[conn_id]["orders"].status == "confirmed"
    assert fs._filters[conn_id]["system_config"].scope == "exempt"
    assert fs._filters[conn_id]["system_config"].status == "draft"
    app_state.runtime.update({"ai_provider": "mock", "ai_api_key": ""})

# ---------- S2-4：守卫边 AI 字段 ----------

def test_parse_graph_edges_guard():
    from app.knowledge.annotator import _parse_graph_edges
    schema = {
        "tables": [{"name": "comments", "kind": "table", "comment": "", "column_count": 3},
                   {"name": "orders", "kind": "table", "comment": "", "column_count": 1},
                   {"name": "users", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [
            {"table": "comments", "name": "id", "type": "int", "pk": True},
            {"table": "comments", "name": "type", "type": "int", "pk": False},
            {"table": "comments", "name": "ref_id", "type": "int", "pk": False},
            {"table": "orders", "name": "id", "type": "int", "pk": True},
            {"table": "users", "name": "id", "type": "int", "pk": True},
        ],
        "foreign_keys": [],
    }
    edges = _parse_graph_edges('''[
        {"from_table": "comments", "from_col": "ref_id", "to_table": "orders", "to_col": "id",
         "cardinality": "n:1", "confidence": "high", "reason": "多态", "guard": "comments.type = 1"}
    ]''', schema)
    assert edges and edges[0]["guard"] == "comments.type = 1"
    # 无 guard → None
    edges2 = _parse_graph_edges('''[
        {"from_table": "comments", "from_col": "ref_id", "to_table": "users", "to_col": "id",
         "cardinality": "n:1", "confidence": "high", "reason": "普通"}
    ]''', schema)
    assert edges2 and edges2[0]["guard"] is None
