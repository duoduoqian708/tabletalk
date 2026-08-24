"""P1/P2/P3 修复回归测试（追加到测试套件）。"""
from __future__ import annotations

import sqlite3

from app.ai.tools import execute_tool


async def test_kb_build_filters_sensitive(client, app_state, demo_db):
    """P1：敏感表不进知识库（构建 → overview/检索均不可见）。"""
    from tests.api.test_api import _build_and_wait

    conn = sqlite3.connect(str(demo_db))
    conn.execute("DROP TABLE IF EXISTS secret_payroll")
    conn.execute("CREATE TABLE secret_payroll (id INTEGER PRIMARY KEY, salary TEXT)")
    conn.commit()
    conn.close()

    c = app_state.connections.create({
        "name": "sens", "dialect": "sqlite", "file": str(demo_db),
        "sensitive": ["secret_*"],
    })
    await _build_and_wait(client, c.id)
    await client.post(f"/api/v1/knowledge/{c.id}/confirm-all")
    ov = (await client.get(f"/api/v1/knowledge/{c.id}/overview")).json()
    assert not any(t["name"] == "secret_payroll" for t in ov["tables"])
    docs = (await client.get(f"/api/v1/knowledge/{c.id}/docs")).json()
    assert not any(d["table"] == "secret_payroll" for d in docs["docs"])
    r = (await client.get(f"/api/v1/knowledge/{c.id}/retrieve", params={"q": "secret_payroll"})).json()
    assert not any(x["table"] == "secret_payroll" for x in r["cards"])


async def test_run_dml_readonly_conn_blocked(app_state, demo_db):
    """P3：只读连接上 AI run_dml 直接 BLOCK（不再给需确认黄卡）。"""
    ro = app_state.connections.create(
        {"name": "ro", "dialect": "sqlite", "file": str(demo_db), "read_only": True}
    )
    out = await execute_tool(
        app_state, "run_dml",
        {"sql": "UPDATE products SET price = 1 WHERE id = 1"},
        ro.id,
    )
    assert out.card["verdict"] == "block"
    assert "只读" in out.card["reason"]


async def test_skill_system_prompt_injected(app_state, conn_id, monkeypatch):
    """P2：自定义技能的 system_prompt 注入对话（跟随 skill_id）。"""
    from app.ai.skills.registry import register_custom
    from app.ai.skills.skill import Skill
    from app.ai.loop import chat_stream
    from app.ai.dto import ChatRequest

    skill = register_custom(Skill(
        id="audit_x", name="审计专用", description="审计",
        tools=["get_schema"], system_prompt="技能指令：只允许查询，禁止写。", triggers=["审计"],
    ))
    captured: dict = {}

    class _FakeProvider:
        async def chat_stream(self, messages, tools=None):
            captured["messages"] = messages
            captured["tools"] = tools
            if False:
                yield  # 空 async generator：无工具调用 → 直接 done

    monkeypatch.setattr("app.ai.gateway.build_provider", lambda *a, **k: _FakeProvider())
    req = ChatRequest(connection_id=conn_id, skill_id=skill.id,
                      messages=[{"role": "user", "content": "审计一下"}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    assert any(ev["type"] == "done" for ev in events)
    sys_msgs = [m["content"] for m in captured.get("messages", []) if m["role"] == "system"]
    assert any("技能指令：只允许查询" in c for c in sys_msgs)
    # 工具集按技能过滤：只暴露 get_schema
    names = [t["function"]["name"] for t in captured.get("tools", [])]
    assert names == ["get_schema"]
