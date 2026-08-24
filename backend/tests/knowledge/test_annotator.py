"""AI 自动注释器测试：prompt 构造 / JSON 解析 / mock 确定性注释。"""
from __future__ import annotations

from app.knowledge.annotator import (
    _generate_candidate_pairs,
    _mock_comments,
    _parse_graph_edges,
    _parse_items,
    annotate_knowledge,
)


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "订单表", "column_count": 1},
            {"name": "customers", "kind": "table", "comment": "", "column_count": 1},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "customers", "name": "name", "type": "text", "pk": False, "fk": False, "comment": "客户姓名"},
        ],
    }


def test_mock_comments_skips_existing():
    items = _mock_comments(_schema(), {})
    # orders 已有表注释 → 不生成表级；customers 无 → 生成
    assert not any(i["table"] == "orders" and i["column"] is None for i in items)
    assert any(i["table"] == "customers" and i["column"] is None for i in items)
    # customers.name 已有注释 → 不生成
    assert not any(i["table"] == "customers" and i["column"] == "name" for i in items)
    # orders.id 无注释 → 生成
    assert any(i["table"] == "orders" and i["column"] == "id" for i in items)


def test_mock_comments_can_include_samples():
    samples = {"orders": {"id": [1, 2, 3]}}
    items = _mock_comments(_schema(), samples)
    id_item = next(i for i in items if i["table"] == "orders" and i["column"] == "id")
    assert "示例取值" in id_item["comment"]


def test_parse_items_plain_json():
    text = '[{"table":"orders","column":"id","comment":"主键"},{"table":"orders","comment":"订单主表"}]'
    items = _parse_items(text)
    assert len(items) == 2
    assert items[0]["column"] == "id"
    assert items[1]["column"] is None


def test_parse_items_code_fence():
    text = '```json\n[{"table":"orders","column":"status","comment":"状态"}]```'
    assert _parse_items(text) == [{"table": "orders", "column": "status", "comment": "状态"}]


def test_parse_items_garbage():
    assert _parse_items("抱歉，我无法") == []
    assert _parse_items("") == []


# ---------- annotate_knowledge 独立路径：出网不变量 ----------


async def test_annotate_knowledge_truncates_samples_before_send(app_state):
    """独立 annotate 路径：授权样本发送前统一值级截断（与枚举 core 同款不变量）。"""
    st = app_state
    long_val = "超长业务取值-" + "很长的说明" * 30  # 远超 60 字符
    schema = {
        "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [
            {"table": "orders", "name": "status", "type": "varchar(16)", "pk": False, "fk": False, "comment": ""},
        ],
    }
    # v2：草案落 ColumnInfo，需先有表壳（离线构建即建壳；DDL 实时获取失败自动回退快照合成）
    await st.knowledge.build("c-anno", schema, enable_ai_annotation=False)
    res = await annotate_knowledge(
        st, "c-anno", include_samples=True, schema=schema,
        samples={"orders": {"status": [long_val]}},
    )
    assert res["added"] > 0
    body = st.knowledge._tables["c-anno"]["orders"].columns["status"].comment
    assert long_val[:60] in body   # 截断值进入草案（= 发送内容）
    assert long_val not in body    # 原始长句不出网/不入库


# ---------- T2：边 v2 解析与候选（四元组 + cardinality + 多侧校验） ----------


def _graph_schema() -> dict:
    return {
        "tables": [{"name": "orders"}, {"name": "customers"}],
        "columns": [
            {"table": "orders", "name": "id", "pk": True},
            {"table": "orders", "name": "customer_id", "pk": False},
            {"table": "customers", "name": "id", "pk": True},
        ],
    }


def test_parse_graph_edges_four_tuple_and_cardinality():
    """四元组 + cardinality 齐备才收；缺字段/缺基数丢弃。"""
    ok = _parse_graph_edges(
        '[{"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id",'
        '"cardinality":"n:1","reason":"订单归属客户"}]', _graph_schema())
    assert len(ok) == 1
    assert ok[0]["from_col"] == "customer_id" and ok[0]["cardinality"] == "n:1"
    # 缺 from_col → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","to_table":"customers","cardinality":"n:1"}]', _graph_schema()) == []
    # 缺 cardinality → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id"}]',
        _graph_schema()) == []
    # 非法基数 → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id",'
        '"cardinality":"m:n"}]', _graph_schema()) == []


def test_parse_graph_edges_many_side_validation():
    """多侧校验：from_col 为 from_table 主键却声明 n:1 → 矛盾丢弃；1:1 保留。"""
    schema = _graph_schema()
    bad = _parse_graph_edges(
        '[{"from_table":"orders","from_col":"id","to_table":"customers","to_col":"id",'
        '"cardinality":"n:1"}]', schema)
    assert bad == []
    ok = _parse_graph_edges(
        '[{"from_table":"orders","from_col":"id","to_table":"customers","to_col":"id",'
        '"cardinality":"1:1"}]', schema)
    assert len(ok) == 1 and ok[0]["cardinality"] == "1:1"
    # 字段不存在 → 丢弃；自环 → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"nope","to_table":"customers","to_col":"id",'
        '"cardinality":"n:1"}]', schema) == []
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"id","to_table":"orders","to_col":"id",'
        '"cardinality":"1:1"}]', schema) == []


def test_generate_candidate_pairs_cardinality():
    """候选对：from=持有引用列的表（多侧）；列兼主键 → 1:1，否则 n:1。"""
    schema = {
        "tables": [{"name": "order"}, {"name": "customer"},
                   {"name": "profile"}, {"name": "user"}],
        "columns": [
            {"table": "order", "name": "id", "pk": True},
            {"table": "order", "name": "customer_id", "pk": False},
            {"table": "customer", "name": "id", "pk": True},
            {"table": "profile", "name": "user_id", "pk": True},
            {"table": "user", "name": "id", "pk": True},
        ],
    }
    cands = _generate_candidate_pairs(schema)
    n1 = next(c for c in cands if c["from_table"] == "order" and c["from_col"] == "customer_id")
    assert n1["to_table"] == "customer" and n1["to_col"] == "id"
    assert n1["cardinality"] == "n:1"
    one1 = next(c for c in cands if c["from_table"] == "profile" and c["from_col"] == "user_id")
    assert one1["to_table"] == "user" and one1["cardinality"] == "1:1"
