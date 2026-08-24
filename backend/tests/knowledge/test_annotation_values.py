"""阶段一注释产出（v2）：values/example 并入逐表注释，受数据授权（include_samples）门控。

断言以 mock 网关的确定性产出为准：
- 授权构建 → 低基数离散列 values 非空且含「；」分隔、example 取首个非空样本（截断60）；
- 未授权 → 无样本 → comment 仅凭结构，values/example 为空；
- 真实解析路径：LLM JSON 的可选 values 字段透传 + example 由后端从样本规范化。
"""
from __future__ import annotations

from app.knowledge.annotator import _first_example, _mock_values_for, _parse_items


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 3},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "orders", "name": "status", "type": "varchar(16)", "pk": False, "fk": False, "comment": ""},
            {"table": "orders", "name": "created_at", "type": "datetime", "pk": False, "fk": False, "comment": ""},
        ],
        "foreign_keys": [],
    }


def _samples() -> dict:
    return {
        "orders": {
            "id": [1, 2, 3],
            "status": ["P", "S", "R"],
            "created_at": ["2026-01-01", "2026-01-02"],
        },
    }


def _status_ci(kb, conn_id: str):
    return kb._tables[conn_id]["orders"].columns["status"]


async def test_build_authorized_writes_values_and_example(app_state):
    """include_samples=True → 低基数列 values 非空且「；」分隔、example 取首样本；列/表进 draft。"""
    st = app_state
    conn = "c-auth"
    stats = await st.knowledge.build(conn, _schema(), _samples(), include_samples=True)

    tk = st.knowledge._tables[conn]["orders"]
    ci = tk.columns["status"]
    assert ci.status == "draft" and ci.comment
    assert ci.values and "；" in ci.values          # 取值对照入库且分号分隔
    assert all("=" in seg for seg in ci.values.split("；"))
    assert ci.example == "P"                        # 首个非空样本
    assert tk.status == "draft" and tk.comment      # 表级注释草案
    assert stats["ai_docs_added"] > 0
    assert tk.ddl                                   # DDL 进 TableKnowledge


async def test_build_unauthorized_empty_values_example(app_state):
    """include_samples=False → 无样本出网 → values/example 为空，仅结构注释。"""
    st = app_state
    conn = "c-noauth"
    await st.knowledge.build(conn, _schema(), _samples(), include_samples=False)
    tk = st.knowledge._tables[conn]["orders"]
    for ci in tk.columns.values():
        assert ci.comment                            # 注释仍生成（凭结构）
        assert ci.values == "" and ci.example == ""  # 无数据授权 → 零实例内容


async def test_incremental_reannotation_respects_gate(app_state):
    """增量同步：未授权时变化表不产生 values；授权后重注释补上。"""
    st = app_state
    import copy

    for conn, authorized in (("inc-off", False), ("inc-on", True)):
        await st.knowledge.build(conn, _schema(), _samples(), include_samples=True)
        schema2 = copy.deepcopy(_schema())
        schema2["columns"][1]["comment"] = "订单状态"  # 触发 changed table
        samples2 = _samples()
        samples2["orders"]["status"] = ["P", "S", "R", "F"]
        res = await st.knowledge.incremental_build(
            conn, schema2, samples2, include_samples=authorized,
        )
        assert res["changed"]
        assert ("F" in _status_ci(st.knowledge, conn).values) is authorized


async def test_sync_full_rebuild_fallback_respects_gate(app_state):
    """未构建过的连接走 sync() 防御性全量分支：门控两态行为与增量分支一致。"""
    st = app_state
    for conn, authorized in (("sync-off", False), ("sync-on", True)):
        await st.knowledge.sync(conn, _schema(), _samples(), include_samples=authorized)
        assert (_status_ci(st.knowledge, conn).values != "") is authorized


async def test_sync_resolves_none_from_runtime_setting(app_state):
    """include_samples=None 时回退运行时授权设置——全量回退分支与增量分支同语义（不分叉）。"""
    st = app_state
    st.runtime.update({"kb_ai_annotation_samples": True})
    await st.knowledge.sync("sync-runtime", _schema(), _samples())
    assert _status_ci(st.knowledge, "sync-runtime").values != ""


# ---------- 真实解析路径（非 mock）：values 透传 + example 后端规范化 ----------


def test_parse_items_passes_values_through():
    text = (
        '[{"table":"orders","column":"status","comment":"订单状态",'
        '"values":" P=待付款；S=已发货 "},'
        '{"table":"orders","column":"amount","comment":"订单金额"},'
        '{"table":"orders","column":"x","comment":"无值", "values": "  "}]'
    )
    items = _parse_items(text)
    assert items[0]["values"] == "P=待付款；S=已发货"   # str 清洗去空格
    assert "values" not in items[1]                     # 未提供 → 不带键
    assert "values" not in items[2]                     # 空串 → 剔除


def test_first_example_takes_first_nonempty_truncated():
    samples = {"status": [None, "", "S", "很长的值" * 100]}
    assert _first_example(samples, "status") == "S"
    long_samples = {"v": ["x" * 100]}
    assert len(_first_example(long_samples, "v")) == 60
    assert _first_example(None, "status") == ""
    assert _first_example({"status": []}, "status") == ""


def test_mock_values_deterministic_range_gated():
    vals = _mock_values_for("status", {"status": ["P", "S", "R"]})
    assert vals == "P=P（业务含义待确认）；S=S（业务含义待确认）；R=R（业务含义待确认）"
    assert _mock_values_for("id", {"id": [f"v{i}" for i in range(51)]}) == ""  # >50 非枚举
    assert _mock_values_for("a", {"a": ["only-one"]}) == ""                    # <2 非枚举
