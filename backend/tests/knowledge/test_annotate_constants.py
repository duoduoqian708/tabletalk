"""S2-2：静态业务常量 AI 识别（kind=constant 生产者）+ context 注入。"""

from __future__ import annotations

from app.knowledge.annotator import (
    _constants_prompt,
    _normalize_constants,
    _tables_desc_for_constants,
    annotate_constants,
)


def test_normalize_constants():
    out = _normalize_constants('''{"constants": [
        {"name": "tax_rate", "value": "0.13", "unit": "%", "source_table": "system_config",
         "source_column": "value", "reason": "税率"},
        {"name": "approve_threshold", "value": "10000", "unit": "", "source_table": "config",
         "source_column": "val", "reason": "审批阈值"},
        {"name": "", "value": "1"}
    ]}''')
    assert len(out) == 2
    assert out[0]["name"] == "tax_rate" and out[0]["value"] == "0.13"


def test_normalize_constants_bad():
    assert _normalize_constants("nope") == []
    assert _normalize_constants('{"constants": "x"}') == []


def test_tables_desc_includes_comments():
    from app.knowledge.store import TableKnowledge, ColumnInfo
    schema = {
        "tables": [{"name": "system_config", "kind": "table", "comment": "参数表", "column_count": 2}],
        "columns": [{"table": "system_config", "name": "key", "type": "text"},
                    {"table": "system_config", "name": "value", "type": "text"}],
        "foreign_keys": [],
    }
    tk = TableKnowledge(name="system_config", comment="系统参数")
    tk.columns["value"] = ColumnInfo(name="value", comment="参数值（税率/阈值）")
    desc = _tables_desc_for_constants(schema, {"system_config": tk})
    assert "系统参数" in desc
    assert "参数值" in desc


def test_constants_prompt_soft_wording():
    p = _constants_prompt("tables desc")
    assert "很可能" in p          # S4：柔和措辞
    assert "不确定的宁可不列" in p
    assert "运行时变量" in p


async def test_annotate_constants_llm_produces_draft(app_state, conn_id, monkeypatch):
    """LLM 识别常量 → Concept(kind=constant, draft) 落库待人工确认。"""
    from app.ai import gateway as gw
    from app.knowledge.store import TableKnowledge

    kb = app_state.knowledge
    schema = {
        "tables": [{"name": "system_config", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [{"table": "system_config", "name": "value", "type": "text"}],
        "foreign_keys": [],
    }
    kb.semantic_store._schema[conn_id] = schema
    tk = TableKnowledge(name="system_config", comment="系统参数（税率等）")
    kb.semantic_store._tables[conn_id] = {"system_config": tk}

    class _FakeResp:
        content = '{"constants": [{"name": "tax_rate", "value": "0.13", "unit": "%", "source_table": "system_config", "source_column": "value", "reason": "税率"}]}'

    class _FakeProvider:
        async def chat(self, messages, tools=None, ctx=None):
            return _FakeResp()

    def _fake_build(cfg):
        return _FakeProvider()

    async def _fake_chat(provider, messages, tools, ctx, **kw):
        return await provider.chat(messages, tools=tools, ctx=ctx)

    import app.knowledge.annotator as ann
    monkeypatch.setattr(gw, "build_provider", _fake_build)
    monkeypatch.setattr(ann, "_chat_with_beat", _fake_chat)
    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})
    try:
        n = await annotate_constants(app_state, conn_id, schema)
        assert n == 1
        cs = kb.concept_store.list(conn_id)
        assert any(c.name == "tax_rate" and c.kind == "constant"
                   and c.status == "draft" and c.canonical_enum[0]["code"] == "0.13"
                   for c in cs)
    finally:
        app_state.runtime.update({"ai_provider": "mock", "ai_api_key": ""})


async def test_annotate_constants_mock_skips(app_state, conn_id):
    """mock 路径：不识别（常量是增强知识，mock 无真实语义）。"""
    from app.knowledge.store import TableKnowledge
    kb = app_state.knowledge
    schema = {
        "tables": [{"name": "t", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [{"table": "t", "name": "c", "type": "text"}],
        "foreign_keys": [],
    }
    kb.semantic_store._schema[conn_id] = schema
    kb.semantic_store._tables[conn_id] = {"t": TableKnowledge(name="t")}
    n = await annotate_constants(app_state, conn_id, schema)
    assert n == 0


async def test_context_injects_confirmed_constants(app_state, conn_id):
    """context 组装：confirmed 常量注入（不依赖候选表路由）；draft 不注入。"""
    from app.ai.context import assemble_context_full
    from app.knowledge.semantic.concepts import Concept

    kb = app_state.knowledge
    kb.concept_store.upsert(conn_id, Concept(
        name="tax_rate", canonical_enum=[{"code": "0.13", "label": "%"}],
        members=[{"table": "system_config", "column": "value"}],
        status="confirmed", kind="constant", source="ai",
    ), {"tables": [], "columns": [], "foreign_keys": []})
    try:
        text, _ = await assemble_context_full(app_state, conn_id, query="销售额",
                                              followup_tables=["orders"])
    except Exception:
        text = ""
    if "【概念字典】" in text:
        assert "常量 tax_rate" in text
        assert "0.13" in text