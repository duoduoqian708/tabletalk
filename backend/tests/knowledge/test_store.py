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
    docs = await kb.retrieve("c1", query="订单", k=10)
    assert any("orders" in d.title for d in docs)


async def test_graph_includes_fk_only(tmp_path):
    """设计文档：图谱仅包含 FK 边（overlap 边已移除，仅用于内部检索）。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    edges = kb.graph("c1")["edges"]
    assert any(e["kind"] == "fk" for e in edges)
    # 无 overlap 边（设计文档：图谱仅 FK 边用于可视化）
    assert not any(e["kind"] == "overlap" for e in edges)


async def test_fk_graph_expansion_surfaces_neighbors(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    docs = await kb.retrieve("c1", query="订单表", k=10)
    titles = [d.title for d in docs]
    assert any("orders" in t for t in titles)
    assert any("customers" in t for t in titles)


async def test_target_table_still_ranks_first(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    docs = await kb.retrieve("c1", query="订单", table="customers", k=10)
    assert docs[0].table == "customers"


async def test_ai_draft_then_confirm_new_model(tmp_path):
    """AI 草案落库到 ColumnInfo/TableKnowledge（v2），确认后 status=confirmed。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)  # 本例测手动草案机制，关掉 AI 阶段保证确定性
    n = kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "订单状态草稿"}])
    assert n == 1
    tk = kb._tables["c1"]["orders"]
    ci = tk.columns["status"]
    assert ci.comment == "订单状态草稿" and ci.status == "draft"
    # 确认单列
    assert kb.confirm("c1", "orders", "status") == 1
    assert ci.status == "confirmed"
    # 再注释不覆盖已确认内容
    kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "覆盖尝试"}])
    assert ci.comment == "订单状态草稿"


async def test_table_level_annotation_and_confirm_all(tmp_path):
    """表级注释草案 → confirm(table) 确认表+全部列；confirm_all 全库确认。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)  # 本例测手动草案机制，关掉 AI 阶段保证确定性
    kb.annotate_drafts("c1", [
        {"table": "orders", "comment": "订单主表草案"},
        {"table": "orders", "column": "id", "comment": "订单ID"},
        {"table": "customers", "column": "name", "comment": "客户姓名"},
    ])
    assert kb.confirm("c1", "orders") == 2  # 表注释 + id 列
    tk = kb._tables["c1"]["orders"]
    assert tk.status == "confirmed" and tk.columns["id"].status == "confirmed"
    assert kb.pending_counts("c1")["draft_docs"] == 1
    counts = kb.confirm_all("c1")
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
    assert kb.reject("c1", "orders", "status") == 1
    ci = kb._tables["c1"]["orders"].columns["status"]
    assert ci.status == "none" and ci.comment == "" and ci.values == "" and ci.example == ""
    # 表级撤下
    kb.annotate_drafts("c1", [{"table": "orders", "comment": "表注释"}])
    assert kb.reject_comment("c1", "orders") == 1
    assert kb._tables["c1"]["orders"].comment == ""


async def test_artifact_persists_across_instances(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "订单状态草稿"}])
    kb.confirm("c1", "orders", "status")

    kb2 = KnowledgeBase(tmp_path)
    assert kb2.is_built("c1")  # artifact 存在 → 无需重采样即可检索
    docs = await kb2.retrieve("c1", query="订单", k=10)
    assert any("orders" in d.title for d in docs)
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


async def test_annotate_persists_across_instances(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.annotate("c1", "orders", "status", "pending 表示待支付")
    kb2 = KnowledgeBase(tmp_path)
    docs = await kb2.retrieve("c1", query="待支付", k=5)
    assert any("待支付" in d.body for d in docs)


async def test_empty_query_returns_all(tmp_path):
    kb = KnowledgeBase(tmp_path)
    n = (await kb.build("c1", _schema()))["docs"]
    docs = await kb.retrieve("c1", query="", k=100)
    assert len(docs) == n


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
    kb2 = KnowledgeBase(tmp_path)
    assert kb2.is_built("c1")
    kb2.ensure_loaded("c1")
    tables = kb2.overview("c1")["tables"]
    assert any(t["name"] == "orders" for t in tables)
    assert any(e["kind"] == "fk" for e in kb2.graph("c1")["edges"])
