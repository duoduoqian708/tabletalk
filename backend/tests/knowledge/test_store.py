"""知识库 v3 测试：构建（图谱/向量）/ 检索排序 / AI 草案 / 人工确认 / 持久化。"""
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


def _samples() -> dict:
    return {
        "orders": {"id": [1, 2, 3], "customer_id": [1, 2, 3], "status": ["paid", "pending"]},
        "customers": {"id": [1, 2, 3, 4], "name": ["陈嘉禾", "林可欣"]},
    }


async def test_build_and_keyword_retrieve(tmp_path):
    kb = KnowledgeBase(tmp_path)
    stats = await kb.build("c1", _schema())
    assert stats["docs"] > 0
    cards = await kb.retrieve("c1", query="订单", k=10)
    assert any(c.table == "orders" for c in cards)


async def test_graph_includes_fk_only(tmp_path):
    """设计文档：图谱仅包含 FK 边（overlap 边已移除，仅用于内部检索）。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    kb.confirm_graph_edges("c1")
    edges = kb.graph("c1")["edges"]
    assert any(e["kind"] == "fk" for e in edges)
    # 无 overlap 边（设计文档：图谱仅 FK 边用于可视化）
    assert not any(e["kind"] == "overlap" for e in edges)


async def test_fk_graph_expansion_surfaces_neighbors(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    cards = await kb.retrieve("c1", query="订单表", k=10)
    tables = {c.table for c in cards}
    assert "orders" in tables
    assert "customers" in tables


async def test_target_table_still_ranks_first(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    cards = await kb.retrieve("c1", query="订单", table="customers", k=10)
    assert cards[0].table == "customers"


async def test_ai_draft_then_confirm_new_model(tmp_path):
    """AI 提案进 proposed_*（当前值不动），确认后提升为当前。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)  # 本例测手动草案机制，关掉 AI 阶段保证确定性
    n = kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "订单状态草稿"}])
    assert n == 1
    tk = kb._tables["c1"]["orders"]
    ci = tk.columns["status"]
    # 2026-09 修订：提案在 proposed_*，当前 comment/status 不动
    assert ci.proposed_comment == "订单状态草稿" and ci.status == "none"
    # 确认单列 -> 提案提升
    assert await kb.confirm("c1", "orders", "status") == 1
    assert ci.status == "confirmed" and ci.comment == "订单状态草稿"
    # 再注释写新提案，不覆盖已确认当前值
    kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "覆盖尝试"}])
    assert ci.comment == "订单状态草稿" and ci.proposed_comment == "覆盖尝试"


async def test_table_level_annotation_and_confirm_all(tmp_path):
    """表级注释草案 → confirm(table) 确认表+全部列；confirm_all 全库确认。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)  # 本例测手动草案机制，关掉 AI 阶段保证确定性
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表草案"},
        {"table": "orders", "column": "id", "comment": "订单ID"},
        {"table": "customers", "column": "name", "comment": "客户姓名"},
    ])
    assert await kb.confirm("c1", "orders") == 2  # 表注释 + id 列
    tk = kb._tables["c1"]["orders"]
    assert tk.status == "confirmed" and tk.columns["id"].status == "confirmed"
    assert kb.pending_counts("c1")["draft_docs"] == 1
    counts = await kb.confirm_all("c1")
    assert counts["docs"] == 1 and counts["tags"] >= 0
    assert all(t.status == "confirmed" for t in kb._tables["c1"].values())
    assert kb.pending_counts("c1")["draft_docs"] == 0


async def test_reject_clears_annotation(tmp_path):
    """拒绝（✕）：整条 AI 注释撤下回 none；values/example 一并清空。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    kb.annotate_drafts("c1", [{
        "table": "orders", "column": "status",
        "comment": "会被撤下的注释", "values": "P=待付款", "example": "P",
    }])
    assert await kb.reject("c1", "orders", "status") == 1
    ci = kb._tables["c1"]["orders"].columns["status"]
    assert ci.status == "none" and ci.comment == "" and ci.values == "" and ci.example == ""
    # 表级撤下
    kb.annotate_drafts("c1", [{"table": "orders", "comment": "表注释"}])
    assert await kb.reject_comment("c1", "orders") == 1
    assert kb._tables["c1"]["orders"].comment == ""


async def test_artifact_persists_across_instances(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    kb.confirm_graph_edges("c1")
    kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "订单状态草稿"}])
    await kb.confirm("c1", "orders", "status")

    kb2 = KnowledgeBase(tmp_path)
    assert kb2.is_built("c1")  # artifact 存在 → 无需重采样即可检索
    cards = await kb2.retrieve("c1", query="订单", k=10)
    assert any(c.table == "orders" for c in cards)
    # 确认状态在 artifact（v2 表级知识）中保留
    ov = kb2.overview("c1")
    tbl = next(t for t in ov["tables"] if t["name"] == "orders")
    col = next(c for c in tbl["columns"] if c["name"] == "status")
    assert col["status"] == "confirmed" and col["comment"] == "订单状态草稿"
    # 图谱 FK 边保留了
    assert any(e["kind"] == "fk" for e in kb2.graph("c1")["edges"])


async def test_tag_lifecycle(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    assert kb.upsert_tags("c1", [{"name": "订单", "description": "订单领域"}]) == 1
    kb.assign_table_tags("c1", "orders", ["订单"])
    # draft → 确认 → 可路由
    assert kb.confirmed_tags("c1") == []
    assert kb.confirm_tag("c1", "订单") is True
    assert kb.confirmed_tags("c1") == ["订单"]
    # 拒绝标签：从库移除 + 解除绑定
    kb.upsert_tags("c1", [{"name": "废标"}])
    kb.assign_table_tags("c1", "orders", ["订单", "废标"])
    assert kb.reject_tag("c1", "废标") is True
    assert "废标" not in {t["name"] for t in kb.tags("c1")["library"]}
    assert "废标" not in kb.tags("c1")["tables"]["orders"]


async def test_route_tables_fk_expansion(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())  # orders FK→customers
    kb.confirm_graph_edges("c1")
    kb.upsert_tags("c1", [{"name": "订单"}])
    kb.confirm_tag("c1", "订单")
    kb.assign_table_tags("c1", "orders", ["订单"])
    r = kb.route_tables("c1", ["订单"], hops=2)
    assert "orders" in r["tables"]
    assert "customers" in r["tables"]  # FK 2 步扩展
    # draft 标签不进路由（未确认不参与）
    kb.upsert_tags("c1", [{"name": "草稿标签"}])
    kb.assign_table_tags("c1", "customers", ["草稿标签"])
    r2 = kb.route_tables("c1", ["草稿标签"], hops=0)
    assert "customers" not in r2["tables"]


async def test_route_tables_respects_table_tags(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    kb.upsert_tags("c1", [{"name": "订单"}, {"name": "客户"}])
    kb.confirm_tag("c1", "订单")
    kb.confirm_tag("c1", "客户")
    kb.assign_table_tags("c1", "orders", ["订单"])
    kb.assign_table_tags("c1", "customers", ["客户"])
    r = kb.route_tables("c1", ["订单"], hops=0)
    # hops=0：只打该标签的表，不扩展
    assert r["tables"] == ["orders"]


async def test_user_note_persists_across_instances(tmp_path):
    """用户手写笔记跨实例保留（v3：笔记不进向量检索，检索统一走表知识卡）。"""
    kb = KnowledgeBase(tmp_path)
    kb.annotate("c1", "orders", "status", "pending 表示待支付")
    kb2 = KnowledgeBase(tmp_path)
    docs = kb2.list_docs("c1")
    assert any(d.body == "pending 表示待支付" for d in docs)


async def test_empty_query_returns_all(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    cards = await kb.retrieve("c1", query="", k=100)
    assert {c.table for c in cards} == {"orders", "customers"}


async def test_embedding_api_selection(tmp_path):
    from app.knowledge.embedding import make_embedder

    e = make_embedder("hash")
    v = await e.embed("订单")
    assert len(v) > 0
    e2 = make_embedder("api", base_url="http://localhost:9", model="bge-m3")
    # 不可达端点 → 抛错，调用方已兜底
    try:
        await e2.embed("订单")
        raise AssertionError("应抛错")
    except Exception:
        pass


async def test_overview_without_retrieve_loads_artifact(tmp_path):
    """回归：重启后（新实例）直接 overview 不经过 retrieve 也能恢复工件——表列表不再为空。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    kb.confirm_graph_edges("c1")
    kb2 = KnowledgeBase(tmp_path)
    assert kb2.is_built("c1")
    kb2.ensure_loaded("c1")
    tables = kb2.overview("c1")["tables"]
    assert any(t["name"] == "orders" for t in tables)
    assert any(e["kind"] == "fk" for e in kb2.graph("c1")["edges"])


# ---------- T2：一表一 chunk 向量合一（spec §4） ----------


async def test_synthesize_text_confirmed_priority(tmp_path):
    """合成文本：confirmed 内容入文（可选值/示例/注释），draft 仅计 payload.draft_count。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表草案"},
        {"table": "orders", "column": "status", "comment": "订单状态",
         "values": "P=待付款；S=已发货", "example": "P"},
    ])
    tk = kb._tables["c1"]["orders"]
    text = kb._synthesize_table_text("c1", tk)
    # draft 内容不入文；结构壳（字段名/主键/类型）在
    assert "订单状态" not in text and "待付款" not in text and "订单主表草案" not in text
    assert "id" in text and "主键" in text and "customer_id" in text
    payload = kb._table_payload("c1", tk)
    assert payload["draft_count"] == 2 and payload["ddl"] == tk.ddl
    # 确认后：confirmed 内容入文，draft_count 归零
    await kb.confirm("c1", "orders")
    text2 = kb._synthesize_table_text("c1", kb._tables["c1"]["orders"])
    assert text2.startswith("orders，订单主表草案。字段有：")
    assert "订单状态" in text2 and "P=待付款；S=已发货" in text2 and "示例为P" in text2
    assert kb._table_payload("c1", kb._tables["c1"]["orders"])["draft_count"] == 0


async def test_table_payload_shape(tmp_path):
    """payload = {ddl, tags, layout, draft_count, updated_at}；layout 仅透传。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.upsert_tags("c1", [{"name": "订单"}])
    kb.assign_table_tags("c1", "orders", ["订单"])
    kb._tables["c1"]["orders"].layout = {"x": 10, "y": 20}
    payload = kb._table_payload("c1", kb._tables["c1"]["orders"])
    assert set(payload) == {"ddl", "tags", "layout", "draft_count", "updated_at"}
    assert payload["tags"] == ["订单"]
    assert payload["layout"] == {"x": 10, "y": 20}
    assert payload["draft_count"] == 0
    assert payload["ddl"].startswith("CREATE TABLE")


async def test_retrieve_returns_table_cards(tmp_path):
    """retrieve 返回表知识卡（text/payload/score），一表一卡；to_context 输出【知识库】表卡段。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表"},
        {"table": "orders", "column": "status", "comment": "订单状态", "values": "P=待付款", "example": "P"},
    ])
    await kb.confirm("c1", "orders")
    cards = await kb.retrieve("c1", query="订单", k=10)
    assert cards and cards[0].table == "orders"
    assert "订单状态" in cards[0].text and "P=待付款" in cards[0].text
    assert cards[0].payload["draft_count"] == 0
    assert cards[0].payload["ddl"] and "updated_at" in cards[0].payload
    assert cards[0].score >= 0
    ctx = await kb.to_context("c1", query="订单", k=10)
    assert "【知识库】" in ctx and "- [表] orders，订单主表。字段有：" in ctx
    assert "AI 草案" not in ctx  # 全确认 → 无草案标记


async def test_vstore_single_table_system(tmp_path):
    """一表一 chunk（确认后建立）：构建期不向量化（确认前 draft 不入文），
    确认全部后才恰 N 表条 chunk，collection=table，payload 进 chunk。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples(), enable_ai_annotation=False)
    # 构建期不预嵌：确认前 vstore 无任何 table chunk（向量延迟到人工确认后）
    assert len(kb._vector_store("c1")) == 0
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表"},
        {"table": "customers", "comment": "客户主表"},
    ])
    await kb.confirm_all("c1")
    vs = kb._vector_store("c1")
    assert len(vs) == 2
    chunk = vs._chunks["tbl-orders"]
    assert chunk.collection == "table"
    assert chunk.metadata == {"table": "orders"}
    assert chunk.payload["ddl"] and "tags" in chunk.payload and "layout" in chunk.payload
    assert chunk.text.startswith("orders") and "字段有：" in chunk.text


# ---------- T2：边 v2（字段级端点 + 基数） ----------


async def test_fk_edge_v2_direction_and_cardinality(tmp_path):
    """FK 边 v2：子表=from（多侧）→ 父表=to；FK 兼 PK→1:1 否则 n:1；reason 记录推断依据。"""
    schema = _schema()
    schema["tables"].append({"name": "user_profiles", "kind": "table", "comment": "", "column_count": 1})
    schema["columns"].append({"table": "user_profiles", "name": "id", "type": "int",
                              "nullable": True, "pk": True, "fk": True, "default": None, "comment": ""})
    schema["foreign_keys"].append(
        {"table": "user_profiles", "column": "id", "ref_table": "customers", "ref_column": "id"})
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", schema, enable_ai_annotation=False)
    kb.confirm_graph_edges("c1")
    edges = kb.graph("c1")["edges"]
    by_field = {(e["from"], e["from_col"]): e for e in edges}
    e_n1 = by_field[("orders", "customer_id")]
    assert e_n1["to"] == "customers" and e_n1["to_col"] == "id"
    assert e_n1["cardinality"] == "n:1"
    assert "FK 约束" in e_n1["reason"]
    e_11 = by_field[("user_profiles", "id")]
    assert e_11["cardinality"] == "1:1"


async def test_add_graph_edge_cardinality_and_field_dedup(tmp_path):
    """手绘边：cardinality 校验（默认 n:1）、字段级去重、reason=人工连线。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    e = kb.add_graph_edge("c1", "orders", "customers", "user",
                          frm_col="customer_id", to_col="id")
    assert e["cardinality"] == "n:1" and e["reason"] == "人工连线"
    # 同字段对去重（返回同一条）；不同字段对可并存
    e2 = kb.add_graph_edge("c1", "orders", "customers", "user",
                           frm_col="customer_id", to_col="id")
    assert e2 is e
    e3 = kb.add_graph_edge("c1", "orders", "customers", "user",
                           frm_col="status", to_col="name", cardinality="1:1")
    assert e3["cardinality"] == "1:1"
    assert len([x for x in kb.graph("c1")["edges"] if x["kind"] == "user"]) == 2
    try:
        kb.add_graph_edge("c1", "orders", "customers", "user", cardinality="m:n")
        raise AssertionError("非法基数应抛错")
    except ValueError:
        pass


# ---------- T5：2D 图布局持久化（spec §4 payload.layout / §6 拖拽写回） ----------


async def test_set_layout_roundtrip_and_overview(tmp_path):
    """set_layout 写入已知表坐标 → overview.graph.layout 回读；未知表/坏点忽略。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    n = kb.set_layout("c1", {
        "orders": {"x": 120.5, "y": 80},
        "customers": {"x": 300, "y": 40},
        "ghost_table": {"x": 1, "y": 1},   # 未知表忽略
        "orders_bad": None,                 # 非法条目忽略（不存在的表）
    })
    assert n == 2
    lay = kb.overview("c1")["graph"]["layout"]
    assert lay["orders"] == {"x": 120.5, "y": 80}
    assert lay["customers"] == {"x": 300, "y": 40}
    assert "ghost_table" not in lay
    # 坐标进 chunk payload.layout（spec §4）
    tk = kb._tables["c1"]["orders"]
    assert kb._table_payload("c1", tk)["layout"] == {"x": 120.5, "y": 80}


async def test_layout_persists_across_instances_without_reembed(tmp_path):
    """布局落盘跨实例保留；写入不动嵌入指纹（不触发重嵌）。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples(), enable_ai_annotation=False)
    fp = kb._artifact_fingerprint.get("c1", "")
    assert kb.set_layout("c1", {"orders": {"x": 42, "y": 7}}) == 1
    assert kb._artifact_fingerprint.get("c1", "") == fp  # 指纹未变
    assert await kb.reembed_if_needed("c1") is False     # 不触发重嵌

    kb2 = KnowledgeBase(tmp_path)
    kb2.ensure_loaded("c1")
    lay = kb2.overview("c1")["graph"]["layout"]
    assert lay["orders"] == {"x": 42, "y": 7}


# ---------- 修复回归：确认/撤下后受影响表即时重嵌 ----------


async def test_confirm_reembeds_chunk_text_and_vector(tmp_path):
    """向量化延迟到确认：确认前无 table chunk（构建期不预嵌）；确认后富知识
    （业务注释/可选值/示例）进 chunk 文本，一表一 chunk 建立。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表"},
        {"table": "orders", "column": "status", "comment": "订单状态",
         "values": "P=待付款；S=已发货", "example": "P"},
    ])
    # 确认前：草案不入文，且构建期未预嵌 → vstore 无该表 chunk
    assert "tbl-orders" not in kb._vector_store("c1")._chunks

    assert await kb.confirm("c1", "orders") == 2

    after = kb._vector_store("c1")._chunks["tbl-orders"]
    assert "订单主表" in after.text   # 确认后：注释/取值/示例入文
    assert "可选值：P=待付款；S=已发货" in after.text and "示例为P" in after.text
    assert after.vector  # 有向量（非空）
    assert after.payload["draft_count"] == 0


async def test_confirm_all_batches_single_reembed(tmp_path):
    """confirm_all 多表草案 → 收集表集合一次性重嵌（_reembed_tables/_rebuild_vstore 各恰一次）。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [
        {"table": "orders", "column": "status", "comment": "订单状态"},
        {"table": "customers", "column": "name", "comment": "客户姓名"},
    ])
    reembed_calls: list[list[str]] = []
    rebuild_calls = 0
    orig_reembed, orig_rebuild = kb._reembed_tables, kb._rebuild_vstore

    async def spy_reembed(conn_id: str, table_names: list[str]) -> None:
        reembed_calls.append(list(table_names))
        await orig_reembed(conn_id, table_names)

    def spy_rebuild(conn_id: str) -> None:
        nonlocal rebuild_calls
        rebuild_calls += 1
        orig_rebuild(conn_id)

    kb._reembed_tables = spy_reembed
    kb._rebuild_vstore = spy_rebuild
    counts = await kb.confirm_all("c1")
    assert counts["docs"] == 2
    assert len(reembed_calls) == 1                      # 一次批量，不逐表重嵌
    assert set(reembed_calls[0]) == {"orders", "customers"}
    assert rebuild_calls == 1                           # 一次 vstore 重建，非 N×全量


async def test_confirm_all_includes_graph_edges(tmp_path):
    """一键确认启用纳入图边：全部 LLM draft 边确认入正式图谱，draft 清空，edges 计数正确。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb._llm_graph_edges["c1"] = [
        {"from_table": "orders", "from_col": "status", "to_table": "customers",
         "to_col": "id", "cardinality": "n:1", "reason": "测试边A", "status": "draft"},
        {"from_table": "orders", "from_col": "id", "to_table": "customers",
         "to_col": "name", "cardinality": "1:n", "reason": "测试边B", "status": "draft"},
    ]
    counts = await kb.confirm_all("c1")
    assert counts["edges"] == 2
    assert kb.llm_graph_edges("c1") == []
    graph_edges = kb.graph("c1")["edges"]
    llm_edges = [e for e in graph_edges if e.get("kind") == "llm"]
    assert len(llm_edges) == 2
    assert all(e.get("reason") in ("测试边A", "测试边B") for e in llm_edges)


async def test_proposal_not_in_vector_until_confirmed(tmp_path):
    """2026-09 修订：提案不入合成文本/向量（确认后才提升），拒绝=清提案。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [{
        "table": "orders", "column": "status",
        "comment": "订单状态", "values": "P=待付款", "example": "P",
    }])
    # 提案未确认 -> 不入向量（合成文本只用当前生效值）
    rich = kb._vector_store("c1")._chunks["tbl-orders"]
    assert "待付款" not in rich.text and "订单状态" not in rich.text
    rich_vec = list(rich.vector)

    # 拒绝提案（保持当前）-> 返回 1，提案清除
    assert await kb.reject_comment("c1", "orders", "status") == 1
    after = kb._vector_store("c1")._chunks["tbl-orders"]
    assert "待付款" not in after.text
    assert after.vector == rich_vec  # 提案从不入向量 -> 向量无变化

async def test_proposal_not_in_vector_until_confirmed(tmp_path):
    """2026-09 修订：提案不入合成文本/向量（确认后才提升）；拒绝=清提案、当前不动。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.annotate_drafts("c1", [{
        "table": "orders", "column": "status",
        "comment": "订单状态", "values": "P=待付款", "example": "P",
    }])
    # 提案未确认 -> 合成文本（向量源）只用当前生效值
    text_before = kb._synthesize_table_text("c1", kb._tables["c1"]["orders"])
    assert "待付款" not in text_before and "订单状态" not in text_before

    # 拒绝提案（保持当前）-> 返回 1，提案清除
    assert await kb.reject_comment("c1", "orders", "status") == 1
    ci = kb._tables["c1"]["orders"].columns["status"]
    assert not ci.has_proposal and ci.comment == ""

    # 确认 -> 提案提升进当前（此后才有取值知识/入向量）
    kb.annotate_drafts("c1", [{
        "table": "orders", "column": "status",
        "comment": "订单状态", "values": "P=待付款", "example": "P",
    }])
    assert await kb.confirm("c1", "orders", "status") == 1
    assert ci.comment == "订单状态" and ci.values == "P=待付款"

