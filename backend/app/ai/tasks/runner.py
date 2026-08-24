"""任务 runner：触发 → assess_sql → v1 SELECT-only 校验 → 执行 → 审计。

v1 安全策略：定时任务只允许 SELECT 查询，禁止 DML/DDL。
手动触发走同一条校验链路，不成为旁路。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.safety import gate as safety_gate
from app.safety.models import Origin, Verdict

if TYPE_CHECKING:
    from app.state import AppState


async def run_task(state: "AppState", task_id: str) -> dict[str, Any]:
    """执行一个定时任务：校验 SQL → 执行 → 记录结果。

    返回 {"ok": bool, "status": str, "summary": str, ...}
    """
    from app.ai.tasks.storage import TaskStore
    from app.config import get_env

    store = TaskStore(get_env().data_dir)
    task = store.get(task_id)
    if not task:
        return {"ok": False, "error": f"任务 {task_id} 不存在"}

    sql = (task.get("sql") or "").strip()
    if not sql:
        store.update(task_id, enabled=0)
        return {"ok": False, "error": "任务无 SQL，已自动禁用"}

    conn_id = task.get("connection_id", "")
    run_id = store.log_run(task_id)

    # 安全校验：过闸门，v1 只允许 SELECT（verdict 必须为 allow，tier 必须为 read）
    try:
        cfg = state.connections.get(conn_id)
        dialect = safety_gate.sqlglot_dialect_for(cfg.dialect)
    except Exception as e:
        status = "error"
        summary = f"连接配置异常: {e}"
        store.finish_run(run_id, status, summary)
        return {"ok": False, "status": status, "summary": summary}

    assessment = safety_gate.assess_sql(sql, dialect, Origin.MANUAL)

    if assessment.verdict != Verdict.ALLOW or assessment.tier.value != "read":
        status = "blocked_unsafe"
        reason_parts = [r.get("message", "") for r in (assessment.reasons or []) if r.get("message")]
        summary = f"SQL 未通过安全校验（verdict={assessment.verdict.value}, tier={assessment.tier.value}）" + (f": {'; '.join(reason_parts[:2])}" if reason_parts else "")
        store.finish_run(run_id, status, summary)
        # 审计
        try:
            state.audit.log(
                connection=task.get("connection_id", ""),
                origin="ai",
                tier=assessment.tier.value,
                verdict=assessment.verdict.value,
                status=status,
                sql=f"[scheduled] {sql[:120]}",
                source="scheduled",
            )
        except Exception:
            pass
        return {"ok": False, "status": status, "summary": summary}

    # 执行 SELECT
    t0 = time.monotonic()
    try:
        from app.core.query import execute as run_query
        res = await run_query(state, conn_id, sql)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        row_count = res.get("row_count", 0)
        truncated = res.get("truncated", False)
        status = "success"
        summary = f"{row_count} 行" + ("（已截断）" if truncated else "")
        store.finish_run(run_id, status, summary)
        # 审计
        try:
            state.audit.log(
                connection=task.get("connection_id", ""),
                origin="ai",
                tier="read",
                verdict="allow",
                status="scheduled_exec",
                sql=f"[scheduled] {sql[:120]}",
                elapsed_ms=elapsed_ms,
                source="scheduled",
            )
        except Exception:
            pass
        return {
            "ok": True,
            "status": status,
            "summary": summary,
            "row_count": row_count,
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            "columns": res.get("columns", []),
        }
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status = "error"
        summary = f"执行失败: {e}"
        store.finish_run(run_id, status, summary)
        # 审计
        try:
            state.audit.log(
                connection=task.get("connection_id", ""),
                origin="ai",
                tier="read",
                verdict="block",
                status="scheduled_error",
                sql=f"[scheduled] {sql[:120]}",
                elapsed_ms=elapsed_ms,
                source="scheduled",
            )
        except Exception:
            pass
        return {"ok": False, "status": status, "summary": summary}
