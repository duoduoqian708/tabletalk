"""T4.5 验收：审批流（E2）闸门不变式。

核心用例：批准的无 WHERE UPDATE 必须被闸门拦（审计 2026-08-21 高优 bug E2 回归保护）。
另覆盖：单机模式拒绝、非 admin 403、创建审批记录真实 verdict/连接名、
连接缺失 fail-closed、合法 UPDATE 批准执行 + 审计链、只读连接拦截、驳回。
"""
from __future__ import annotations

import pytest

_NO_WHERE = "UPDATE products SET price = 9"
_WITH_WHERE = "UPDATE products SET price = 9 WHERE id = 1"


def _mk_user(state, username, role):
    u = state.auth.create_user(username, f"{username}-pass", role)
    return state.auth.issue_token(u)


def _auth(token):
    return {"X-TableTalk-Token": token}


async def _create_approval(client, conn_id, sql, token):
    return await client.post(
        "/api/v1/approvals",
        json={"connection_id": conn_id, "sql": sql},
        headers=_auth(token),
    )


# ---- 准入与角色 ----


async def test_single_mode_create_allowed(app_state, conn_id, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post(
        "/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


async def test_non_admin_approve_forbidden(app_state, conn_id, client, monkeypatch):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    member = _mk_user(app_state, "member1", "member")
    r = await _create_approval(client, conn_id, _WITH_WHERE, member)
    assert r.status_code == 200
    aid = r.json()["id"]
    r = await client.post(
        f"/api/v1/approvals/{aid}/approve", json={}, headers=_auth(member)
    )
    assert r.status_code == 403


# ---- 创建审批：真实 verdict / 连接名 / approval_id ----


async def test_create_audits_real_verdict_and_conn_name(
    app_state, conn_id, client, monkeypatch
):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    admin = _mk_user(app_state, "boss", "admin")
    cfg = app_state.connections.get(conn_id)
    r = await _create_approval(client, conn_id, _NO_WHERE, admin)
    assert r.status_code == 200
    aid = r.json()["id"]
    entries = [
        e for e in app_state.audit.list(source="approval") if e.get("approval_id") == aid
    ]
    assert entries, "缺转审批审计条目"
    e = entries[0]
    assert e["verdict"] == "block", "无 WHERE UPDATE 创建时必须记录真实 BLOCK 判定"
    assert e["reasons"], "必须记录闸门 reasons"
    assert e["connection"] == cfg.name, "审计 connection 应为连接名而非 id"


async def test_create_valid_update_records_review(
    app_state, conn_id, client, monkeypatch
):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    admin = _mk_user(app_state, "boss2", "admin")
    r = await _create_approval(client, conn_id, _WITH_WHERE, admin)
    aid = r.json()["id"]
    entries = [
        e for e in app_state.audit.list(source="approval") if e.get("approval_id") == aid
    ]
    assert entries[0]["verdict"] == "review"


# ---- 核心验收：批准的无 WHERE UPDATE 被闸门拦 ----


async def test_approved_unconditional_update_blocked_by_gate(
    app_state, conn_id, demo_db, client, monkeypatch
):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    admin = _mk_user(app_state, "boss3", "admin")
    r = await _create_approval(client, conn_id, _NO_WHERE, admin)
    aid = r.json()["id"]
    r = await client.post(f"/api/v1/approvals/{aid}/approve", json={}, headers=_auth(admin))
    assert r.status_code == 403
    # 未执行：价格未变
    chk = await client.post(
        "/api/v1/query",
        json={"connection_id": conn_id, "sql": "SELECT price FROM products WHERE id = 1"},
    )
    assert chk.json()["rows"][0][0] != 9
    # 无放行类审计
    statuses = [e.get("status") for e in app_state.audit.list(source="approval")]
    assert "审批通过" not in statuses
    assert "审批执行完成" not in statuses


async def test_missing_connection_fail_closed(app_state, client, monkeypatch):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    admin = _mk_user(app_state, "boss4", "admin")
    a = app_state.approvals.create("conn-missing", _WITH_WHERE, "someone")
    r = await client.post(
        f"/api/v1/approvals/{a.id}/approve", json={}, headers=_auth(admin)
    )
    assert r.status_code == 404
    execs = [
        e for e in app_state.audit.list(source="approval") if e.get("status") == "审批执行完成"
    ]
    assert not execs, "fail-closed：连接缺失时绝不能执行"


# ---- 合法路径与只读拦截 ----


async def test_valid_update_approved_executes_with_audit_chain(
    app_state, conn_id, client, monkeypatch
):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    admin = _mk_user(app_state, "boss5", "admin")
    r = await _create_approval(client, conn_id, _WITH_WHERE, admin)
    aid = r.json()["id"]
    r = await client.post(f"/api/v1/approvals/{aid}/approve", json={}, headers=_auth(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["affected_rows"] >= 1
    chk = await client.post(
        "/api/v1/query",
        json={"connection_id": conn_id, "sql": "SELECT price FROM products WHERE id = 1"},
    )
    assert chk.json()["rows"][0][0] == 9
    chain = [
        e.get("status")
        for e in app_state.audit.list(source="approval")
        if e.get("approval_id") == aid
    ]
    assert "转审批" in chain and "审批通过" in chain and "审批执行完成" in chain


async def test_readonly_connection_review_write_blocked(
    app_state, demo_db, client, monkeypatch
):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    ro = app_state.connections.create(
        {"name": "ro-demo", "dialect": "sqlite", "file": str(demo_db), "read_only": True}
    )
    admin = _mk_user(app_state, "boss6", "admin")
    r = await _create_approval(client, ro.id, _WITH_WHERE, admin)
    aid = r.json()["id"]
    r = await client.post(f"/api/v1/approvals/{aid}/approve", json={}, headers=_auth(admin))
    assert r.status_code == 403


# ---- 驳回 ----


async def test_reject_flow(app_state, conn_id, client, monkeypatch):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    member = _mk_user(app_state, "member2", "member")
    admin = _mk_user(app_state, "boss7", "admin")
    r = await _create_approval(client, conn_id, _WITH_WHERE, member)
    aid = r.json()["id"]
    r = await client.post(
        f"/api/v1/approvals/{aid}/reject",
        json={"note": "不允许"},
        headers=_auth(admin),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
