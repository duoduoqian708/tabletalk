"""SQL 类原子工具：run_query / run_dml / draft_ddl（写与改结构均过闸门，执行权在人）。"""
from __future__ import annotations

from app.ai.tools.registry import ToolOutcome, _get_ctx, _sub, register_tool
from app.safety import gate as safety_gate
from app.safety.blast import build_blast
from app.safety.models import Origin, Verdict


async def _run_query(state, args, conn_id, include_data=False):
    _cfg, dialect = _get_ctx(state, conn_id)
    sql = args.get("sql", "")
    assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
    if assessment.verdict == Verdict.ALLOW:
        from app.core.query import execute as run_query

        # B4 三档：strict 下 include_data 一律不放行（即使脱敏也不行）
        try:
            mode = state.runtime.get().privacy_mode
        except Exception:
            mode = "standard"
        if include_data and mode == "strict":
            return ToolOutcome(
                result={"ok": False, "verdict": "block", "reason": "严格模式下不允许返回行数据，请切换至标准或开放模式，或关闭 include_data"},
                card={"tier": "read", "verdict": "block", "sql": sql, "sub": "严格模式拦截", "reason": "严格模式下不允许返回行数据"},
                think="严格模式拦截 include_data。",
            )
        res = await run_query(state, conn_id, sql)
        # B2 脱敏：standard 下 include_data 的行在出网前脱敏（本地确定性 token），卡片仍展示原文；open 下明文
        redacted_rows = None
        redactions: list[str] = []
        if include_data and res.get("rows"):
            if mode == "open":
                redacted_rows = res["rows"]
            else:
                try:
                    from app.safety.redact import get_salt, redact_rows
                    from app.config import get_env
                    salt = get_salt(get_env().data_dir)
                    try:
                        sensitive = state.connections.get(conn_id).sensitive
                    except Exception:
                        sensitive = []
                    tbl = assessment.tables[0] if assessment.tables else ""
                    redacted, mp = redact_rows(res["rows"], res["columns"], tbl, salt, sensitive)
                    redacted_rows = redacted
                    redactions = list(mp.keys())[:5]
                except Exception:
                    redacted_rows = res["rows"]
        result: dict = {
            "ok": True,
            "columns": res["columns"],
            "row_count": res["row_count"],
            "truncated": res["truncated"],
            "elapsed_ms": res["elapsed_ms"],
        }
        if include_data:
            result["rows"] = redacted_rows if redacted_rows is not None else res["rows"]
            if redactions:
                result["redactions"] = redactions
        # 卡片带完整结果（含 rows 供前端直接渲染，消除同 SQL 二次执行）；喂模型的 result 为脱敏后
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
    reason_text = "; ".join(r.get("message", "") for r in assessment.reasons)
    return ToolOutcome(
        result={"ok": False, "verdict": assessment.verdict.value, "reason": reason_text, "reasons": assessment.reasons},
        card={"tier": assessment.tier.value, "verdict": assessment.verdict.value, "sql": sql,
              "sub": _sub(sql, dialect), "reason": reason_text, "reasons": assessment.reasons},
        think="安全闸门未放行。",
    )


async def _run_dml(state, args, conn_id, include_data=False):
    _cfg, dialect = _get_ctx(state, conn_id)
    # 只读连接硬边界：AI 不生成写卡（最终执行也会被 POST /query 拦截，这里提前告知）
    if _cfg.read_only:
        ro_reasons = [{
            "rule_id": "read-only",
            "message": "该连接标记为只读，禁止写操作",
            "message_en": "Connection is read-only; writes are blocked",
            "objects": [],
        }]
        return ToolOutcome(
            result={"ok": False, "verdict": "block", "reason": "该连接标记为只读，禁止写操作", "reasons": ro_reasons},
            card={"tier": "dml", "verdict": "block", "sql": args.get("sql", ""),
                  "sub": "只读连接", "reason": "该连接标记为只读，禁止写操作", "reasons": ro_reasons},
            think="只读连接，写操作已拦截。",
        )
    sql = args.get("sql", "")
    assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
    if assessment.verdict == Verdict.BLOCK:
        reason_text = "; ".join(r.get("message", "") for r in assessment.reasons)
        return ToolOutcome(
            result={"ok": False, "verdict": "block", "reason": reason_text, "reasons": assessment.reasons},
            card={"tier": "dml", "verdict": "block", "sql": sql, "sub": _sub(sql, dialect),
                  "reason": reason_text, "reasons": assessment.reasons},
            think="写操作被拦截。",
        )
    preview = await safety_gate.preview_rows(state, conn_id, sql, dialect)
    reason_text = "; ".join(r.get("message", "") for r in assessment.reasons)
    blast = build_blast(state, conn_id, assessment.tables, preview)
    rollback = None
    try:
        from app.safety.rollback import build_rollback
        rollback = build_rollback(sql, dialect)
    except Exception:
        rollback = None
    return ToolOutcome(
        result={"ok": True, "verdict": "review", "preview_rows": preview, "needs_confirm": True,
                "reason": reason_text, "reasons": assessment.reasons, "blast": blast, "rollback": rollback},
        card={"tier": "dml", "verdict": "review", "sql": sql, "sub": _sub(sql, dialect),
              "preview_rows": preview, "reason": "需确认后执行", "reasons": assessment.reasons, "blast": blast, "rollback": rollback},
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
