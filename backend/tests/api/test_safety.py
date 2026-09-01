"""安全闸门规则目录 + gate_rules 配置契约（读写闭环 + 变更审计 + 手动路径生效）。"""
from __future__ import annotations


async def test_safety_rules_catalog(client):
    r = await client.get("/api/v1/safety/rules")
    assert r.status_code == 200
    body = r.json()
    ids = {x["id"] for x in body["rules"]}
    assert {"parse-failure", "dml-confirm", "dml-no-where", "read-no-limit",
            "multi-statement", "ddl-ai", "read-allow"} <= ids
    dml = next(x for x in body["rules"] if x["id"] == "dml-confirm")
    assert dml["default_verdict"] == "review" and dml["tier"] == "dml" and dml["floor"] is False
    assert dml["override"] is None
    pf = next(x for x in body["rules"] if x["id"] == "parse-failure")
    assert pf["floor"] is True and pf["default_verdict"] == "review"
    assert "policy" in body and body["policy"] is not None


async def test_put_gate_rules_valid_and_invalid_cleaned(client):
    r = await client.put("/api/v1/settings", json={
        "gate_rules": {"dml-confirm": "block", "read-no-limit": "banana",
                       "parse-failure": "allow", "not-a-rule": "block", "dml-no-where": True},
    })
    assert r.status_code == 200
    gr = r.json()["gate_rules"]
    assert gr.get("dml-confirm") == "block"
    assert "read-no-limit" not in gr   # 非法 value
    assert "parse-failure" not in gr   # floor 放宽
    assert "not-a-rule" not in gr      # 未知规则
    assert "dml-no-where" not in gr    # bool 历史语义丢弃
    # 清回默认，避免污染后续用例
    await client.put("/api/v1/settings", json={"gate_rules": {}})


async def test_gate_rules_change_is_audited(client):
    r = await client.put("/api/v1/settings", json={"gate_rules": {"dml-confirm": "block"}})
    assert r.status_code == 200
    ar = await client.get("/api/v1/audit")
    entries = ar.json()["entries"]
    assert any(e.get("source") == "settings" and "gate_rules" in e.get("sql", "") for e in entries)
    await client.put("/api/v1/settings", json={"gate_rules": {}})


async def test_override_affects_manual_query_path(client, conn_id):
    # 手动查询走 assess_configured：dml-confirm 收严为 block 后，UPDATE 直接拦截（原为 review）
    await client.put("/api/v1/settings", json={"gate_rules": {"dml-confirm": "block"}})
    q = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": "UPDATE orders SET status='paid' WHERE id=1", "origin": "manual",
    })
    assert q.status_code == 200
    body = q.json()
    assert body["verdict"] == "block"
    assert body["reasons"][0]["rule_id"] == "dml-confirm"
    assert "配置收严" in body["reasons"][0]["message"]
    # 恢复默认 → 回到 review
    await client.put("/api/v1/settings", json={"gate_rules": {}})
    q2 = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": "UPDATE orders SET status='paid' WHERE id=1", "origin": "manual",
    })
    assert q2.json()["verdict"] == "review"


async def test_policy_table_rule_blocks_manual_query(client, conn_id):
    # 表级策略对称生效：orders=block → UPDATE orders 拦截
    await client.put("/api/v1/settings", json={"policy": {"table_rules": {"orders": "block"}}})
    q = await client.post("/api/v1/query", json={
        "connection_id": conn_id, "sql": "UPDATE orders SET status='paid' WHERE id=1", "origin": "manual",
    })
    assert q.status_code == 200
    body = q.json()
    assert body["verdict"] == "block"
    assert any(r.get("rule_id") == "policy-table-orders" for r in body["reasons"])
    # 清回
    await client.put("/api/v1/settings", json={"policy": {"table_rules": {}}})


async def test_policy_blocks_approval_execution(client, conn_id):
    # 审批流防绕过（fail-closed）：表策略 orders=block，create→approve 在批准路径被闸门拦
    await client.put("/api/v1/settings", json={"policy": {"table_rules": {"orders": "block"}}})
    c = await client.post("/api/v1/approvals", json={
        "connection_id": conn_id, "sql": "UPDATE orders SET status='paid' WHERE id=1",
    })
    assert c.status_code == 200, c.text
    aid = c.json()["id"]
    a = await client.post(f"/api/v1/approvals/{aid}/approve", json={"note": "t"})
    assert a.status_code == 403
    await client.put("/api/v1/settings", json={"policy": {"table_rules": {}}})