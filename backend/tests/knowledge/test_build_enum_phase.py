"""构建第四阶段：枚举字典抽取并入 build 流水线，受数据授权（include_samples）门控。

断言以 mock 网关的确定性产出为准：
- _mock_enums 对去重取值数 ∈ [2, ENUM_MAX_VALUES] 的列生成 draft 条目；
- 按用户决策不做列级过滤：授权后整行样本直发，created_at 等审计列同样参与，
  唯一防护是值级 60 字符截断。
"""
from __future__ import annotations


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


def _status_values(kb, conn_id: str) -> set[str]:
    return {
        e["value"]
        for d in kb.enum_drafts(conn_id)
        if d["column"] == "status"
        for e in d["entries"]
    }


async def test_build_runs_enum_phase_when_authorized(app_state):
    """include_samples=True → 枚举 draft 入库（整行样本直发，审计列也参与）；False → 完全跳过、零产出。"""
    st = app_state
    conn = "c-gate"
    phases: list[str] = []

    def report(stage, percent, detail=None, phase=None):
        if phase:
            phases.append(phase)

    # act：授权构建
    stats = await st.knowledge.build(
        conn, _schema(), _samples(), on_progress=report, include_samples=True,
    )

    # assert：status 列枚举草案入库；created_at 等审计列样本随整行发出属预期（无列级过滤）
    drafts = {(d["table"], d["column"]) for d in st.knowledge.enum_drafts(conn)}
    assert ("orders", "status") in drafts
    assert ("orders", "created_at") in drafts
    assert stats["enums_added"] > 0
    assert "enums" in phases

    # 清理后未授权重建：完全跳过枚举阶段（进度条无该阶段属预期）
    st.knowledge.clear(conn)
    phases.clear()
    stats2 = await st.knowledge.build(
        conn, _schema(), _samples(), on_progress=report, include_samples=False,
    )
    assert st.knowledge.enum_drafts(conn) == []
    assert stats2["enums_added"] == 0
    assert "enums" not in phases


async def test_text_typed_low_cardinality_column_still_extracted(app_state):
    """TEXT 型低基数列（SQLite/PG status 常为 TEXT）天然参与枚举抽取（无类型过滤）；
    入库 value 为截断后的字符串，与发送内容一致。"""
    st = app_state
    schema = _schema()
    schema["columns"][1]["type"] = "TEXT"  # status 改为 TEXT：无类型过滤，照常参与
    long_val = "已支付-等待发货-" + "很长的状态说明" * 20  # 远超 60 字符
    samples = _samples()
    samples["orders"]["status"] = [long_val, "S"]

    stats = await st.knowledge.build("c-text", schema, samples, include_samples=True)

    drafts = {d["column"]: d for d in st.knowledge.enum_drafts("c-text")}
    assert "status" in drafts
    vals = {e["value"] for e in drafts["status"]["entries"]}
    assert long_val[:60] in vals   # 截断值入库（字典键 = 发送内容）
    assert long_val not in vals    # 原始长句不入库
    assert stats["enums_added"] > 0


async def test_incremental_rebuild_respects_enum_gate(app_state):
    """增量同步：未授权时变化表不重提枚举（新取值 F 不入库）；授权时重提。"""
    st = app_state
    schema2 = _schema()
    schema2["columns"][1]["comment"] = "订单状态"  # 触发 changed table
    samples2 = _samples()
    samples2["orders"]["status"] = ["P", "S", "R", "F"]  # 新增枚举取值

    for conn, authorized in (("inc-off", False), ("inc-on", True)):
        await st.knowledge.build(conn, _schema(), _samples(), include_samples=True)
        assert "F" not in _status_values(st.knowledge, conn)

        res = await st.knowledge.incremental_build(
            conn, schema2, samples2, include_samples=authorized,
        )
        assert res["changed"]
        assert ("F" in _status_values(st.knowledge, conn)) is authorized


async def test_sync_full_rebuild_fallback_respects_gate(app_state):
    """未构建过的连接走 sync() 防御性全量分支：门控两态行为与增量分支一致。"""
    st = app_state
    for conn, authorized in (("sync-off", False), ("sync-on", True)):
        stats = await st.knowledge.sync(conn, _schema(), _samples(), include_samples=authorized)
        drafts = st.knowledge.enum_drafts(conn)
        assert (len(drafts) > 0) is authorized
        assert (stats["enums_added"] > 0) if authorized else (stats["enums_added"] == 0)


async def test_sync_resolves_none_from_runtime_setting(app_state):
    """include_samples=None 时回退运行时授权设置——全量回退分支与增量分支同语义（不分叉）。"""
    st = app_state
    st.runtime.update({"kb_ai_annotation_samples": True})
    stats = await st.knowledge.sync("sync-runtime", _schema(), _samples())
    assert stats["enums_added"] > 0
    assert st.knowledge.enum_drafts("sync-runtime")
