"""报告模式管线：澄清 → 计划 → 逐章执行 → 汇总成文（引擎合一后为纯管线非循环）。

- 三次单发 LLM 调用（澄清/规划/成文）+ 直接执行 SQL（不经工具循环）；逐章 SQL
  由 LLM 在规划阶段直接产出，物理上无写路径（闸门 + 只读检查每章照过）。
- 澄清流：欠定义时 yield 一个 clarify 事件后结束当前 SSE（done）；前端答完
  用新一次请求把完整"报告历史"（含已答的澄清）重传，后端 replay 到断点继续。
- 逐章查询每条都过安全闸门 + 写审计（挂 report_id），与手动查询一样可回溯。
- 快照：report_start 携带 snapshot_ts；该报告所有子审计挂同一 report_id。

隐私红线：图表数据只传聚合结果（GROUP BY 后统计值）；run_query 工具默认只回列名+行数，
报告模式**强制 include_data=True** 以拿到聚合数据集喂模型写叙述——但聚合优先，明细依旧不回。
"""
from __future__ import annotations

import json
import time
from app.core.timeutil import utcnow_iso
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.ai import gateway as gw
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.context import (
    assemble_context_full,
    report_system_prompt,
)
from app.ai.manifest import build_manifest
from app.ai.dto import ChatRequest
from app.safety import gate as safety_gate
from app.safety.models import Origin, Verdict

if TYPE_CHECKING:
    from app.state import AppState

CLARIFY_MAX_QUESTIONS = 3    # 一轮澄清最多 3 个问题


def _normalize_messages(messages: list) -> list[dict]:
    # loop 的同名助手是唯一实现（测试与 harness 共用）；函数级导入规避 loop→report 循环依赖
    from app.ai.loop import _normalize_messages as _impl
    return _impl(messages)


def _last_user_text(messages: list[dict]) -> str:
    from app.ai.loop import _last_user_text as _impl
    return _impl(messages)


def _extract_clarify_answers(messages: list[dict]) -> list[dict]:
    """从消息历史里提取已经回答过的澄清问题（report 模式前端重传的历史含这些）。

    约定：澄清问答表现为连续的 system 'clarify'（提问）+ user（回答）对。
    缺失则视为尚未澄清。
    """
    ans: list[dict] = []
    i = 0
    msgs = messages
    while i < len(msgs) - 1:
        m = msgs[i]
        nxt = msgs[i + 1]
        if m.get("role") == "system" and m.get("name") == "clarify" and nxt.get("role") == "user":
            ans.append({
                "question": m.get("content", ""),
                "answer": nxt.get("content", ""),
            })
            i += 2
            continue
        i += 1
    return ans


def _server_history_to_report_messages(rows: list[dict]) -> list[dict]:
    """chat.db 服务端历史 → report_stream 期望的消息格式（历史统一：服务端为唯一来源）。

    转换规则：
    - kind=clarify（assistant 澄清问题）→ {"role":"system","name":"clarify"}，
      与其后第一条 user 回答配对（前端重放语义：多个澄清共享一次作答）
    - assistant text → assistant（供 manifest 历史计数/脱敏）
    - think/stage/sql_card/report 工件行不进报告上下文（与前端回放等价）
    """
    out: list[dict] = []
    pending_clarifies: list[str] = []
    for r in rows:
        role = r.get("role")
        kind = r.get("kind") or "text"
        content = r.get("content") or ""
        if role == "user":
            for q in pending_clarifies:
                out.append({"role": "system", "name": "clarify", "content": q})
            pending_clarifies = []
            if content:
                out.append({"role": "user", "content": content})
        elif role == "assistant":
            if kind == "clarify":
                pending_clarifies.append(content)
            elif kind == "text" and content:
                out.append({"role": "assistant", "content": content})
    for q in pending_clarifies:
        out.append({"role": "system", "name": "clarify", "content": q})
    return out


# ---- mock 报告计划：无 key 也能跑通，确定性按关键词规划章节 ----
def _mock_plan(question: str) -> list[dict]:
    """mock 下按问题关键词拼一个 2~3 章的确定计划。每章一个聚合查询 + chart_hint。"""
    q = question.lower()
    # 默认：销售分析
    sections = [
        {
            "id": "r1",
            "title": "销售总览",
            "intent": "按月统计订单数与销售额",
            "chart_hint": "line",
            "depends_on_chapter": None,
            "sql": (
                "SELECT strftime('%Y-%m', o.created_at) AS month, "
                "COUNT(DISTINCT o.id) AS orders, "
                "ROUND(SUM(o.total_amount), 2) AS revenue "
                "FROM orders o GROUP BY month ORDER BY month"
            ),
        },
        {
            "id": "r2",
            "title": "渠道占比",
            "intent": "按营销渠道统计销售额",
            "chart_hint": "pie",
            "depends_on_chapter": None,
            "sql": (
                "SELECT c.name AS campaign, ROUND(SUM(o.total_amount), 2) AS revenue "
                "FROM orders o LEFT JOIN campaigns c ON c.id = o.campaign_id "
                "GROUP BY c.id ORDER BY revenue DESC"
            ),
        },
    ]
    if any(k in q for k in ("退货", "return", "退款")):
        sections.append({
            "id": "r3",
            "title": "退货表现",
            "intent": "退货率最高的商品",
            "chart_hint": "bar",
            "depends_on_chapter": "r1",
            "sql": (
                "SELECT p.product_name, COUNT(*) AS orders, COUNT(r.id) AS returns, "
                "ROUND(COUNT(r.id) * 100.0 / COUNT(*), 1) AS return_rate "
                "FROM products p JOIN order_items oi ON oi.product_id = p.id "
                "LEFT JOIN returns r ON r.order_item_id = oi.id "
                "GROUP BY p.id ORDER BY return_rate DESC LIMIT 8"
            ),
        })
    elif any(k in q for k in ("库存", "inventory", "周转", "积压")):
        sections.append({
            "id": "r3",
            "title": "库存周转",
            "intent": "滞销商品清单",
            "chart_hint": "bar",
            "depends_on_chapter": None,
            "sql": (
                "SELECT p.product_name, c.name AS category, i.qty AS stock, i.last_moved_at "
                "FROM inventory i JOIN products p ON p.id = i.product_id "
                "JOIN categories c ON c.id = p.category_id "
                "ORDER BY i.last_moved_at ASC LIMIT 8"
            ),
        })
    else:
        sections.append({
            "id": "r3",
            "title": "TOP 客户",
            "intent": "按消费额排名的客户",
            "chart_hint": "bar",
            "depends_on_chapter": None,
            "sql": (
                "SELECT cu.name, ROUND(SUM(o.total_amount), 2) AS revenue "
                "FROM orders o JOIN customers cu ON cu.id = o.customer_id "
                "GROUP BY cu.id ORDER BY revenue DESC LIMIT 6"
            ),
        })
    return sections


def _mock_clarify(question: str) -> list[dict]:
    """mock 下欠定义才问：未给时间范围/口径就问一句。"""
    q = question.lower()
    qs: list[dict] = []
    if not any(k in q for k in ("q1", "q2", "q3", "q4", "2026", "本月", "上月", "本月", "本季")):
        qs.append({"field": "time_range", "question": "你要分析的时间范围是？（如 2026 全年 / Q3 / 上月）"})
    if len(qs) == 0 and not any(k in q for k in ("销售", "退货", "库存", "客户", "收入", "revenue")):
        qs.append({"field": "scope", "question": "报告聚焦哪个业务域？（销售 / 退货 / 库存 / 客户）"})
    return qs[:CLARIFY_MAX_QUESTIONS]


def _summarize_results(rows: list, columns: list, limit: int = 12) -> str:
    """把聚合查询结果摊成一个紧凑文本块喂模型写叙述。只前 limit 行，防 token 爆炸。"""
    if not rows:
        return "（无数据）"
    head = rows[:limit]
    lines = [" | ".join(str(c) for c in columns)]
    for r in head:
        lines.append(" | ".join(str(v) for v in r))
    if len(rows) > limit:
        lines.append(f"...（共 {len(rows)} 行，已截断至前 {limit}）")
    return "\n".join(lines)


async def _run_report_query(
    state: "AppState", conn_id: str, report_id: str, sql: str, result_id: str
) -> dict[str, Any]:
    """执行一章的只读查询：过闸门 → 执行 → 写审计（挂 report_id）→ 返回数据集。

    这是报告模式与手动查询的审计对齐点：每章子查询照常进审计，可由 report_id 回溯。
    报告子查询标 source="report"，与对话循环内读(source=loop_internal)/手动执行区分来源。
    """
    cfg = state.connections.get(conn_id)
    dialect = safety_gate.sqlglot_dialect_for(cfg.dialect)
    # R6/T8：会话变量替换 + 表级过滤器注入（闸门/审计用真实执行 SQL）
    try:
        from app.knowledge.filters import prepare_query_sql
        sql = prepare_query_sql(state, conn_id, sql)
    except Exception:
        pass
    # R1/P2-11：图校验（与 run_query 同一校验器）——report 子查询 JOIN 也必须命中知识库图
    try:
        from app.ai.tools.sql import plausibility_check
        _ok, _miss, _ = plausibility_check(state, conn_id, sql)
        if not _ok:
            _desc = "、".join(f"{ft}.{fc} = {tt}.{tc}" for ft, fc, tt, tc in _miss)
            _reason = f"JOIN 条件 {_desc} 不在知识库图内（幻觉 join）"
            try:
                state.audit.log(connection=cfg.name, origin=Origin.AI.value, tier="read",
                                verdict="block", status="报告查询拦截（图校验）", sql=sql,
                                report_id=report_id, source="report", tables=None)
            except Exception:
                pass
            return {"ok": False, "result_id": result_id, "sql": sql,
                    "reason": _reason, "plausibility": _miss}
    except Exception:
        pass  # 校验器异常不阻塞报告（与 run_query 一致）
    assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
    # 报告只读：闸门非 ALLOW 直接转 block 记录（工具集本就只读，理论不会出现写）
    if assessment.verdict != Verdict.ALLOW:
        state.audit.log(connection=cfg.name, origin=Origin.AI.value, tier=assessment.tier.value,
                        verdict="block", status="报告查询拦截", sql=sql, report_id=report_id,
                        source="report", reasons=assessment.reasons, tables=assessment.tables)
        return {"ok": False, "result_id": result_id, "sql": sql,
                "reason": "; ".join(r.get("message", "") for r in assessment.reasons),
                "reasons": assessment.reasons}
    from app.core.query import execute as run_query

    # B4 严格档：行数据不出网（include_data 硬拦在 sql.py B4 实现），报告聚合的 include_data=true 已由 report 模式保证
    try:
        _mode = state.runtime.get().privacy_mode
    except Exception:
        _mode = "standard"
    if _mode == "strict":
        # 严格模式下报告查询仍执行供卡片展示，后续 _llm_narration 正常调用
        pass

    res = await run_query(state, conn_id, sql, max_rows=200)   # 报告聚合，200 行足够
    # B2 脱敏：报告聚合行在出网前脱敏（标准档 token 化，开放档明文，严格档仍 token 化作为 defense-in-depth）
    redacted_rows = res["rows"]
    _redactions: list[str] = []
    if res.get("rows"):
        if _mode != "open":
            try:
                from app.safety.redact import get_salt as _grs, redact_rows as _rr
                from app.config import get_env as _ge
                _salt = _grs(_ge().data_dir)
                try:
                    _sens = state.connections.get(conn_id).sensitive
                except Exception:
                    _sens = []
                _tbl = assessment.tables[0] if assessment.tables else ""
                redacted_rows, _mp = _rr(res["rows"], res["columns"], _tbl, _salt, _sens)
                _redactions = list(_mp.keys())[:5]
            except Exception:
                # fail-closed
                redacted_rows = []
                _redactions = ["redact-failed"]
    state.audit.log(connection=cfg.name, origin=Origin.AI.value, tier="read", verdict="allow",
                    status="报告查询", sql=sql, elapsed_ms=res.get("elapsed_ms"),
                    report_id=report_id, source="report")
    # 存原始 rows 供前端图表/卡片展示，redacted_rows 供模型
    return {
        "ok": True,
        "result_id": result_id,
        "sql": sql,
        "columns": res["columns"],
        "types": res["types"],
        "rows": res["rows"],
        "redacted_rows": redacted_rows,
        "redactions": _redactions,
        "row_count": res["row_count"],
        "elapsed_ms": res["elapsed_ms"],
    }


async def report_stream(state: "AppState", req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    """报告流 generator。前端持有中间态；后端无状态，靠 messages 历史 replay。"""
    provider = gw.build_provider(resolve_provider_cfg(state, req))
    conn_id = req.connection_id
    report_id = f"rep_{uuid.uuid4().hex[:14]}"
    snapshot_ts = utcnow_iso()

    user_text = _last_user_text(_normalize_messages(req.messages))
    # B2: 报告请求的用户文本亦脱敏
    _report_text_redactions: list[str] = []
    try:
        from app.safety.redact import get_salt as _rgrs, redact_text as _rrt
        from app.config import get_env as _rge
        _rsalt = _rgrs(_rge().data_dir)
        try:
            _rsens = state.connections.get(conn_id).sensitive
        except Exception:
            _rsens = []
        _rtext, _rmp = _rrt(user_text, _rsalt, _rsens)
        if _rmp:
            _report_text_redactions = list(_rmp.keys())[:5]
            user_text = _rtext
    except Exception:
        pass
    context, meta = await assemble_context_full(state, conn_id, req.table, user_text)

    # 历史统一：服务端历史优先（stream() 已加载并压缩）；前端回放仅作无历史时的兼容通道
    _server_hist = getattr(req, "_server_history", None) or []
    if _server_hist:
        report_msgs = _server_history_to_report_messages(_server_hist)
        _new_q = _last_user_text(_normalize_messages(req.messages))
        if _new_q and (not report_msgs or report_msgs[-1].get("role") != "user"
                       or report_msgs[-1].get("content") != _new_q):
            report_msgs.append({"role": "user", "content": _new_q})
    else:
        report_msgs = _normalize_messages(req.messages)

    # B1 出网清单（报告模式：强制 include_data=true，聚合结果）
    try:
        provider_cfg_for_manifest = resolve_provider_cfg(state, req)
        # 历史消息同样脱敏
        _hist_norm = [dict(m) for m in report_msgs]
        try:
            from app.safety.redact import get_salt as _hgs, redact_text as _hrt
            from app.config import get_env as _hge
            _hsalt = _hgs(_hge().data_dir)
            try:
                _hsens = state.connections.get(conn_id).sensitive
            except Exception:
                _hsens = []
            for _hm in _hist_norm:
                if _hm.get("role") == "user" and _hm.get("content"):
                    _hrc, _hmp = _hrt(_hm["content"], _hsalt, _hsens)
                    if _hmp:
                        for _hk in _hmp.keys():
                            if _hk not in _report_text_redactions and len(_report_text_redactions) < 5:
                                _report_text_redactions.append(_hk)
                        _hm["content"] = _hrc
        except Exception:
            pass
        # 报告的 messages 包含历史 + 当前上下文
        _msgs_for_manifest = [
            {"role": "system", "content": report_system_prompt()},
            {"role": "system", "content": context},
            *_hist_norm,
        ]
        manifest = build_manifest(state, conn_id, meta, _msgs_for_manifest, True, provider_cfg_for_manifest, context)
        if _report_text_redactions:
            manifest["redactions"] = _report_text_redactions[:5]
        # 出网清单审计改由中央记账拦截器（gateway）按每次 LLM 调用统一写；SSE 的 manifest 事件仍下发
    except Exception:
        # B1 修复：兜底 manifest 亦需读取真实 privacy_mode 而非硬编码 standard
        try:
            _fallback_mode = state.runtime.get().privacy_mode
        except Exception:
            _fallback_mode = "standard"
        manifest = {"tables": meta.get("candidate_tables") or [], "kb_docs": meta.get("kb_docs", 0), "history_turns": len(req.messages), "include_data": True, "redactions": [], "mode": _fallback_mode, "ts": utcnow_iso(), "model": "", "provider": "mock"}
    # 中央记账 ctx（gateway 拦截器按每次 LLM 调用写；叙述轮带 include_data=True）
    try:
        _rc_reds = _report_text_redactions[:5]
    except Exception:
        _rc_reds = []
    _report_ctx = {
        "conn_id": conn_id, "connection": conn_id, "skill": "report",
        "session_id": req.session_id, "source": "egress", "status": "egress",
        "include_data": False, "redactions": _rc_reds, "context_meta": meta,
        "manifest": manifest,
    }
    yield {"type": "report_start", "connection": conn_id,
           "report_id": report_id, "snapshot_ts": snapshot_ts}
    yield {"type": "manifest", "manifest": manifest}

    # ---- 1) 澄清：若历史里没有澄清问答且问题欠定义 → yield clarify 后结束本轮 ----
    answered = _extract_clarify_answers(report_msgs)
    is_mock = resolve_provider_cfg(state, req)["provider"] == "mock"

    need_clarify: list[dict] = []
    if len(answered) == 0 and not any(
        m.get("name") == "clarify" for m in report_msgs
    ):
        need_clarify = _mock_clarify(user_text) if is_mock else await _llm_clarify(provider, user_text, ctx=_report_ctx)

    if need_clarify:
        # 把澄清问题作为一条带 name=clarify 的 system 消息记下（前端答完回传时凭借它判断 replay）
        _cl_ev: list[dict] = []
        for q in need_clarify:
            ev = {"type": "clarify", "origin": "report", "question": q["question"], "field": q.get("field", "")}
            yield ev
            _cl_ev.append(ev)
        # 澄清问题必须先落库（用户回答的下一轮依赖服务端历史重建问答对）
        from app.ai.events_codec import ai_messages_from_events as _amfe

        yield {"type": "_commit", "messages": _amfe(_cl_ev)}
        yield {"type": "done"}
        return

    # ---- 2) 计划：mock 下确定性 → plan 事件 ----
    plan = _mock_plan(user_text) if is_mock else await _llm_plan(provider, user_text, context, ctx=_report_ctx)
    if not plan:
        # 真实路径规划失败：报错而非生成空壳报告（零定制，不注入演示数据）
        yield {"type": "error", "message": "报告章节规划失败，请稍后重试或换个问法。"}
        yield {"type": "done"}
        return
    yield {"type": "plan", "sections": plan}

    # ---- 3) 逐章执行：每章过闸门+审计 → section 事件（含聚合数据） ----
    section_results: list[dict] = []
    for sec in plan:
        rid = sec["id"]
        qr = await _run_report_query(state, conn_id, report_id, sec["sql"], rid)
        if not qr.get("ok"):
            yield {"type": "section", "id": rid, "title": sec["title"],
                   "result_id": rid, "ok": False, "reason": qr.get("reason"),
                   "sql": sec["sql"], "chart": {"kind": sec.get("chart_hint", "bar"), "data": []},
                   "rows": [], "columns": [], "elapsed_ms": None}
            continue
        section_results.append(qr)
        yield {
            "type": "section",
            "id": rid,
            "title": sec["title"],
            "intent": sec.get("intent", ""),
            "result_id": rid,
            "ok": True,
            "sql": sec["sql"],
            "chart": {"kind": sec.get("chart_hint", "bar"), "data": qr["rows"], "columns": qr["columns"]},
            "rows": qr["rows"],
            "columns": qr["columns"],
            "types": qr["types"],
            "row_count": qr["row_count"],
            "elapsed_ms": qr["elapsed_ms"],
        }

    # ---- 4) 汇总成文：把各章聚合数据集喂模型，产出引用 result_id 的叙述 ----
    narration = _mock_narration(user_text, plan, section_results) if is_mock \
        else await _llm_narration(provider, user_text, plan, section_results, ctx={**_report_ctx, "include_data": True})
    # 一个 narration 事件叙述整份报告的结论（含各章数字 + 来源标注）
    yield {"type": "narration", "section_id": None, "text": narration,
           "refs": _collect_refs(plan, section_results)}

    # ---- 5) 报告落库（results.db，source='chat'）：正文 markdown，失败静默不影响对话 ----
    try:
        _save_chat_report(state, req, conn_id, report_id, user_text, plan, section_results, narration)
    except Exception:  # noqa: BLE001
        pass
    # 报告工件逐事件落会话历史（committed-turn）
    from app.ai.events_codec import ai_messages_from_events as _amfe

    _rep_ev: list[dict] = [{"type": "plan", "sections": plan}]
    for sec in plan:
        qr = next((q for q in section_results if q.get("result_id") == sec.get("id")), None)
        _rep_ev.append({
            "type": "section", "id": sec["id"], "title": sec["title"],
            "result_id": sec.get("id"), "ok": bool(qr),
            "sql": sec.get("sql", ""),
            "chart": {"kind": sec.get("chart_hint", "bar"), "data": (qr or {}).get("rows") or []},
            "rows": (qr or {}).get("rows") or [], "columns": (qr or {}).get("columns") or [],
        })
    _rep_ev.append({"type": "narration", "section_id": None, "text": narration,
                    "refs": _collect_refs(plan, section_results)})
    yield {"type": "_commit", "messages": _amfe(_rep_ev)}
    yield {"type": "report_done", "report_id": report_id, "section_count": len(plan)}
    yield {"type": "done"}


# ---- LLM 版澄清/计划/叙述（真实网关下走；mock 下不走） ----
def _save_chat_report(state, req, conn_id: str, report_id: str, question: str,
                      plan: list[dict], section_results: list[dict], narration: str) -> None:
    """报告结果落库（results.db）。source_id=<session_id>#<report_id>，每份唯一、可回溯审计。"""
    lines: list[str] = [f"# {question[:80] or '分析报告'}", ""]
    try:
        conn_name = state.connections.get(conn_id).name
    except Exception:
        conn_name = conn_id
    lines += [f"- 数据源：{conn_name}", f"- report_id：`{report_id}`", ""]
    for sec in plan:
        lines.append(f"## {sec.get('title', sec.get('id', ''))}")
        sql = (sec.get("sql") or "").strip()
        if sql:
            lines += ["", "```sql", sql, "```", ""]
        qr = next((q for q in section_results if q.get("result_id") == sec.get("id")), None)
        if qr and qr.get("columns"):
            cols = qr["columns"]
            rows = (qr.get("rows") or [])[:12]
            lines.append("| " + " | ".join(str(c) for c in cols) + " |")
            lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
            for r in rows:
                lines.append("| " + " | ".join(str(v) for v in r) + " |")
            lines.append("")
    lines += ["## 结论", "", narration or "", ""]
    content = "\n".join(lines)
    session_id = (req.session_id or "").strip() or "anon"
    state.results.save_result(
        source="chat", group_id=session_id, source_id=f"{session_id}#{report_id}",
        content=content, title=(question[:80] or "分析报告"), connection_id=conn_id or "",
        fmt="md",
        meta={"session_id": session_id, "report_id": report_id,
              "question": question[:200], "section_count": len(plan)},
    )


async def _llm_clarify(provider, question: str, ctx: dict | None = None) -> list[dict]:
    try:
        from app.ai.prompts import render
        prompt = render("report_clarify", question=question)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None, ctx=ctx)
        text = (resp.content or "").strip()
        text = __import__("re").sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        return (data.get("questions") or [])[:CLARIFY_MAX_QUESTIONS]
    except Exception:  # noqa: BLE001
        return []   # 拿不准就不澄清，直接出计划


async def _llm_plan(provider, question: str, context: str, ctx: dict | None = None) -> list[dict]:
    try:
        from app.ai.prompts import render
        prompt = render("report_plan", context=context, question=question)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None, ctx=ctx)
        text = (resp.content or "").strip()
        text = __import__("re").sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        secs = data.get("sections") or []
        # 补全 id / chart_hint
        out = []
        for i, s in enumerate(secs[:4]):
            out.append({
                "id": s.get("id") or f"r{i+1}",
                "title": s.get("title", f"章节 {i+1}"),
                "intent": s.get("intent", ""),
                "chart_hint": (s.get("chart_hint") or "bar").lower(),
                "depends_on_chapter": s.get("depends_on_chapter"),
                "sql": s.get("sql", ""),
            })
        return out
    except Exception:  # noqa: BLE001
        # 真实路径异常时不回填 mock 演示 SQL（零定制红线），返回空计划并记日志
        return []


async def _llm_narration(provider, question, plan, section_results, ctx: dict | None = None) -> str:
    try:
        # B2: 聚合行使用脱敏后 redacted_rows 出网。
        # M22 降级：行数据未出网（严格模式脱敏失败 fail-closed / 空 redacted_rows 但有行数）→
        # 明确告知模型只有结构信息，定性描述，禁止编造数字。
        def _sec_body(qr: dict) -> str:
            rows = qr.get("redacted_rows", qr.get("rows", []))
            if not rows:
                cols = qr.get("columns") or []
                n = qr.get("row_count") or qr.get("rowcount")
                if n:
                    return (f"（行数据未出网：{len(cols)} 列 × {n} 行。"
                            "请基于列名与行数做定性描述，不要编造具体数字。）")
                return "（本章节无结果行。）"
            return _summarize_results(rows, qr.get("columns", []))

        blocks = "\n\n".join(
            f"## {s['title']}（result_id={s['id']}）\n"
            f"SQL：{s['sql']}\n"
            f"结果：\n{_sec_body(qr)}"
            for s, qr in zip(plan, section_results) if qr.get("ok")
        )
        from app.ai.prompts import render
        prompt = render("report_narrate", question=question, blocks=blocks)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None, ctx=ctx)
        return (resp.content or "").strip() or "（无法生成叙述）"
    except Exception:  # noqa: BLE001
        # 真实路径异常时不回填 mock 演示叙述（零定制红线），返回明确占位
        return "（报告叙述生成失败，请重试）"


def _mock_narration(question: str, plan: list[dict], section_results: list[dict]) -> str:
    """mock 下用各章结果里能拿到的数字拼一段叙述，挂来源标注。"""
    by_id = {qr["result_id"]: qr for qr in section_results if qr.get("ok")}
    parts: list[str] = []

    # 销售总览 r1
    if "r1" in by_id:
        rows = by_id["r1"].get("rows", [])
        total_orders = sum(r[1] for r in rows) if rows else 0
        total_rev = sum(float(r[2]) for r in rows if r[2] is not None) if rows else 0.0
        parts.append(f"近阶段共 {total_orders} 笔订单 [r1]，累计销售额 ¥{total_rev:.2f} [r1]。")
    # 渠道占比 r2
    if "r2" in by_id:
        rows = by_id["r2"].get("rows", [])
        top = rows[0] if rows else None
        if top:
            parts.append(f"营销渠道中「{top[0]}」贡献最高，销售额 ¥{float(top[1]):.2f} [r2]。")
    # 第三章 r3
    if "r3" in by_id:
        rows = by_id["r3"].get("rows", [])
        if rows:
            head = rows[0]
            label = head[0]
            parts.append(f"重点项中「{label}」最值得关注 [r3]（明细见折叠数据块）")
    parts.append("（mock 网关：配真实大模型后会基于完整结果生成更丰富叙述。）")
    return "\n\n".join(parts)


def _collect_refs(plan: list[dict], section_results: list[dict]) -> list[dict]:
    """轻量数字回溯：每个章节给出可跳转的 result_id + 该章节 SQL 首行。"""
    refs = []
    for s, qr in zip(plan, section_results):
        if not qr.get("ok"):
            continue
        refs.append({
            "result_id": s["id"],
            "title": s["title"],
            "sql_head": (s.get("sql") or "").split("\n")[0][:120],
            "row_count": qr.get("row_count", 0),
        })
    return refs