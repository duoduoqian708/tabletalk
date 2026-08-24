"""WS7 T7.4 验收：地板不可关；勾选审计；成员不能恢复管理员禁用项。"""
from __future__ import annotations

import os


async def test_floor_cannot_disable(client):
    r = await client.put("/api/v1/skills/query", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    r = await client.put("/api/v1/skills/refusal", json={"enabled": False})
    assert r.json()["enabled"] is True
    # 非地板可关
    r = await client.put("/api/v1/skills/report", json={"enabled": False})
    assert r.json()["enabled"] is False
    await client.put("/api/v1/skills/report", json={"enabled": True})


async def test_skill_toggle_audits(app_state, client):
    # 切 report 开关应产生 source=settings 审计
    before = len([e for e in app_state.audit.list(source="settings") if e.get("skill_id") == "report"])
    await client.put("/api/v1/skills/report", json={"enabled": False})
    await client.put("/api/v1/skills/report", json={"enabled": True})
    entries = [e for e in app_state.audit.list(source="settings") if e.get("skill_id") == "report"]
    assert len(entries) >= before + 2
    assert any(e.get("enabled") is False for e in entries)
    assert any(e.get("enabled") is True for e in entries)


async def test_member_cannot_restore_admin_disabled(app_state, client, monkeypatch):
    monkeypatch.setenv("TABLETALK_AUTH_MODE", "team")
    # admin 禁用
    admin = app_state.auth.create_user("adm1", "pass123", "admin")
    member = app_state.auth.create_user("mem1", "pass123", "member")
    admin_tok = app_state.auth.issue_token(admin)
    mem_tok = app_state.auth.issue_token(member)
    # admin 禁用 report
    r = await client.put("/api/v1/skills/report", json={"enabled": False}, headers={"X-TableTalk-Token": admin_tok, "Authorization": f"Bearer {admin_tok}"})
    assert r.json()["enabled"] is False
    # member 试图恢复 -> 403
    r = await client.put("/api/v1/skills/report", json={"enabled": True}, headers={"X-TableTalk-Token": mem_tok, "Authorization": f"Bearer {mem_tok}"})
    assert r.status_code == 403
    # admin 恢复 -> 成功
    r = await client.put("/api/v1/skills/report", json={"enabled": True}, headers={"X-TableTalk-Token": admin_tok, "Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
