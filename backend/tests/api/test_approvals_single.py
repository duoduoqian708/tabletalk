"""单机模式全链路：创建→批准→执行→executed_audit_id/rollback_ref 回填。"""
from __future__ import annotations

_WITH_WHERE = "UPDATE products SET price = 9 WHERE id = 1"


async def test_single_mode_full_chain(app_state, conn_id, demo_db, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post("/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE})
    aid = r.json()["id"]

    # 创建即快照影响行数预览
    created = app_state.approvals.get(aid)
    assert created.preview_rows is not None and created.preview_rows >= 1

    r2 = await client.post(f"/api/v1/approvals/{aid}/approve", json={})
    assert r2.status_code == 200, r2.text
    a = app_state.approvals.get(aid)
    assert a.status == "approved"
    assert isinstance(a.executed_audit_id, int) and a.executed_audit_id > 0
    assert a.rollback_ref  # 有回滚剧本哈希


async def test_single_mode_reject_allowed(app_state, conn_id, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post("/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE})
    aid = r.json()["id"]
    r2 = await client.post(f"/api/v1/approvals/{aid}/reject", json={"note": "先不动"})
    assert r2.status_code == 200 and r2.json()["status"] == "rejected"


async def test_double_decide_conflict(app_state, conn_id, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post("/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE})
    aid = r.json()["id"]
    await client.post(f"/api/v1/approvals/{aid}/reject", json={})
    r2 = await client.post(f"/api/v1/approvals/{aid}/approve", json={})
    assert r2.status_code in (404, 409)   # 非 pending 一律拒绝（幂等底线）
