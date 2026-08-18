"""SQL 类原子工具：run_query / run_dml / draft_ddl（写与改结构均过闸门，执行权在人）。"""
from __future__ import annotations

from app.ai.tools.registry import ToolOutcome, _get_ctx, _sub, register_tool
from app.safety import gate as safety_gate
from app.safety.models import Origin, Verdict


async def _run_query(state, args, conn_id, include_data=False):
    # TODO: 测试后删除
    from app.debuglog import dbg
    _cfg, dialect = _get_ctx(state, conn_id)
    sql = args.get("sql", "")
    assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
    if assessment.verdict == Verdict.ALLOW:
        from app.core.query import execute as run_query

        res = await run_query(state, conn_id, sql)
        result: dict = {
            "ok": True,
            "columns": res["columns"],
            "row_count": res["row_count"],
            "truncated": res["truncated"],
            "elapsed_ms": res["elapsed_ms"],
        }
        if include_data:
            result["rows"] = res["rows"]
        # 卡片带完整结果（含 rows 供前端直接渲染，消除同 SQL 二次执行）；喂模型的 result 保持不含行
        card = {
            "tier": "read", "verdict": "allow", "sql": sql, "sub": _sub(sql, dialect),
            "result": {
                "columns": res["columns"], "types": res["types"],
                "rows": res["rows"], "row_count": res["row_count"],
                "truncated": res["truncated"], "elapsed_ms": res["elapsed_ms"],
            },
        }
        # 审计：对话内部真实读必须留痕
        state.audit.log(
            connection=_cfg.name, origin="ai", tier="read", verdict="allow",
            status="对话内读（循环内测量）", sql=sql,
            elapsed_ms=res.get("elapsed_ms"), source="loop_internal",
        )
        return ToolOutcome(
            result=result,
            card=card,
            think=f"只读查询，安全闸门放行（{res['row_count']} 行）。",
        )
    # TODO: 测试后删除
    dbg("[tool.run_query] NOT_ALLOW verdict=", assessment.verdict.value, "reasons=", assessment.reasons)
    return ToolOutcome(
        result={"ok": False, "verdict": assessment.verdict.value, "reason": "; ".join(assessment.reasons)},
        card={"tier": assessment.tier.value, "verdict": assessment.verdict.value, "sql": sql,
              "sub": _sub(sql, dialect), "reason": "; ".join(assessment.reasons)},
        think="安全闸门未放行。",
    )


async def _run_dml(state, args, conn_id, include_data=False):
    _cfg, dialect = _get_ctx(state, conn_id)
    sql = args.get("sql", "")
    assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
    if assessment.verdict == Verdict.BLOCK:
        return ToolOutcome(
            result={"ok": False, "verdict": "block", "reason": "; ".join(assessment.reasons)},
            card={"tier": "dml", "verdict": "block", "sql": sql, "sub": _sub(sql, dialect),
                  "reason": "; ".join(assessment.reasons)},
            think="写操作被拦截。",
        )
    preview = await safety_gate.preview_rows(state, conn_id, sql, dialect)
    return ToolOutcome(
        result={"ok": True, "verdict": "review", "preview_rows": preview, "needs_confirm": True,
                "reason": "; ".join(assessment.reasons)},
        card={"tier": "dml", "verdict": "review", "sql": sql, "sub": _sub(sql, dialect),
              "preview_rows": preview, "reason": "需确认后执行"},
        think=f"写操作已评估：预估影响 {preview if preview is not None else '未知'} 行，需确认。",
    )


async def _draft_ddl(state, args, conn_id, include_data=False):
    sql = args.get("sql", "")
    return ToolOutcome(
        result={"ok": True, "note": "DDL 脚本已生成，发送到编辑器手动执行。AI 不执行 DDL。"},
        card={"tier": "ddl", "verdict": "manual", "sql": sql, "sub": "DDL · manual only"},
        think="生成 DDL 脚本（仅草稿，不执行）。",
    )


def register() -> None:
    register_tool(
        "run_query",
        "执行只读查询。结果只会返回列名与行数；如需明细需用户 opt-in。",
        {"sql": {"type": "string", "description": "只读 SELECT SQL"}},
        ["sql"],
        _run_query,
    )
    register_tool(
        "run_dml",
        "执行写操作（INSERT/UPDATE/DELETE）。必须先评估影响行数并需用户确认，绝不自动执行。",
        {"sql": {"type": "string", "description": "DML SQL"}},
        ["sql"],
        _run_dml,
    )
    register_tool(
        "draft_ddl",
        "生成 DDL 脚本（CREATE/ALTER/DROP 等）草稿，发送到编辑器由用户手动执行。绝不执行。",
        {"sql": {"type": "string", "description": "DDL SQL 脚本"}},
        ["sql"],
        _draft_ddl,
    )
