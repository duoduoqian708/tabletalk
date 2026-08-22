"""技能广场 API：清单（含工具目录）/ 新建 / 更新 / 删除 + 组合安全校验。"""

from __future__ import annotations

import json


async def test_skills_list(client):
    r = await client.get("/api/v1/skills")
    assert r.status_code == 200
    body = r.json()
    ids = [s["id"] for s in body["skills"]]
    assert "query" in ids and "report" in ids
    tool_names = {t["name"] for t in body["tools"]}
    assert "run_query" in tool_names and "draft_ddl" in tool_names
    assert not any(t["name"] == "run_ddl" for t in body["tools"])  # DDL 执行工具物理不存在


async def test_skills_create_update_delete(client):
    # 新建
    r = await client.post("/api/v1/skills", json={
        "name": "对账助手", "description": "核对订单回款", "read_only": True,
        "tools": ["run_query", "get_schema"], "triggers": ["对账", "回款"],
        "system_prompt": "你是一个对账专家，只读。",
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert sid.startswith("sk_")
    # 列表可见
    r = await client.get("/api/v1/skills")
    assert any(s["id"] == sid for s in r.json()["skills"])
    # 更新：禁用 + 组合
    r = await client.put(f"/api/v1/skills/{sid}", json={"enabled": False, "tools": ["get_schema"]})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["tools"] == ["get_schema"]
    # 删除
    r = await client.delete(f"/api/v1/skills/{sid}")
    assert r.status_code == 200
    r = await client.get("/api/v1/skills")
    assert not any(s["id"] == sid for s in r.json()["skills"])


async def test_skills_validation_and_builtin_protection(client):
    # 未注册工具 → 422
    r = await client.post("/api/v1/skills", json={"name": "x", "tools": ["hack_db"]})
    assert r.status_code == 422
    # 只读技能挂写工具 → 422
    r = await client.post("/api/v1/skills", json={"name": "x", "tools": ["run_dml"], "read_only": True})
    assert r.status_code == 422
    # 内置技能不可删
    r = await client.delete("/api/v1/skills/query")
    assert r.status_code == 403
    # 地板技能不可禁用（T1.2/08§4.5）：query 保持启用
    r = await client.put("/api/v1/skills/query", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    # 非地板技能可禁用
    r = await client.put("/api/v1/skills/report", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    # 恢复，避免影响其他测试
    await client.put("/api/v1/skills/report", json={"enabled": True})
