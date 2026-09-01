"""WS7 T7.1 验收：工具注册规格扩展（trust/confirm/audit_source 元数据）。"""
from __future__ import annotations

import pytest

from app.ai.tools import registry as reg


async def _noop(state, args, conn_id, include_data=False):
    return reg.ToolOutcome(result={"ok": True})


def test_register_without_trust_rejected():
    with pytest.raises(ValueError):
        reg.register_tool(
            "_bad_tool", "x", {}, [], _noop,
        )


def test_register_bad_trust_value_rejected():
    with pytest.raises(ValueError):
        reg.register_tool(
            "_bad_tool2", "x", {}, [], _noop, trust="superpower",
        )


def test_register_bad_confirm_value_rejected():
    with pytest.raises(ValueError):
        reg.register_tool(
            "_bad_tool3", "x", {}, [], _noop, trust="readonly", confirm="maybe",
        )


def test_existing_tools_have_meta_and_pass_selfcheck():
    names = {t["function"]["name"] for t in reg.TOOL_SCHEMAS}
    assert {
        "get_schema", "run_query", "run_dml", "draft_ddl", "ai_review",
        "query_audit", "kb_read", "kb_write", "graph_read", "graph_write",
        "suggest_followup",
    } <= names
    meta = reg.TOOL_META
    assert meta["get_schema"]["trust"] == "readonly"
    assert meta["run_query"]["trust"] == "readonly"
    assert meta["ai_review"]["trust"] == "readonly"
    assert meta["query_audit"]["trust"] == "readonly"
    assert meta["draft_ddl"]["trust"] == "readonly", "草稿工具不执行，运行期只读"
    assert meta["run_dml"]["trust"] == "mutating"
    assert meta["run_dml"]["confirm"] == "card"
    assert meta["run_query"]["confirm"] == "none"
    reg.validate_registry()


def test_validate_registry_catches_missing_meta():
    reg._TOOL_HANDLERS["_orphan"] = _noop
    try:
        with pytest.raises(ValueError):
            reg.validate_registry()
    finally:
        del reg._TOOL_HANDLERS["_orphan"]
