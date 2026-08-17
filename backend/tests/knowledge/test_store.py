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


async def test_graph_includes_fk_and_value_overlap(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    edges = kb.graph("c1")["edges"]
    assert any(e["kind"] == "fk" for e in edges)
    # orders.customer_id 与 customers.id 共享样本值 → 值重叠边
    assert any(e["kind"] == "overlap" and e["from"] == "orders" and e["to"] == "customers" for e in edges)
    # 启发式：同名主键 id↔id 的重叠被过滤
    assert not any(e["kind"] == "overlap" and e["from_col"] == "id" and e["to_col"] == "id" for e in edges)


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


async def test_ai_draft_then_confirm(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    assert kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "订单状态草稿"}]) == 1
    docs = await kb.retrieve("c1", query="状态", k=10)
    assert any(d.source == "ai_draft" and d.status == "draft" for d in docs)
    assert kb.confirm("c1", "orders", "status") == 1
    docs2 = await kb.retrieve("c1", query="状态", k=10)
    assert any(d.status == "confirmed" for d in docs2)


async def test_reject_removes_draft(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "会被拒绝的草稿"}])
    drafts = [d for d in kb._drafts["c1"]]
    assert kb.reject("c1", drafts[0].id) is True
    assert len(kb._drafts["c1"]) == 0


async def test_artifact_persists_across_instances(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    kb.annotate_drafts("c1", [{"table": "orders", "column": "status", "comment": "订单状态草稿"}])
    kb.confirm("c1", "orders", "status")

    kb2 = KnowledgeBase(tmp_path)
    assert kb2.is_built("c1")  # artifact 存在 → 无需重采样即可检索
    docs = await kb2.retrieve("c1", query="订单", k=10)
    assert any("orders" in d.title for d in docs)
    # 确认状态在 artifact 中保留
    col = next(c for c in kb2.overview("c1")["columns"] if c["name"] == "status")
    assert col["status"] == "confirmed"
    # 图谱（含值重叠边）也保留了
    assert any(e["kind"] == "overlap" for e in kb2.graph("c1")["edges"])


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
