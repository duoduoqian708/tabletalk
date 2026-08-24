"""构建第四阶段：枚举字典抽取并入 build 流水线，受数据授权（include_samples）门控。

断言以 mock 网关的确定性产出为准：
- _mock_enums 对去重取值数 ∈ [2, ENUM_MAX_VALUES] 的列生成 draft 条目；
- llm_safe_samples 裁剪噪声列（created_at 等审计列）——即使有取值也不发 LLM。
注意：status 用 varchar 而非 text——TEXT 前缀命中裁剪器的长文本规则（既有设计）。
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
    """include_samples=True → 枚举 draft 入库（噪声列除外）；False → 完全跳过、零产出。"""
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

    # assert：status 列枚举草案入库；created_at 是审计噪声列被出网裁剪
    drafts = {(d["table"], d["column"]) for d in st.knowledge.enum_drafts(conn)}
    assert ("orders", "status") in drafts
    assert ("orders", "created_at") not in drafts
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
