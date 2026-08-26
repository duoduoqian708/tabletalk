"""段9 收口：KB 构建 × LLM 记账 端到端不变量。

强制走「真实 LLMGateway（中央拦截器）+ 确定性 MockProvider」路径，验证：
- llm_log / cost_tracker / egress 审计在 KB 构建中全记录（kb-annotation/tags/graph）；
- 一次请求 = 一组记录（逐表注释条数 == 表数）；
- mock 网关也记（"含 mock 全记"）；
- 未授权构建零 values/example 出网/落库。
egress-graph-verify 在有候选对时才会产生（其覆盖见 test_annotator 图谱自检）。
"""
from __future__ import annotations

import json

import app.ai.gateway as gw
from app.ai.cost_tracker import CostTracker
from app.ai.gateway import MockProvider
from app.ai.llm_log import LlmCallLog


class _FakeResponses:
    """patch MockProvider.chat 用的确定性响应：按 prompt 特征返回可解析 JSON。"""

    def __init__(self, schema: dict) -> None:
        self.schema = schema

    async def chat(self, messages, tools=None, ctx=None):
        prompt = messages[0]["content"]
        tbls = [t["name"] for t in self.schema["tables"]]
        if "表画像" in prompt or "领域划分" in prompt:
            if "初版划分" in prompt or "审校" in prompt:
                return type("R", (), {"content": '{"unchanged": true}'})()
            return type("R", (), {"content": json.dumps(
                [{"name": "核心业务", "description": "主表域", "tables": tbls, "reason": "归并"}] )})()
        if "待裁决候选关系" in prompt or ("候选" in prompt and "裁决" in prompt):
            return type("R", (), {"content": '[]'})()
        if "表结构" in prompt and "关系" in prompt:
            return type("R", (), {"content": '[]'})()
        items = []
        for t in self.schema["tables"]:
            items.append({"table": t["name"], "comment": f"{t['name']} 表"})
            for c in self.schema["columns"]:
                if c["table"] == t["name"]:
                    items.append({"table": t["name"], "column": c["name"], "comment": f"{c['name']} 列"})
        return type("R", (), {"content": json.dumps(items, ensure_ascii=False)})()


async def test_kb_build_records_all_llm_calls(app_state, demo_db, conn_id, monkeypatch):
    """KB 构建三阶段所有 LLM 调用进 llm_log + cost + egress；一次请求一组；mock 也记。"""
    from app.core.schema import get_schema

    schema = await get_schema(app_state, conn_id)
    n_tables = len(schema["tables"])
    fake = _FakeResponses(schema)
    data_dir = app_state.env.data_dir

    async def _patched(cls, messages, tools=None):
        return await fake.chat(messages, tools)

    monkeypatch.setattr(gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(MockProvider, "chat", classmethod(_patched))
    res = await app_state.knowledge.build(
        conn_id, schema, {}, include_samples=True, self_check=True,
    )

    assert res["ai_docs_added"] > 0 and res["ai_tags_added"] > 0

    # llm_log：三个 skill 都记，逐表注释一次请求=一组（== 表数）
    calls = LlmCallLog(data_dir).list_calls(conn_id=conn_id, limit=500)
    skills = {c["skill"] for c in calls}
    assert {"kb-annotation", "kb-tags", "kb-graph"} <= skills, f"{sorted(skills)}"
    ann = [c for c in calls if c["skill"] == "kb-annotation"]
    assert len(ann) == n_tables, f"逐表注释应每表一组，got {len(ann)}"
    assert any(c.get("request_json") for c in calls), "请求明文应落库"

    # egress 出网审计（清单先于出网）
    egress = [e for e in app_state.audit.list() if str(e.get("status", "")).startswith("egress")]
    egress_status = {e["status"] for e in egress}
    assert {"egress-annotation", "egress-tags", "egress-tags-selfcheck", "egress-graph-global"} <= egress_status
    assert len([e for e in egress if e["status"] == "egress-annotation"]) == n_tables

    # cost 摘要（skill 维度）
    summary = CostTracker(data_dir).get_summary()
    by_skill = {r["skill"] for r in summary["by_skill"]}
    assert {"kb-annotation", "kb-tags", "kb-graph"} <= by_skill
    assert summary["total_calls"] > 0


async def test_kb_build_unauthorized_zero_instance_data(app_state, demo_db):
    """未授权构建（include_samples=False）→ 注释零 values/example 出网/落库。"""
    from app.core.schema import get_schema

    cfg = app_state.connections.create({"name": "d2", "dialect": "sqlite", "file": str(demo_db)})
    conn = cfg.id
    app_state.connections.set_kb_status(conn, "ready")
    schema = await get_schema(app_state, conn)
    await app_state.knowledge.build(conn, schema, {}, include_samples=False)
    leaked = 0
    for tk in app_state.knowledge._tables.get(conn, {}).values():
        for ci in tk.columns.values():
            if ci.values or ci.example:
                leaked += 1
    assert leaked == 0