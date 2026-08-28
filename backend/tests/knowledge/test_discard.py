"""放弃（discard）语义测试：撤下本轮全部草案、保留历史已确认内容、状态流转与审计留痕。

- store 层：draft→none（文本保留便于下次重建对照）、draft 标签移除解绑、LLM 边删除+墓碑；
- API 层：kb_status 流转（无 confirmed→none / 有 confirmed→ready）
  + 审计 origin=kb_build / status=discarded / source=manual。
"""
from __future__ import annotations

from app.knowledge.store import KnowledgeBase


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 2},
            {"name": "customers", "kind": "table", "comment": "客户表", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "customer_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "orders", "name": "status", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
            {"table": "customers", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "customers", "name": "name", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"},
        ],
    }


# ───────────────────────── store 层 ─────────────────────────

async def test_rebuild_then_discard_keeps_confirmed_tags(app_state):
    """有历史版本（confirmed 标签）→ 重建 → 放弃：旧标签库与表绑定原封不动。

    回归：clear_tags 曾全清标签库（含 confirmed），重建后放弃把历史标签也丢了。
    """
    st = app_state
    conn = "c-rebuild-tags"
    kb = st.knowledge
    await kb.build(conn, _schema(), enable_ai_annotation=False)
    # 上一轮历史：confirmed 标签 + 表绑定
    assert kb.create_tag(conn, "交易域", "", "#e25050") is True
    kb.assign_table_tags(conn, "orders", ["交易域"])
    assert kb.confirm_tag(conn, "交易域") is True
    # 重建（mock AI 重新划分：新 draft 标签 + 绑定）
    await kb.build(conn, _schema())
    assert any(tg["status"] == "draft" for tg in kb.tags(conn)["library"]), "重建应产生新 draft 标签"
    # 放弃本轮草案 → 旧 confirmed 标签与绑定恢复原样
    await kb.discard_drafts(conn)
    tags = kb.tags(conn)
    assert any(tg["name"] == "交易域" and tg["status"] == "confirmed" for tg in tags["library"])
    assert "交易域" in tags["tables"].get("orders", [])
    assert kb.has_confirmed_content(conn) is True


async def test_first_round_discard_withdraws_all_drafts(tmp_path):
    """首轮放弃：列/表注释 draft→none、标签移除解绑、LLM 边删除；全库无 confirmed 内容。

    撤下仅回退状态，注释/取值/示例文本保留（便于下次重建对照）。
    """
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表草案"},
        {"table": "orders", "column": "status", "comment": "订单状态草案", "values": "P=待付款", "example": "P"},
        {"table": "customers", "column": "name", "comment": "客户姓名草案"},
    ])
    kb.upsert_tags("c1", [{"name": "交易域", "description": ""}])
    kb.assign_table_tags("c1", "orders", ["交易域"])
    kb._llm_graph_edges["c1"] = [{
        "from_table": "orders", "from_col": "customer_id",
        "to_table": "customers", "to_col": "id",
        "cardinality": "n:1", "reason": "LLM 推测", "status": "draft",
    }]

    counts = await kb.discard_drafts("c1")
    assert counts == {"columns": 2, "tables": 1, "tags": 1, "edges": 1}
    # 草案全撤：pending 清零
    assert kb.pending_counts("c1") == {"draft_docs": 0, "draft_tags": 0, "llm_graph_draft": 0}
    tk = kb._tables["c1"]["orders"]
    assert tk.status == "none"
    assert tk.columns["status"].status == "none"
    # 文本保留不清空
    assert tk.comment == "订单主表草案"
    assert tk.columns["status"].comment == "订单状态草案"
    assert tk.columns["status"].values == "P=待付款"
    assert tk.columns["status"].example == "P"
    # 标签移除并解绑（对齐 reject_tag）
    tags = kb.tags("c1")
    assert not any(tg["name"] == "交易域" for tg in tags["library"])
    assert all("交易域" not in names for names in tags["tables"].values())
    # LLM 边撤下 + 墓碑记录
    assert kb.llm_graph_edges("c1") == []
    keys = {KnowledgeBase._llm_edge_key(t) for t in kb._llm_edge_tombstones.get("c1", [])}
    assert KnowledgeBase._llm_edge_key({
        "from_table": "orders", "from_col": "customer_id",
        "to_table": "customers", "to_col": "id",
    }) in keys
    # 首轮无历史 → 无 confirmed 内容（API 层据此置 kb_status=none）
    assert kb.has_confirmed_content("c1") is False


async def test_discard_keeps_confirmed_history(tmp_path):
    """有历史确认时放弃：confirmed 原样保留，剩余草案撤下，判定存在 confirmed 内容。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [
        {"table": "orders", "column": "status", "comment": "订单状态草案"},
        {"table": "customers", "column": "name", "comment": "客户姓名草案"},
    ])
    # 确认一列（模拟上一轮历史成果）
    assert await kb.confirm("c1", "orders", "status") == 1

    counts = await kb.discard_drafts("c1")
    assert counts == {"columns": 1, "tables": 0, "tags": 0, "edges": 0}
    ci = kb._tables["c1"]["orders"].columns["status"]
    assert ci.status == "confirmed" and ci.comment == "订单状态草案"
    assert kb._tables["c1"]["customers"].columns["name"].status == "none"
    assert kb.has_confirmed_content("c1") is True


async def test_discard_tombstones_llm_edges_no_revival_on_rebuild(tmp_path):
    """墓碑生效：放弃的 LLM 边在重建后被标记 previously_rejected，不复活为活跃草案。"""
    kb = KnowledgeBase(tmp_path)
    schema = _schema()
    await kb.build("c1", schema)  # mock AI：FK 推导出确定性 llm draft 边
    first_edges = kb.llm_graph_edges("c1")
    assert first_edges, "mock 构建应产生 llm draft 边"

    counts = await kb.discard_drafts("c1")
    assert counts["edges"] == len(first_edges)
    assert kb.llm_graph_edges("c1") == []

    await kb.build("c1", schema)  # 同 schema 重建 → mock 再次产出同一批边
    edges = kb.llm_graph_edges("c1")
    assert edges, "重建应再次发现关系"
    assert all(e.get("status") == "previously_rejected" for e in edges), \
        "墓碑中的边不得复活为活跃草案"


# ───────────────────────── API 层 ─────────────────────────

async def test_discard_api_first_round_none_and_audited(client, app_state, conn_id):
    """首轮放弃端到端：kb_status→none、草案全撤、审计留痕（origin=kb_build/status=discarded）。"""
    from tests.api.test_api import _build_and_wait

    await _build_and_wait(client, conn_id)
    r = await client.post(f"/api/v1/knowledge/{conn_id}/discard")
    assert r.status_code == 200
    body = r.json()
    assert body["kb_status"] == "none"
    assert body["discarded"]["columns"] > 0 or body["discarded"]["tables"] > 0
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["kb_status"] == "none"
    ov = (await client.get(f"/api/v1/knowledge/{conn_id}/overview")).json()
    assert all(t["comment_status"] != "draft" for t in ov["tables"])
    assert all(c["status"] != "draft" for t in ov["tables"] for c in t["columns"])
    entries = [e for e in app_state.audit.list(connection=conn_id, origin="kb_build")
               if e["status"] == "discarded"]
    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "manual"
    assert e["verdict"] == "allow"
    assert e["tier"] == "read"
    # kwargs 平铺：extra 记草案计数，与响应一致（与构建发起对称）
    assert e["discarded"] == body["discarded"]


async def test_discard_api_ready_when_history_exists(client, conn_id):
    """存在已确认内容时放弃：旧 confirmed 保留，kb_status 回 ready，新草案撤下。"""
    from tests.api.test_api import _build_and_wait

    await _build_and_wait(client, conn_id)
    ov = (await client.get(f"/api/v1/knowledge/{conn_id}/overview")).json()
    tbl = next(t for t in ov["tables"] if any(c["status"] == "draft" for c in t["columns"]))
    col = next(c for c in tbl["columns"] if c["status"] == "draft")
    r = await client.post(f"/api/v1/knowledge/{conn_id}/confirm",
                          json={"table": tbl["name"], "column": col["name"]})
    assert r.json()["confirmed"] >= 1

    r = await client.post(f"/api/v1/knowledge/{conn_id}/discard")
    body = r.json()
    assert body["kb_status"] == "ready"

    ov2 = (await client.get(f"/api/v1/knowledge/{conn_id}/overview")).json()
    t2 = next(t for t in ov2["tables"] if t["name"] == tbl["name"])
    c2 = next(c for c in t2["columns"] if c["name"] == col["name"])
    assert c2["status"] == "confirmed" and c2["comment"] == col["comment"]
    assert all(cc["status"] != "draft" for t in ov2["tables"] for cc in t["columns"])
