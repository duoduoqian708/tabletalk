"""AI 自动注释器测试：prompt 构造 / JSON 解析 / mock 确定性注释。"""
from __future__ import annotations

from app.knowledge.annotator import _mock_comments, _parse_items, annotate_knowledge


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
    res = await annotate_knowledge(
        st, "c-anno", include_samples=True, schema=schema,
        samples={"orders": {"status": [long_val]}},
    )
    assert res["added"] > 0
    body = next(
        d.body for d in st.knowledge.list_docs("c-anno")
        if d.column == "status" and d.source == "ai_draft"
    )
    assert long_val[:60] in body   # 截断值进入草案（= 发送内容）
    assert long_val not in body    # 原始长句不出网/不入库
