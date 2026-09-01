"""SQL 类原子工具：run_query / run_dml / draft_ddl（写与改结构均过闸门，执行权在人）。"""
from __future__ import annotations

import logging
import time

from app.ai.tools.registry import ToolOutcome, _get_ctx, _sub, register_tool
from app.safety import gate as safety_gate
from app.safety.blast import build_blast
from app.safety.models import Origin, Verdict

logger = logging.getLogger("ai.tools.sql")

# R1/P1-4：图校验失败计数——60s 窗口内连续失败 >2 次降级放行防死循环；
# 窗口外（跨轮）或校验通过（成功）即清零，不跨会话累计。
_plausibility_fail_count: dict[str, tuple[int, float]] = {}
_PLAUSIBILITY_WINDOW = 60.0


def _plausibility_fails(conn_id: str) -> int:
    now = time.time()
    c, ts = _plausibility_fail_count.get(conn_id, (0, 0.0))
    return c if now - ts <= _PLAUSIBILITY_WINDOW else 0


def _bump_plausibility_fail(conn_id: str) -> int:
    now = time.time()
    c, ts = _plausibility_fail_count.get(conn_id, (0, 0.0))
    c = c + 1 if now - ts <= _PLAUSIBILITY_WINDOW else 1
    _plausibility_fail_count[conn_id] = (c, now)
    return c


def plausibility_check(state: Any, conn_id: str, sql: str) -> tuple[bool, list[tuple[str, str, str, str]], bool]:
    """R1 图校验（P2-11：run_query 与 report 子查询共用）：JOIN 必须命中知识库图。

    返回 (ok, miss, degraded)：
    - mock 演示跳过；KB 未构建/加载失败不拦（宁缺勿错）
    - 60s 窗口连续失败 >2 次 → 降级放行（防死循环）；成功即清零
    """
    try:
        from app.ai.gateway import is_effective_mock
        _rt = getattr(state, "runtime", None)
        _is_mock = False
        try:
            _is_mock = bool(_rt and is_effective_mock(_rt.get().provider_config()))
        except Exception:
            _is_mock = False
        if _is_mock:
            return True, [], False
        from app.knowledge.graph.sql_joins import extract_join_pairs
        pairs = extract_join_pairs(sql)
        if not pairs:
            return True, [], False
        _conn_state = getattr(state, "knowledge", None)
        miss: list[tuple[str, str, str, str]] = []
        for ft, fc, tt, tc in pairs:
            try:
                ok = _conn_state.validate_join(conn_id, ft, fc, tt, tc)
            except Exception:
                ok = True  # KB 未构建/加载失败：不拦（宁缺勿错）
            if not ok:
                miss.append((ft, fc, tt, tc))
        if not miss:
            # P1-4：校验通过即清零（成功恢复严格，不跨轮累计）
            _plausibility_fail_count.pop(conn_id, None)
            return True, [], False
        if _plausibility_fails(conn_id) >= 2:
            # 同 turn 连续失败 >2 次：降级放行（防死循环），记 warning
            logger.warning("[plausibility] conn=%s 连续 %d 次图校验失败，降级放行：%s",
                           conn_id, _bump_plausibility_fail(conn_id), sql[:120])
            return True, [], True
        _bump_plausibility_fail(conn_id)
        return False, miss, False
    except Exception as e:  # pragma: no cover - 校验器自身异常不阻塞查询
        logger.warning("[plausibility] conn=%s 图校验异常，跳过：%s", conn_id, e)
        return True, [], False


def _decodify_sql(state, conn_id, sql: str) -> str:
    """B3 修复：模型拿到代号 schema 产出的 SQL 需先还原再过闸门/执行。无映射时原样返回。"""
    try:
        from app.safety.codify import decodify_text
        from app.config import get_env
        return decodify_text(get_env().data_dir, conn_id, sql)
    except Exception:
        return sql


def _codify_columns_for_model(state, conn_id, columns: list[str], table_hint: str = "") -> list[str]:
    """T6.2 回喂代号化：仅对敏感表的列名做代号化，模型可见世界恒代号。"""
    try:
        from app.safety.codify import codify_column, is_sensitive_table
        from app.config import get_env
        dd = get_env().data_dir
        out: list[str] = []
        for col in columns:
            # 需判断列所属表是否敏感；table_hint 为空则尝试按列名本身判断（列级不依赖表时仍可 codify）
            sensitive = False
            if table_hint:
                try:
                    sensitive = is_sensitive_table(state, conn_id, table_hint)
                except Exception:
                    sensitive = False
            if sensitive:
                try:
                    out.append(codify_column(dd, conn_id, table_hint, col))
                    continue
                except Exception:
                    pass
            # 若 table_hint 未命中，尝试全局列级映射（兼容旧数据：col 作为 key 的后缀匹配）
            # 此时不强制 codify，保持原文（非敏感列不代号）
            out.append(col)
        return out
    except Exception:
        return columns


def _codify_result_for_model(state, conn_id, result: dict, table_hint: str = "") -> dict:
    """将 tool result 的 columns 代号化后返回新 dict（不污染 card）。"""
    cols = result.get("columns")
    if not cols or not isinstance(cols, list):
        return result
    try:
        codified = _codify_columns_for_model(state, conn_id, cols, table_hint)
        if codified != cols:
            new_result = dict(result)
            new_result["columns"] = codified
            return new_result
    except Exception:
        pass
    return result

async def _run_query(state, args, conn_id, include_data=False):
    _cfg, dialect = _get_ctx(state, conn_id)
    sql = args.get("sql", "")
    # S3-1：LLM 顺带产出的可选追加项建议（不参与执行，透传到 card 供前端渲染）
    options = args.get("options") or []
    if options and isinstance(options, list):
        options = [o for o in options if isinstance(o, dict)
                   and o.get("label")][:3]
    else:
        options = []
    # B3: 代号还原后进闸门
    sql = _decodify_sql(state, conn_id, sql)
    # R6/T8：会话变量替换 + 表级过滤器注入（闸门/卡片/审计用真实执行 SQL）
    try:
        from app.knowledge.filters import prepare_query_sql
        sql = prepare_query_sql(state, conn_id, sql)
    except Exception:
        pass
    # R1/R15：图校验器（plausibility gate，§10⑤）——JOIN 必须命中知识库图，
    # 否则打回重写（非安全 BLOCK；同 turn 连续失败降级放行防死循环；mock 演示不拦）
    _ok, _miss, _degraded = plausibility_check(state, conn_id, sql)
    if not _ok:
        from app.knowledge.graph.traverse import path_strings
        desc = "、".join(f"{ft}.{fc} = {tt}.{tc}" for ft, fc, tt, tc in _miss)
        hint = (f"JOIN 条件 {desc} 不在知识库图内（幻觉 join）。"
                f"请用 graph_read 确认表间真实关联后重写；当前图内路径供参考：")
        try:
            _paths = path_strings(
                state.knowledge.graph(conn_id).get("edges", []),
                {_miss[0][0], _miss[0][2]}, hops=2)
            hint += "；".join(_paths[:3]) if _paths else "（无候选路径）"
        except Exception:
            pass
        return ToolOutcome(
            result={"ok": False, "verdict": "block", "reason": "JOIN 不在知识库图内",
                    "error": "JOIN 不在知识库图内", "hint": hint, "plausibility": _miss},
            card={"tier": "read", "verdict": "block", "sql": sql,
                  "sub": _sub(sql, dialect), "reason": "JOIN 不在知识库图内",
                  "reason_detail": hint},
            think="图校验未通过：该 JOIN 不在知识库图内，提示模型重写。",
        )
    assessment = safety_gate.assess_configured(state, sql, dialect, Origin.AI)
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
        try:
            res = await run_query(state, conn_id, sql)
        except Exception as e:
            err = str(e)
            low = err.lower()
            if any(k in low for k in ("no such table", "no such column", "doesn't exist", "unknown column", "no such table:")):
                # T5.2 有界纠错：注入全表清单一次，供模型重写
                try:
                    from app.core.schema import get_schema as _gs
                    _schema = await _gs(state, conn_id)
                    _all_tables = [t.get("name", "") for t in _schema.get("tables", []) if t.get("name")]
                except Exception:
                    _all_tables = assessment.tables or []
                msg = f"SQL 引用了候选之外的表或列（{err}），以下是全部表清单，请重写：{', '.join(_all_tables[:20])}"
                return ToolOutcome(
                    result={"ok": False, "error": err, "available_tables": _all_tables, "hint": msg},
                    card={"tier": "read", "verdict": "allow", "sql": sql, "sub": _sub(sql, dialect), "reason": msg},
                    think="有界纠错：已注入全表清单，待模型重写。",
                )
            raise
        # R7/T10：查询成功回灌（边加权 + few-shot）；失败静默，不影响查询返回
        try:
            question = None
            try:
                from app.ai.tools.registry import get_active_session
                _sid = get_active_session()
                if _sid:
                    for _m in reversed(state.chats.get_messages(_sid) or []):
                        if _m.get("role") == "user" and (_m.get("content") or "").strip():
                            question = _m["content"]
                            break
            except Exception:
                question = None
            state.knowledge.record_query_success(conn_id, sql, question)
        except Exception:
            pass
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
                    # B2 fail-closed：脱敏异常时丢弃行数据而非明文直发
                    redacted_rows = []
                    redactions = ["redact-failed"]
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
        # T6.2 回喂代号化：喂模型的 result columns 按敏感表代号化（卡片保持真名）
        tbl_hint = assessment.tables[0] if assessment.tables else ""
        result_for_model = _codify_result_for_model(state, conn_id, result, tbl_hint)
        # 卡片带完整结果（含 rows 供前端直接渲染，消除同 SQL 二次执行）；喂模型的 result 为脱敏后+代号化
        card = {
"tier": "read", "verdict": "allow", "sql": sql, "sub": _sub(sql, dialect),
            "options": options,  # S3-1：LLM 可选追加项建议（前端渲染 chips，用户点一下织入）
            "result": {
                "columns": res["columns"], "types": res["types"],
                "rows": res["rows"], "row_count": res["row_count"],
                "truncated": res["truncated"], "elapsed_ms": res["elapsed_ms"],
            },
        }
        # R3/§10⑥：空结果反馈（不阻断，提示模型自检口径）
        if res.get("row_count", 0) == 0:
            card["empty_hint"] = ("查询返回 0 行：可能是过滤条件过严或时间范围过窄，"
                                  "请结合上下文核对口径后决定是否放宽重查。")
        # 审计：对话内部真实读必须留痕（T6.3 记真名 + 与清单代号的对应）
        _codified_map: dict[str, str] = {}
        _codified = False
        try:
            from app.safety.codify import codify_table, is_sensitive_table
            from app.config import get_env
            dd = get_env().data_dir
            for tbl in assessment.tables or []:
                try:
                    if is_sensitive_table(state, conn_id, tbl):
                        code = codify_table(dd, conn_id, tbl)
                        _codified_map[code] = tbl
                        _codified = True
                except Exception:
                    continue
        except Exception:
            pass
        state.audit.log(
            connection=_cfg.name, origin="ai", tier="read", verdict="allow",
            status="对话内读（循环内测量）", sql=sql,
            elapsed_ms=res.get("elapsed_ms"), source="loop_internal",
            tables=assessment.tables,
            codified=_codified,
            codify_map=_codified_map,
        )
        return ToolOutcome(
            result=result_for_model,
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
    sql = _decodify_sql(state, conn_id, sql)
    # P2-14：会话变量替换 + 过滤器注入（与确认执行同一加工链，preview/闸门/影响行数一致）
    try:
        from app.knowledge.filters import prepare_query_sql
        sql = prepare_query_sql(state, conn_id, sql)
    except Exception:
        pass
    assessment = safety_gate.assess_configured(state, sql, dialect, Origin.AI)
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
    sql = _decodify_sql(state, conn_id, sql)
    return ToolOutcome(
        result={"ok": True, "note": "DDL 脚本已生成，发送到编辑器手动执行。AI 不执行 DDL。"},
        card={"tier": "ddl", "verdict": "manual", "sql": sql, "sub": "DDL · manual only"},
        think="生成 DDL 脚本（仅草稿，不执行）。",
    )


def register() -> None:
    register_tool(
        "run_query",
        "执行只读查询。结果只会返回列名与行数；如需明细需用户 opt-in。"
        "生成 SQL 时请自查：对照知识库中该表的已确认规则（过滤条件/常量）与分页纪律"
        "（查询应带 LIMIT，默认上限 1000，勿全表扫描）；若你的 SQL 未体现某条规则且"
        "用户未明确表示不需要，在 options 参数中提供可追加项（最多 3 条），由用户决定是否采用。"
        "options 仅作建议，不影响 SQL 执行。",
        {"sql": {"type": "string", "description": "只读 SELECT SQL"},
         "options": {"type": "array", "items": {"type": "object",
                     "properties": {"id": {"type": "string"},
                                    "label": {"type": "string", "description": "用户可见的追加项文案，如「补充租户ID过滤」"},
                                    "hint": {"type": "string", "description": "给改写调用的提示，如「该表一般需带 tenant_id = :current_tenant」"}}},
                     "description": "可选追加项建议（S3：用户点一下→轻量 LLM 改写 SQL）"}},
        ["sql"],
        _run_query,
        trust="readonly",
    )
    register_tool(
        "run_dml",
        "执行写操作（INSERT/UPDATE/DELETE）。必须先评估影响行数并需用户确认，绝不自动执行。",
        {"sql": {"type": "string", "description": "DML SQL"}},
        ["sql"],
        _run_dml,
        trust="mutating",
        confirm="card",
    )
    register_tool(
        "draft_ddl",
        "生成 DDL 脚本（CREATE/ALTER/DROP 等）草稿，发送到编辑器由用户手动执行。绝不执行。",
        {"sql": {"type": "string", "description": "DDL SQL 脚本"}},
        ["sql"],
        _draft_ddl,
        trust="readonly",
    )
