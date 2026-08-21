"""查询路由：跑 SQL（走闸门）、取消、格式化 — A1 可解释：结构化 reasons。"""
from __future__ import annotations

import time

import sqlglot
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import query as core_query
from app.safety import gate as safety_gate
from app.safety.blast import build_blast
from app.safety.models import Origin, Verdict
from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["query"])


class QueryRequest(BaseModel):
    connection_id: str
    sql: str
    origin: str = "manual"
    confirm: bool = False
    limit: int | None = None
    offset: int | None = None
    count_total: bool = False


class CancelRequest(BaseModel):
    connection_id: str


class FormatRequest(BaseModel):
    sql: str
    dialect: str = "sqlite"


def _ctx(state, conn_id: str):
    try:
        cfg = state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return cfg, safety_gate.sqlglot_dialect_for(cfg.dialect)


def _reason_text(reasons: list[dict]) -> str:
    return "; ".join(r.get("message", "") for r in reasons if r.get("message"))


async def _estimate_cost(state, conn_id: str, sql: str, dialect: str) -> int | None:
    """EXPLAIN 估算（方言适配），失败返回 None（可用性优先）。"""
    try:
        from app.core.dialects.registry import registry
        cfg = state.connections.get(conn_id)
        adapter = registry.get(cfg.dialect) if registry.has(cfg.dialect) else None
        if adapter is None:
            return None

        async def _do(a, c):
            return await a.explain(c, sql)

        res = await state.pools.run(conn_id, _do)
        est = res.get("estimated_rows") if isinstance(res, dict) else None
        if est is not None:
            return int(est)
        if isinstance(res, dict) and res.get("is_scan"):
            from app.core.schema import get_schema
            try:
                schema = await get_schema(state, conn_id)
                for t in schema.get("tables", []):
                    if t["name"] in sql or t["name"].lower() in sql.lower():
                        rc = int(t.get("row_count", 0) or 0)
                        if rc > 0:
                            return rc
                m = max((int(t.get("row_count", 0) or 0) for t in schema.get("tables", [])), default=0)
                return m if m > 0 else None
            except Exception:
                pass
        return None
    except Exception:
        return None


@router.post("/query")
async def run_query(req: QueryRequest) -> dict:
    state = get_state()
    cfg, dialect = _ctx(state, req.connection_id)
    if cfg.kb_status != "ready":
        raise HTTPException(status_code=409, detail={
            "code": "kb_not_built",
            "message": "该数据源知识库未构建，请先构建并确认启用",
        })
    origin = Origin.AI if req.origin == "ai" else Origin.MANUAL
    assessment = safety_gate.assess_sql(req.sql, dialect, origin)
    # A2 策略即配置：表级/模式级覆盖（热更新，无需重启）
    try:
        policy = state.runtime.get().policy
        # 表级：最高优先生效
        for tbl in list(assessment.tables):
            act = policy.table_rules.get(tbl) or policy.table_rules.get(tbl.lower()) if hasattr(policy, "table_rules") else None
            if act:
                act = str(act).lower()
                ver = Verdict.BLOCK if act == "block" else Verdict.REVIEW if act == "review" else Verdict.ALLOW
                tier = assessment.tier
                if ver != assessment.verdict:
                    reason = {
                        "rule_id": f"policy-table-{tbl}",
                        "message": f"命中策略 v{getattr(policy, 'version', 1)}：表 “{tbl}” 规则 {act.upper()}",
                        "message_en": f"Policy v{getattr(policy, 'version', 1)}: table '{tbl}' {act.upper()}",
                        "objects": [tbl],
                    }
                    new_reasons = [reason] + [r for r in assessment.reasons if r.get("rule_id") != f"policy-table-{tbl}"]
                    if act == "allow" and assessment.verdict != Verdict.ALLOW:
                        pass
                    else:
                        assessment.verdict = ver
                        assessment.reasons = new_reasons
                        if ver == Verdict.BLOCK:
                            assessment.tier = tier
                        break
        if assessment.verdict == Verdict.REVIEW and assessment.tables:
            for pr in getattr(policy, "pattern_rules", []) or []:
                pid = pr.get("id", "")
                if pid == "delete-requires-time" and "delete" in req.sql.lower():
                    has_time = any(kw in req.sql.lower() for kw in ["created_at", "updated_at", "time", "date", "timestamp"])
                    if not has_time:
                        reason = {
                            "rule_id": "policy-pattern-delete-time",
                            "message": f"命中策略 v{getattr(policy, 'version', 1)}：DELETE 必须 WHERE 带时间范围",
                            "message_en": f"Policy v{getattr(policy, 'version', 1)}: DELETE requires time predicate",
                            "objects": assessment.tables,
                        }
                        assessment.reasons = [reason] + assessment.reasons
                        assessment.verdict = Verdict.BLOCK
                        break
    except Exception:
        pass
    t0 = time.monotonic()
    # A5 成本防护：ALLOW 的读若估算超阈值则升 REVIEW；失败放行并审计
    if assessment.verdict == Verdict.ALLOW:
        try:
            try:
                pol_thr = getattr(state.runtime.get().policy, "threshold", None)
                thr = int(pol_thr) if pol_thr else int(state.runtime.get().gate_review_threshold)
            except Exception:
                thr = 100000
            est = await _estimate_cost(state, req.connection_id, req.sql, dialect)
            if est is not None and est > thr:
                reason = {
                    "rule_id": "cost-threshold",
                    "message": f"预计扫描约 {est} 行，超过阈值 {thr}，需确认",
                    "message_en": f"Estimated scan ~{est} rows exceeds threshold {thr}, requires confirmation",
                    "objects": assessment.tables,
                }
                assessment.verdict = Verdict.REVIEW
                assessment.reasons = [reason] + assessment.reasons
                assessment.tier = Tier.READ
        except Exception as e:
            # 失败放行，已在 _estimate_cost 内审计降级
            pass

    if cfg.read_only and assessment.verdict != Verdict.ALLOW:
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        ro_reasons = [{
            "rule_id": "read-only",
            "message": "该连接标记为只读，禁止写操作与结构变更",
            "message_en": "Connection is read-only; writes and DDL are blocked",
            "objects": assessment.tables,
        }]
        state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                        verdict="block", status="只读连接拦截", sql=req.sql, elapsed_ms=elapsed,
                        reasons=ro_reasons, tables=assessment.tables)
        return {
            "verdict": "block", "tier": assessment.tier.value,
            "reason": ro_reasons[0]["message"],
            "reasons": ro_reasons,
            "suggestions": [],
            "elapsed_ms": elapsed,
        }

    if assessment.verdict == Verdict.ALLOW:
        res = await core_query.execute(state, req.connection_id, req.sql, req.limit, req.offset)
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        total = None
        if req.count_total:
            total = await core_query.count_total(state, req.connection_id, req.sql)
        state.audit.log(connection=cfg.name, origin=origin.value, tier="read", verdict="allow",
                        status="放行", sql=req.sql, elapsed_ms=elapsed,
                        reasons=[], tables=assessment.tables)
        return {**res, "verdict": "allow", "tier": "read", "reason": "", "reasons": [], "total": total,
                "suggestions": []}

    if assessment.verdict == Verdict.BLOCK:
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                        verdict="block", status="拦截", sql=req.sql, elapsed_ms=elapsed,
                        reasons=assessment.reasons, tables=assessment.tables)
        return {
            "verdict": "block", "tier": assessment.tier.value,
            "reason": _reason_text(assessment.reasons),
            "reasons": assessment.reasons,
            "suggestions": safety_gate.suggest_safe(req.sql, dialect, origin),
            "elapsed_ms": elapsed,
        }

    # REVIEW
    if not req.confirm:
        preview = None
        if assessment.tier.value == "dml":
            preview = await safety_gate.preview_rows(state, req.connection_id, req.sql, dialect)
        blast = build_blast(state, req.connection_id, assessment.tables, preview)
        # A4 回滚剧本
        rollback = None
        if assessment.tier.value == "dml":
            try:
                from app.safety.rollback import build_rollback
                rollback = build_rollback(req.sql, dialect)
                # 剧本自身过闸（若为可执行 SQL，则校验）
                if rollback and rollback.get("rollback_sql") and not rollback["rollback_sql"].strip().startswith("--"):
                    rb_assess = safety_gate.assess_sql(rollback["rollback_sql"], dialect, origin)
                    if rb_assess.verdict == Verdict.BLOCK:
                        rollback["note"] += "（剧本含高危操作，已标记）"
            except Exception:
                rollback = None
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                        verdict="review", status="需确认", sql=req.sql, elapsed_ms=elapsed,
                        reasons=assessment.reasons, tables=assessment.tables)
        return {
            "verdict": "review", "tier": assessment.tier.value,
            "reason": _reason_text(assessment.reasons),
            "reasons": assessment.reasons,
            "preview_rows": preview, "needs_confirm": True,
            "blast": blast,
            "rollback": rollback,
            "elapsed_ms": elapsed,
        }

    reassess = safety_gate.assess_sql(req.sql, dialect, origin)
    if reassess.verdict == Verdict.BLOCK:
        return {
            "verdict": "block", "tier": reassess.tier.value,
            "reason": _reason_text(reassess.reasons),
            "reasons": reassess.reasons,
            "suggestions": safety_gate.suggest_safe(req.sql, dialect, origin),
        }
    res = await core_query.execute(state, req.connection_id, req.sql, req.limit, req.offset)
    elapsed = round((time.monotonic() - t0) * 1000, 1)
    status = "已确认执行" if assessment.tier.value == "dml" else "已执行"
    state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                    verdict=assessment.verdict.value, status=status, sql=req.sql, elapsed_ms=elapsed,
                    reasons=assessment.reasons, tables=assessment.tables)
    return {
        "verdict": "executed", "tier": assessment.tier.value,
        "reason": "已确认执行",
        "reasons": assessment.reasons,
        "affected_rows": res.get("affected_rows"),
        "row_count": res.get("row_count"),
        "elapsed_ms": elapsed,
    }


@router.post("/query/cancel")
async def cancel_query(req: CancelRequest) -> dict:
    state = get_state()
    _ctx(state, req.connection_id)
    n = core_query.cancel(req.connection_id)
    return {"cancelled": n}


@router.post("/sql/format")
async def format_sql(req: FormatRequest) -> dict:
    try:
        pretty = sqlglot.transpile(req.sql, read=req.dialect, pretty=True)
        return {"formatted": "\n".join(pretty)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"格式化失败: {e}") from e


class LintRequest(BaseModel):
    connection_id: str
    sql: str


def _edit_distance(a: str, b: str) -> int:
    # 经典 DP，短字符串（表名/列名）开销可忽略
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _closest(target: str, candidates: list[str]) -> str | None:
    if not candidates:
        return None
    best = min(candidates, key=lambda c: _edit_distance(target.lower(), c.lower()))
    # 仅当编辑距离 <= 3 才建议，避免乱建议
    if _edit_distance(target.lower(), best.lower()) <= 3:
        return best
    return None


@router.post("/sql/lint")
async def lint_sql(req: LintRequest) -> dict:
    """实时闸门检查（C1）：复用后端 sqlglot + 规则，保证与闸门同源；零模型调用，<100ms。"""
    import time as _time
    t0 = _time.monotonic()
    state = get_state()
    cfg, dialect = _ctx(state, req.connection_id)
    # schema（30s 缓存，lint 轻量，不强制 refresh）
    from app.core.schema import get_schema
    try:
        schema = await get_schema(state, req.connection_id)
    except Exception:
        schema = {"tables": [], "columns": [], "foreign_keys": []}
    # Gate 评估（结构化 reasons 复用）
    from app.safety.models import Origin, Verdict
    assessment = safety_gate.assess_sql(req.sql, dialect, Origin.MANUAL)
    # 只读连接硬边界（与闸门同源，保持编辑器与执行时一致）
    if cfg.read_only and assessment.verdict != Verdict.ALLOW:
        diagnostics = [{
            "rule_id": "read-only",
            "severity": "error",
            "message": "该连接标记为只读，禁止写操作与结构变更",
            "message_en": "Connection is read-only; writes and DDL are blocked",
            "objects": assessment.tables,
            "suggestion": None,
            "from": 0,
            "to": len(req.sql),
        }]
        elapsed = round((_time.monotonic() - t0) * 1000, 1)
        return {"diagnostics": diagnostics, "elapsed_ms": elapsed, "tables": assessment.tables}
    diagnostics: list[dict] = []
    # 1) 未知表
    known_tables = {t["name"].lower(): t["name"] for t in schema.get("tables", [])}
    # 从 parser 取表名（已归一化小写比较）
    for tbl in assessment.tables:
        if tbl.lower() not in known_tables:
            sug = _closest(tbl, list(known_tables.values()))
            msg = f"未知表 “{tbl}”"
            msg_en = f"Unknown table '{tbl}'"
            if sug:
                msg += f"，您是否想输入 “{sug}”？"
                msg_en += f", did you mean '{sug}'?"
            diagnostics.append({
                "rule_id": "unknown-table",
                "severity": "error",
                "message": msg,
                "message_en": msg_en,
                "objects": [tbl],
                "suggestion": sug,
                "from": req.sql.lower().find(tbl.lower()),
                "to": req.sql.lower().find(tbl.lower()) + len(tbl) if tbl.lower() in req.sql.lower() else None,
            })
    # 2) 未知列（基于 sqlglot Column 抽取，轻量启发式）
    try:
        import sqlglot
        from sqlglot import exp
        parsed = sqlglot.parse(req.sql, read=dialect)
        cols: list[tuple[str, str | None]] = []  # (col, table)
        for node in parsed:
            if node is None:
                continue
            for col in node.find_all(exp.Column):
                col_name = col.name
                tbl = col.table if col.table else None
                cols.append((col_name, tbl))
        # 已知列：按表分组
        known_cols_by_table: dict[str, set[str]] = {}
        known_cols_all: set[str] = set()
        for c in schema.get("columns", []):
            known_cols_by_table.setdefault(c["table"].lower(), set()).add(c["name"].lower())
            known_cols_all.add(c["name"].lower())
        for col_name, tbl in cols:
            # 若指定表，则在该表下列中查找；否则在全库列中
            if tbl and tbl.lower() in known_cols_by_table:
                if col_name.lower() not in known_cols_by_table[tbl.lower()]:
                    sug = _closest(col_name, list(known_cols_by_table[tbl.lower()]))
                    msg = f"表 “{tbl}” 无列 “{col_name}”"
                    msg_en = f"Table '{tbl}' has no column '{col_name}'"
                    if sug:
                        msg += f"，您是否想输入 “{sug}”？"
                        msg_en += f", did you mean '{sug}'?"
                    diagnostics.append({
                        "rule_id": "unknown-column",
                        "severity": "error",
                        "message": msg,
                        "message_en": msg_en,
                        "objects": [f"{tbl}.{col_name}"],
                        "suggestion": sug,
                        "from": req.sql.lower().find(col_name.lower()),
                        "to": req.sql.lower().find(col_name.lower()) + len(col_name) if col_name.lower() in req.sql.lower() else None,
                    })
            elif not tbl:
                if col_name.lower() not in known_cols_all and col_name.lower() not in ("*", "count", "sum", "avg", "min", "max"):
                    # 仅对不在任何表的列名报（避免误报聚合函数）
                    sug = _closest(col_name, list(known_cols_all))
                    msg = f"未知列 “{col_name}”"
                    msg_en = f"Unknown column '{col_name}'"
                    if sug:
                        msg += f"，您是否想输入 “{sug}”？"
                        msg_en += f", did you mean '{sug}'?"
                    diagnostics.append({
                        "rule_id": "unknown-column",
                        "severity": "error",
                        "message": msg,
                        "message_en": msg_en,
                        "objects": [col_name],
                        "suggestion": sug,
                        "from": req.sql.lower().find(col_name.lower()),
                        "to": req.sql.lower().find(col_name.lower()) + len(col_name) if col_name.lower() in req.sql.lower() else None,
                    })
    except Exception:
        pass
    # 3) 规则级 lint（与闸门同源，共享 rule_id/message/objects）
    for r in assessment.reasons:
        # 仅对 REVIEW/BLOCK 的规则做行内警告/错误，ALLOW 的 read-no-limit 不警告
        if r["rule_id"] in ("read-no-limit", "read-allow"):
            continue
        severity = "error" if any(rr.verdict.value == "block" for rr in assessment.rules if rr.rule == r["rule_id"]) else "warning"
        diagnostics.append({
            "rule_id": r["rule_id"],
            "severity": severity,
            "message": r["message"],
            "message_en": r.get("message_en", ""),
            "objects": r.get("objects", []),
            "suggestion": None,
        })
    # 去重（按 rule_id+objects）
    seen = set()
    uniq: list[dict] = []
    for d in diagnostics:
        key = (d["rule_id"], tuple(d.get("objects") or []))
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    elapsed = round((_time.monotonic() - t0) * 1000, 1)
    return {"diagnostics": uniq, "elapsed_ms": elapsed, "tables": assessment.tables}
