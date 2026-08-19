"""报告模式 generator：澄清 → 计划 → 逐章执行 → 汇总成文。

复用现有 function-calling 底座（provider.chat + execute_tool），但：
- 工具集只读（TOOL_SCHEMAS_READONLY：结构发现 + 只读查询），物理写不了。
- system prompt 用 report_system_prompt（章节三件套 + 数字回溯 + 聚合优先）。
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
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.ai import gateway as gw
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.context import (
    assemble_context,
    report_system_prompt,
)
from app.ai.intent import classify_tags
from app.ai.dto import ChatRequest
from app.ai.tools import TOOL_SCHEMAS_READONLY, execute_tool
from app.core.schema import get_schema
from app.safety import gate as safety_gate
from app.safety.models import Origin, Verdict

if TYPE_CHECKING:
    from app.state import AppState

MAX_REPORT_TURNS = 10        # 报告比单查询多几轮：澄清 + 计划 + 多章
CLARIFY_MAX_QUESTIONS = 3    # 一轮澄清最多 3 个问题


def _normalize_messages(messages: list) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, list):
            content = " ".join(p.get("text", "") for p in m.content if isinstance(p, dict))
        else:
            content = m.content
        out.append({
            "role": m.role,
            "content": content,
            **({"name": m.name} if m.name else {}),
            **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
        })
    return out


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


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
    assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
    # 报告只读：闸门非 ALLOW 直接转 block 记录（工具集本就只读，理论不会出现写）
    if assessment.verdict != Verdict.ALLOW:
        state.audit.log(connection=cfg.name, origin=Origin.AI.value, tier=assessment.tier.value,
                        verdict="block", status="报告查询拦截", sql=sql, report_id=report_id,
                        source="report")
        return {"ok": False, "result_id": result_id, "sql": sql,
                "reason": "; ".join(assessment.reasons)}
    from app.core.query import execute as run_query

    res = await run_query(state, conn_id, sql, max_rows=200)   # 报告聚合，200 行足够
    state.audit.log(connection=cfg.name, origin=Origin.AI.value, tier="read", verdict="allow",
                    status="报告查询", sql=sql, elapsed_ms=res.get("elapsed_ms"),
                    report_id=report_id, source="report")
    return {
        "ok": True,
        "result_id": result_id,
        "sql": sql,
        "columns": res["columns"],
        "types": res["types"],
        "rows": res["rows"],
        "row_count": res["row_count"],
        "elapsed_ms": res["elapsed_ms"],
    }


async def report_stream(state: "AppState", req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    """报告流 generator。前端持有中间态；后端无状态，靠 messages 历史 replay。"""
    # TODO: 测试后删除
    from app.debuglog import dbg
    dbg("[report] start conn=", req.connection_id, "model_id=", req.model_id,
        "reasoning=", req.reasoning)
    provider = gw.build_provider(resolve_provider_cfg(state, req))
    conn_id = req.connection_id
    report_id = f"rep_{uuid.uuid4().hex[:14]}"
    snapshot_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 知识库：未构建过才构建（与 chat_stream 一致）
    if not state.knowledge.is_built(conn_id):
        try:
            schema = await get_schema(state, conn_id)
            await state.knowledge.build(conn_id, schema)
        except Exception:
            pass

    user_text = _last_user_text(_normalize_messages(req.messages))
    context = await assemble_context(state, conn_id, req.table, user_text)

    yield {"type": "report_start", "connection": conn_id,
           "report_id": report_id, "snapshot_ts": snapshot_ts}

    # ---- 1) 澄清：若历史里没有澄清问答且问题欠定义 → yield clarify 后结束本轮 ----
    answered = _extract_clarify_answers(_normalize_messages(req.messages))
    is_mock = resolve_provider_cfg(state, req)["provider"] == "mock"

    need_clarify: list[dict] = []
    if len(answered) == 0 and not any(
        m.get("name") == "clarify" for m in _normalize_messages(req.messages)
    ):
        need_clarify = _mock_clarify(user_text) if is_mock else await _llm_clarify(provider, user_text)

    if need_clarify:
        # 把澄清问题作为一条带 name=clarify 的 system 消息记下（前端答完回传时凭借它判断 replay）
        for q in need_clarify:
            yield {"type": "clarify", "question": q["question"], "field": q.get("field", "")}
        yield {"type": "done"}
        return

    # ---- 2) 计划：mock 下确定性 → plan 事件 ----
    plan = _mock_plan(user_text) if is_mock else await _llm_plan(provider, user_text, context)
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
        else await _llm_narration(provider, user_text, plan, section_results)
    # 一个 narration 事件叙述整份报告的结论（含各章数字 + 来源标注）
    yield {"type": "narration", "section_id": None, "text": narration,
           "refs": _collect_refs(plan, section_results)}
    yield {"type": "report_done", "report_id": report_id, "section_count": len(plan)}
    yield {"type": "done"}


# ---- LLM 版澄清/计划/叙述（真实网关下走；mock 下不走） ----
async def _llm_clarify(provider, question: str) -> list[dict]:
    try:
        prompt = (
            "你是 tabletalk 分析副驾。用户想要一份报告，判断问题是否需要澄清口径。\n"
            f"用户问题：{question}\n"
            "若欠定义（如时间范围/口径/维度不明），提不超过 3 个澄清问题。\n"
            '若已足够清楚，返回 JSON：{"questions": []}。\n'
            '否则返回 JSON：{"questions": [{"field":"time_range","question":"..."}]}。\n'
            "只返回 JSON。"
        )
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        text = (resp.content or "").strip()
        text = __import__("re").sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        return (data.get("questions") or [])[:CLARIFY_MAX_QUESTIONS]
    except Exception:  # noqa: BLE001
        return []   # 拿不准就不澄清，直接出计划


async def _llm_plan(provider, question: str, context: str) -> list[dict]:
    try:
        prompt = (
            "根据以下数据库结构与用户分析目标，规划 2~4 个报告章节，每章一个聚合只读查询。\n"
            f"{context}\n"
            f"用户分析目标：{question}\n"
            "返回 JSON：{\"sections\":[{\"id\":\"r1\",\"title\":\"\",\"intent\":\"\","
            "\"chart_hint\":\"bar|line|pie\",\"sql\":\"只读 SELECT\"}]}。\n"
            "只返回 JSON。chart_hint 仅 bar/line/pie。SQL 必须是有 GROUP BY 或 LIMIT 的聚合查询。"
        )
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
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


async def _llm_narration(provider, question, plan, section_results) -> str:
    try:
        blocks = "\n\n".join(
            f"## {s['title']}（result_id={s['id']}）\n"
            f"SQL：{s['sql']}\n"
            f"结果：\n{_summarize_results(qr.get('rows', []), qr.get('columns', []))}"
            for s, qr in zip(plan, section_results) if qr.get("ok")
        )
        prompt = (
            "基于以下各章聚合查询结果，写这份报告的中文总结叙述。\n"
            "叙述里每个数字必须来自下面的真实结果，并在数字后用 [r<id>] 标注来源。\n"
            f"分析目标：{question}\n{blocks}\n"
            "直接输出 2~4 段中文叙述，不要复述表格。"
        )
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
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