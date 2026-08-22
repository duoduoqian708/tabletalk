"""load_result 工具（08 §4.3 行 6，WS3 T3.3）：按 result_id 取会话工件，只读。

入参 {result_id}；返回列名/行数/样例行（上限 N=20）。三档行数据复用 data_tier（strict 无行、
standard redact、open 明文）；不存在/跨 session 的 id 报错友好。工件进模型上下文恒经脱敏管道（铁律1）。
"""
from __future__ import annotations

from app.ai.tools.registry import ToolOutcome, get_active_session, register_tool

SAMPLE_ROWS = 20  # "看形状"样例行上限


async def _load_result(state, args, conn_id, include_data=False):
    from app.ai.tools.data_tier import apply_data_tier, codify_columns_for_model

    result_id = (args or {}).get("result_id", "")
    if not result_id:
        return ToolOutcome(
            result={"ok": False, "error": "缺少 result_id，无法定位工件。请从对话历史里引用具体的 result_id。"},
            card={"tier": "read", "verdict": "block", "sql": "", "sub": "load_result 缺参", "reason": "缺少 result_id"},
            think="load_result 缺 result_id。",
        )
    session_id = get_active_session()
    if not session_id:
        return ToolOutcome(
            result={"ok": False, "error": "当前没有可用的会话上下文，无法按会话定位工件。"},
            card={"tier": "read", "verdict": "block", "sql": "", "sub": "load_result 无会话", "reason": "无会话上下文"},
        )
    try:
        artifact = state.chats.get_artifact(session_id, result_id)
    except Exception:
        artifact = None
    if artifact is None:
        # 不存在或跨 session：统一友好报错（不泄露别的会话存在性）
        return ToolOutcome(
            result={"ok": False, "error": f"工件 {result_id} 不存在，或不属于当前会话（跨会话不可访问）。请从当前对话历史里引用有效的 result_id。"},
            card={"tier": "read", "verdict": "block", "sql": "", "sub": "load_result 未命中", "reason": "工件事务不存在或跨会话"},
            think=f"工件 {result_id} 不在当前会话，已拒绝。",
        )

    columns = artifact.get("columns") or []
    rows = artifact.get("rows") or []
    row_count = artifact.get("row_count") or len(rows)
    sample = rows[:SAMPLE_ROWS]
    # 三档行数据处理 + B3 列代号化（模型可见世界恒代号）
    tiered_rows, redactions = apply_data_tier(state, conn_id, sample, columns)
    model_columns = codify_columns_for_model(state, conn_id, columns)
    result: dict = {
        "ok": True,
        "result_id": result_id,
        "columns": model_columns,
        "row_count": row_count,
        "truncated": (artifact.get("truncated") or len(rows) > SAMPLE_ROWS),
        "sample_rows": tiered_rows if tiered_rows is not None else [],  # strict 档为空数组（行不返回）
        "rows_returned": (len(tiered_rows) if tiered_rows is not None else 0),
    }
    if redactions:
        result["redactions"] = redactions
    if tiered_rows is None:
        result["note"] = "严格模式下不返回行数据，仅列名与行数。"
    return ToolOutcome(
        result=result,
        think=f"按 result_id 取回工件（{row_count} 行，样例行深 {len(tiered_rows or [])}）。",
    )


def register() -> None:
    register_tool(
        name="load_result",
        description="按 result_id 取当前会话里之前查询的结果工件（列名/行数/样例行），用于追问引用“前面那个结果”。只读，不执行 SQL。",
        props={"result_id": {"type": "string", "description": "对话历史里出现过的 result_id"}},
        required=["result_id"],
        handler=_load_result,
    )