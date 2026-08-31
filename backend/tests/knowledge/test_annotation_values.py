"""阶段一注释产出（v2）：values/example 并入逐表注释，受数据授权（include_samples）门控。

断言以 mock 网关的确定性产出为准：
- 授权构建 → 低基数离散列 values 非空且含「；」分隔、example 取首个非空样本（截断60）；
- 未授权 → 无样本 → comment 仅凭结构，values/example 为空；
- 真实解析路径：LLM JSON 的可选 values 字段透传 + example 由后端从样本规范化。
"""
from __future__ import annotations

from app.knowledge.annotator import _first_example, _mock_values_for, _parse_items, _explicit_enum


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
    """include_samples=True -> 低基数列 values 非空且「；」分隔、example 取首样本（进提案）。"""
    st = app_state
    conn = "c-auth"
    stats = await st.knowledge.build(conn, _schema(), _samples(), include_samples=True)

    tk = st.knowledge._tables[conn]["orders"]
    ci = tk.columns["status"]
    # 2026-09 修订：AI 产出进 proposed_*（当前值不动，待确认提升）
    assert ci.proposed_comment
    assert ci.proposed_values and "；" in ci.proposed_values
    assert all("=" in seg for seg in ci.proposed_values.split("；"))
    assert ci.proposed_example == "P"                # 首个非空样本
    assert tk.proposed_comment                       # 表级注释提案
    assert stats["ai_docs_added"] > 0
    assert tk.ddl                                     # DDL 进 TableKnowledge
    # 确认 -> 提案提升为当前
    assert await st.knowledge.confirm(conn) > 0
    assert ci.status == "confirmed" and ci.comment
    assert ci.values and "；" in ci.values and ci.example == "P"


async def test_sync_without_samples_preserves_existing_values(app_state):
    """增量同步无样本：变化表不清空已有 values/example——draft 列防护（版本制重建已从零，此防护服务 sync）。"""
    st = app_state
    conn = "c-preserve"
    await st.knowledge.build(conn, _schema(), _samples(), include_samples=True)
    await st.knowledge.confirm(conn)  # 提案提升为当前
    ci = _status_ci(st.knowledge, conn)
    assert ci.values and ci.example  # 前置：授权构建已产出 values/example
    prev_values, prev_example = ci.values, ci.example
    # 增量同步（无样本）：注释可被结构注释覆盖，但取值知识不清
    import copy
    schema2 = copy.deepcopy(_schema())
    schema2["columns"][1]["comment"] = "订单状态"  # 触发 changed table
    await st.knowledge.incremental_build(conn, schema2, {}, include_samples=False)
    ci2 = _status_ci(st.knowledge, conn)
    assert ci2.values == prev_values
    assert ci2.example == prev_example


async def test_sync_supplements_missing_values_on_confirmed(app_state):
    """增量同步带样本：confirmed 列缺失的 values/example 被补充（comment 不被覆盖）。"""
    st = app_state
    conn = "c-supp"
    await st.knowledge.build(conn, _schema(), _samples(), include_samples=True)
    kb = st.knowledge
    ci = kb._tables[conn]["orders"].columns["status"]
    await kb.confirm(conn)  # 全部提升为当前（comment/values/example 就位）
    kb._save_conn(conn)
    import copy
    schema2 = copy.deepcopy(_schema())
    schema2["columns"][1]["comment"] = "订单状态"  # 触发 changed table
    await kb.incremental_build(conn, schema2, _samples(), include_samples=True)
    ci2 = kb._tables[conn]["orders"].columns["status"]
    assert ci2.status == "confirmed"
    assert ci2.comment                       # comment 保留
    assert ci2.values                        # values 补充
    assert ci2.example                       # example 补充


async def test_build_unauthorized_empty_values_example(app_state):
    """include_samples=False → 无样本出网 → values/example 为空，仅结构注释。"""
    st = app_state
    conn = "c-noauth"
    await st.knowledge.build(conn, _schema(), _samples(), include_samples=False)
    tk = st.knowledge._tables[conn]["orders"]
    for ci in tk.columns.values():
        assert ci.proposed_comment                    # 注释仍生成（凭结构）
        assert ci.proposed_values == "" and ci.proposed_example == ""  # 无数据授权 -> 零实例内容


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
        assert ("F" in _status_ci(st.knowledge, conn).proposed_values) is authorized


async def test_sync_full_rebuild_fallback_respects_gate(app_state):
    """未构建过的连接走 sync() 防御性全量分支：门控两态行为与增量分支一致。"""
    st = app_state
    for conn, authorized in (("sync-off", False), ("sync-on", True)):
        await st.knowledge.sync(conn, _schema(), _samples(), include_samples=authorized)
        assert (_status_ci(st.knowledge, conn).proposed_values != "") is authorized


async def test_sync_resolves_none_from_runtime_setting(app_state):
    """include_samples=None 时回退运行时授权设置——全量回退分支与增量分支同语义（不分叉）。"""
    st = app_state
    st.runtime.update({"kb_ai_annotation_samples": True})
    await st.knowledge.sync("sync-runtime", _schema(), _samples())
    assert _status_ci(st.knowledge, "sync-runtime").proposed_values != ""


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


# ---------- 结构显式枚举（不勾样本也能有 values） ----------


def test_explicit_enum_from_check_constraint():
    ddl = (
        'CREATE TABLE "orders" (\n'
        '  "id" INTEGER PRIMARY KEY,\n'
        '  "status" TEXT CHECK (status IN (\'P\',\'S\',\'R\'))\n'
        ');'
    )
    assert _explicit_enum(ddl, "status", "TEXT") == ["P", "S", "R"]
    assert _explicit_enum(ddl, "id", "INTEGER") is None


def test_explicit_enum_from_mysql_enum_type():
    ddl = "CREATE TABLE t (s ENUM('a','b','c'))"
    assert _explicit_enum(ddl, "s", "ENUM('a','b','c')") == ["a", "b", "c"]
    assert _explicit_enum(ddl, "s", "enum('a','b')") == ["a", "b"]  # 小写兼容


async def test_build_unauthorized_extracts_explicit_enum(app_state):
    """不勾样本：显式枚举（ENUM 类型）转占位 key-value 提为 values；example 仍为空。"""
    st = app_state
    schema = _schema()
    schema["columns"][1]["type"] = "ENUM('P','S','R')"  # status 变显式枚举
    conn = "c-enum"
    await st.knowledge.build(conn, schema, {}, include_samples=False)
    ci = st.knowledge._tables[conn]["orders"].columns["status"]
    assert ci.proposed_values == "P=P（业务含义待确认）；S=S（业务含义待确认）；R=R（业务含义待确认）"
    assert ci.example == ""


async def test_overview_marks_is_enum_by_values(app_state):
    """is_enum 契约：有 key-value 枚举数组才算枚举（LLM/结构/手动同一判定）。"""
    st = app_state
    # 授权构建 → 低基数列有 values → is_enum=true；单值列无枚举 → false
    conn = "c-flag"
    samples = _samples()
    samples["orders"]["id"] = [1]  # 单值 → mock 不产占位枚举
    await st.knowledge.build(conn, _schema(), samples, include_samples=True)
    cols = {c["name"]: c for c in st.knowledge.overview(conn)["tables"][0]["columns"]}
    assert cols["status"]["is_enum"] is True
    assert cols["id"]["is_enum"] is False
    # 无样本无显式枚举 → 无 values → 非枚举
    conn2 = "c-flag2"
    await st.knowledge.build(conn2, _schema(), {}, include_samples=False)
    cols2 = {c["name"]: c for c in st.knowledge.overview(conn2)["tables"][0]["columns"]}
    assert all(c["is_enum"] is False for c in cols2.values())
