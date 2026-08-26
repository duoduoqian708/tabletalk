"""signal 端点：徽章计数。"""
from __future__ import annotations


async def test_signal_counts(app_state, client):
    app_state.audit.log(connection="demo", origin="ai", tier="dml",
                        verdict="block", status="拦截", sql="DELETE FROM t")
    app_state.audit.log(connection="demo", origin="ai", tier="dml",
                        verdict="review", status="需确认", sql="UPDATE t SET a=1 WHERE id=1")
    app_state.audit.log(connection="demo", origin="ai", tier="read",
                        verdict="allow", status="放行", sql="SELECT 1")
    app_state.approvals.create("conn-x", "UPDATE t SET a=1 WHERE id=1", "someone")
    r = await client.get("/api/v1/audit/signal")
    assert r.status_code == 200
    body = r.json()
    assert body["unread_exceptions"] == 2
    assert body["pending_approvals"] == 1
    assert body["today"]["blocked"] == 1 and body["today"]["review"] == 1


async def test_signal_connection_filter(app_state, client):
    app_state.audit.log(connection="a", origin="ai", tier="dml", verdict="block", status="拦截", sql="DELETE FROM t")
    r = await client.get("/api/v1/audit/signal", params={"connection": "b"})
    assert r.json()["unread_exceptions"] == 0
