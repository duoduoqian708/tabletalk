"""双通道融合路由：标签路由 × 向量召回 → FK 扩展（一次查询既用图库又用向量库）。"""

from __future__ import annotations

from app.knowledge.store import KnowledgeBase
from app.ai.context import assemble_context_full


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "订单主表", "column_count": 3},
            {"name": "customers", "kind": "table", "comment": "客户表", "column_count": 2},
            {"name": "refund_requests", "kind": "table", "comment": "退款申请（无标签，靠语义召回）", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "customer_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "orders", "name": "total", "type": "real", "nullable": True, "pk": False, "fk": False, "default": None, "comment": "订单金额"},
            {"table": "customers", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "customers", "name": "name", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
            {"table": "refund_requests", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "refund_requests", "name": "order_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": "关联订单"},
            {"table": "refund_requests", "name": "amount", "type": "real", "nullable": True, "pk": False, "fk": False, "default": None, "comment": "退款金额"},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"},
            {"table": "refund_requests", "column": "order_id", "ref_table": "orders", "ref_column": "id"},
        ],
    }


async def test_vector_route_recalls_unlabeled_table(tmp_path):
    """向量通道：无标签但有语义相关注释的表也能被问题召回（补齐标签覆盖率短板）。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    hits = await kb.vector_route_tables("c1", "哪些订单申请了退款", top_k=3)
    names = [t for t, _ in hits]
    assert "refund_requests" in names  # 语义命中"退款"→ 无标签表被召回
    assert hits[0][1] > 0


async def test_expand_tables_fk_connectivity(tmp_path):
    """图谱通道：融合种子沿 FK 扩展成连通子图。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    expanded = kb.expand_tables("c1", {"refund_requests"}, hops=2)
    assert "orders" in expanded      # refund_requests FK→orders
    assert "customers" in expanded   # orders FK→customers（2 跳）


async def test_assemble_context_fusion_uses_both_channels(app_state, conn_id):
    """融合路由端到端：问题命中"退款"语义时，无标签的 refund 相关表进入候选子图。"""
    text, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    tables = set(meta.get("candidate_tables", []))
    assert tables, "融合路由必须产生候选表（不再因标签未命中退化为全表）"
    assert "returns" in tables  # 词面加权命中（问题含表名 returns）
    assert "order_items" in tables or "orders" in tables  # 图谱 FK 扩展生效


async def test_reembed_when_embedding_config_changes(tmp_path):
    """用户配置/更换嵌入模型 → 旧向量必须重嵌（模型必须用户配置且配置后生效）。"""
    from app.core.settings import SettingsStore
    from app.knowledge.embedding import HashingEmbedder

    runtime = SettingsStore(tmp_path)
    kb = KnowledgeBase(tmp_path, runtime=runtime)
    await kb.build("c1", _schema())
    fp_before = kb._artifact_fingerprint.get("c1")
    assert fp_before == "hash"  # 未配置嵌入模型 → 哈希指纹

    # 未变化：不重嵌
    assert await kb.reembed_if_needed("c1") is False

    # 模拟用户配置 api 嵌入（无 key 时 make_embedder 仍走 api 分支？这里直接替换 embedder 并改指纹来源）
    class FakeRuntime:
        def get(self):
            class S:
                embedding_provider = "api"
                embedding_base_url = "https://example.com/v1"
                embedding_model = "bge-m3"
                embedding_api_key = ""
            return S()
    kb._runtime = FakeRuntime()  # type: ignore[assignment]
    # 换成可注入的假嵌入器（避免真实网络调用）：直接打桩
    class FakeEmb:
        async def embed(self, text: str) -> list[float]:
            return [0.5] * 8
    kb._emb = FakeEmb()
    assert await kb.reembed_if_needed("c1") is True
    assert kb._artifact_fingerprint.get("c1").startswith("api:")
    # 再次调用：指纹一致 → 不重嵌
    assert await kb.reembed_if_needed("c1") is False
