"""验收：query_audit 平台工具。"""
from __future__ import annotations

from app.ai.tools import execute_tool
from app.ai.tools.registry import TOOL_META


async def _seed_audits(app_state, conn_id, n=5):
    cfg = app_state.connections.get(conn_id)
    for i in range(n):
        app_state.audit.log(
            connection=cfg.name, origin="ai" if i % 2 == 0 else "manual",
            tier="read", verdict="allow" if i < 3 else "block",
            status=f"seed-{i}", sql=f"SELECT {i}", source="seed",
        )
    return cfg.name


def test_meta_trust_audit_source():
    meta = TOOL_META.get("query_audit")
    assert meta, "query_audit 未注册"
    assert meta["trust"] == "readonly"
    assert meta["confirm"] == "none"


async def test_query_audit_basic(app_state, conn_id):
    await _seed_audits(app_state, conn_id, 5)
    out = await execute_tool(app_state, "query_audit", {"limit": 20}, conn_id)
    assert out.result["ok"] is True
    assert out.result["total"] >= 5
    assert len(out.result["entries"]) >= 5


async def test_query_audit_filters(app_state, conn_id):
    name = await _seed_audits(app_state, conn_id, 6)
    out = await execute_tool(app_state, "query_audit", {"verdict": "block", "limit": 20}, conn_id)
    assert all(e["verdict"] == "block" for e in out.result["entries"])
    out2 = await execute_tool(app_state, "query_audit", {"connection": name, "limit": 20}, conn_id)
    assert all(e["connection"] == name for e in out2.result["entries"])
    out3 = await execute_tool(app_state, "query_audit", {"origin": "manual", "limit": 20}, conn_id)
    assert all(e["origin"] == "manual" for e in out3.result["entries"])


async def test_query_audit_pagination(app_state, conn_id):
    await _seed_audits(app_state, conn_id, 25)
    out = await execute_tool(app_state, "query_audit", {"limit": 5, "offset": 0}, conn_id)
    assert len(out.result["entries"]) == 5
    assert out.result["limit"] == 5
    out2 = await execute_tool(app_state, "query_audit", {"limit": 5, "offset": 5}, conn_id)
    assert out2.result["entries"][0]["sql"] != out.result["entries"][0]["sql"]


async def test_query_audit_redacts_in_standard(app_state, conn_id):
    app_state.runtime.update({"privacy_mode": "standard"})
    cfg = app_state.connections.get(conn_id)
    app_state.audit.log(
        connection=cfg.name, origin="ai", tier="read", verdict="allow",
        status="pii", sql="SELECT * FROM users WHERE phone='13812345678'", source="seed",
    )
    out = await execute_tool(app_state, "query_audit", {"limit": 20}, conn_id)
    for e in out.result["entries"]:
        if "users" in e.get("sql", "") and "phone" in e.get("sql", ""):
            assert "13812345678" not in e["sql"]
            break
    app_state.runtime.update({"privacy_mode": "standard"})


async def test_query_audit_open_no_redact(app_state, conn_id):
    app_state.runtime.update({"privacy_mode": "open"})
    cfg = app_state.connections.get(conn_id)
    app_state.audit.log(
        connection=cfg.name, origin="ai", tier="read", verdict="allow",
        status="pii2", sql="SELECT * FROM t WHERE phone='13900001111'", source="seed",
    )
    out = await execute_tool(app_state, "query_audit", {"limit": 20}, conn_id)
    assert any("13900001111" in e.get("sql", "") for e in out.result["entries"])
    app_state.runtime.update({"privacy_mode": "standard"})
