"""唯一 ReAct 循环核心（引擎合一，2026-09）。

三个入口共用一套执行器：
- harness_stream = 默认对话流：readonly 工具常开 + ask_user/propose_plan 交互，
  意图分解由模型在循环内自主完成；mutating 工具不在场——写操作走 propose_plan 先审后动
- controlled_step_stream = 受控计划步骤执行器：步骤指令注入 + 按步骤动作收窄工具集
  （write 步骤带 run_dml + DML 确认 token 流），非交互（无 ask_user/propose_plan）
- run_react_loop = 循环核心：DML REVIEW→confirm_token 流、出网清单（B1）、覆盖率
  指标、committed-turn、provider 重试回灌、墙钟护栏都在核心内

继承底座五件套：committed-turn（_commit 事件）、provider 重试回灌、墙钟护栏、
断连 carry-over（注入/清除由 loop.py 助手承担，保存由 ai.py gen() finally 承担）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.ai import gateway as gw
from app.ai.context import assemble_context_full
from app.ai.events_codec import ai_messages_from_events as _amfe
from app.ai.manifest import build_manifest
from app.ai.prompts import render as _render_prompt
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.tools import execute_tool
from app.ai.tools.registry import (
    TOOL_META,
    TOOL_SCHEMAS,
    reset_active_session as _reset_active_session,
    set_active_session as _set_active_session,
)

if TYPE_CHECKING:
    from app.state import AppState

MAX_TURNS = 30       # 探索预算上限（预算是上限不是目标；简单问题模型自然早停）
WALL_CLOCK = 600.0   # 单请求墙钟（秒）
STEP_MAX_TURNS = 6   # 受控计划步骤轮数预算

_HARNESS_PROMPT = _render_prompt("harness")

_TASK_AGENT_TOOLS = {"candidate_write", "candidate_read", "candidate_test"}


def harness_tool_schemas() -> list[dict]:
    """Harness 工具面：全部 readonly 工具（含 ask_user/propose_plan/draft_ddl）。
    mutating（run_dml/kb_write/graph_write）一律不在场——先审后动；
    candidate_* 是任务智能体专用，不进对话工具面。"""
    return [t for t in TOOL_SCHEMAS
            if t["function"]["name"] not in _TASK_AGENT_TOOLS
            and TOOL_META.get(t["function"]["name"], {}).get("trust") == "readonly"]


# 受控计划步骤工具面（按步骤动作收窄；action 来自 propose_plan 的封闭枚举）
_STEP_TOOLS: dict[str, list[str]] = {
    "write": ["run_dml", "run_query", "get_schema", "ai_review"],
    "query": ["run_query", "get_schema", "query_audit", "ai_review", "kb_read", "graph_read"],
    "kb": ["kb_read", "kb_write", "graph_read", "graph_write", "get_schema", "run_query"],
    "schedule": ["run_query", "get_schema"],
}


def controlled_tool_schemas(action: str) -> list[dict]:
    """受控步骤工具集：按 TaskSpec.action 查表；未知动作回退只读查询面。"""
    names = set(_STEP_TOOLS.get(action) or _STEP_TOOLS["query"])
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in names]


async def _prepare(state: "AppState", req, server_hist: list[dict] | None, pf=None):
    """公共前置段：用户文本脱敏 + 历史组装 + 断连续答注入 + 上下文检索 + messages 构建。

    受控步骤注入（req._task_target 步骤指令 / req._prior_results 前序任务结果）一并
    在此完成（读 req 属性，默认不生效）。返回 (user_text, redactions, messages, context, context_meta)。
    """
    conn_id = req.connection_id
    raw_user_text = ""
    for m in reversed(req.messages):
        role = m.role if hasattr(m, "role") else m.get("role", "")
        if role == "user":
            raw_user_text = m.content if hasattr(m, "content") else m.get("content", "")
            break
    user_text = raw_user_text

    # B2 文本脱敏
    _text_redactions: list[str] = []
    try:
        from app.safety.redact import get_salt, redact_text
        from app.config import get_env as _ge

        _salt = get_salt(_ge().data_dir)
        try:
            _sens = state.connections.get(conn_id).sensitive
        except Exception:
            _sens = []
        _rt, _mp = redact_text(user_text, _salt, _sens)
        if _mp:
            _text_redactions = list(_mp.keys())[:5]
            user_text = _rt
    except Exception:
        pass

    # 历史组装（服务端唯一来源；stream() 已压缩）。无服务端历史（session_id=None/新库）
    # 时回退前端全量回放——兼容通道。
    from app.ai.loop import _server_history_for_model, _pop_carryover_note, _normalize_messages

    if server_hist:
        _norm_msgs = _server_history_for_model(server_hist)
        _new_q = raw_user_text
        if _new_q:
            _norm_msgs.append({"role": "user", "content": _new_q})
    else:
        _norm_msgs = _normalize_messages(req.messages)
    try:
        from app.safety.redact import get_salt as _gs, redact_text as _rt2
        from app.config import get_env as _ge2

        _slt = _gs(_ge2().data_dir)
        try:
            _sens_hist = state.connections.get(conn_id).sensitive
        except Exception:
            _sens_hist = []
        for _m in _norm_msgs:
            if _m.get("role") in ("user", "assistant") and _m.get("content"):
                _rc, _mp2 = _rt2(_m["content"], _slt, _sens_hist)
                if _mp2:
                    for _k in _mp2.keys():
                        if _k not in _text_redactions and len(_text_redactions) < 5:
                            _text_redactions.append(_k)
                    _m["content"] = _rc
    except Exception:
        pass
    # 断点续答：上一轮中断摘要（一次性）
    _carry_note = _pop_carryover_note(state, req.session_id)
    if _carry_note:
        _norm_msgs.append({"role": "user", "content": _carry_note})

    # 上下文组装
    _pf_tags = list(getattr(pf, "tags", None) or []) if pf else []
    _followup = getattr(pf, "followup_tables", None) if pf else None
    _skip_retrieval = bool(getattr(pf, "skip_retrieval", False))
    context, context_meta = await assemble_context_full(
        state, conn_id, req.table, user_text, tags=_pf_tags,
        followup_tables=_followup, skip_retrieval=_skip_retrieval)
    if "coverage" not in context_meta:
        context_meta["coverage"] = 0.0

    messages: list[dict] = [
        {"role": "system", "content": _HARNESS_PROMPT},
        {"role": "system", "content": context},
        *_norm_msgs,
    ]

    # E7：前序任务结果注入（复合计划共享 Context，§18）——受控步骤用
    try:
        _prior = getattr(req, "_prior_results", None) or {}
        if _prior:
            _prior_text = "\n".join(
                f"- 任务 {k}（{v.get('type', 'text')}）：{(v.get('content') or '')[:200]}"
                for k, v in _prior.items()
            )
            messages.insert(2, {"role": "system",
                                "content": f"【前序任务结果（只读参考，勿重复执行）】\n{_prior_text}"})
    except Exception:
        pass
    # 受控计划步骤指令注入（propose_plan 确认后的受控执行器使用）
    try:
        _tgt = getattr(req, "_task_target", None) or {}
        _instr = str(_tgt.get("instruction") or "").strip()
        if _instr:
            messages.insert(2, {"role": "system",
                                "content": f"【受控计划·当前步骤指令（严格执行，勿扩展范围）】\n{_instr}"
                                           + (f"\n步骤参考 SQL：\n{str(_tgt.get('sql'))[:800]}" if _tgt.get("sql") else "")})
    except Exception:
        pass

    return user_text, _text_redactions, messages, context, context_meta


async def run_react_loop(
    state: "AppState", req, *,
    provider, provider_cfg,
    user_text: str, redactions: list[str],
    messages: list[dict], context: str, context_meta: dict,
    tools: list[dict], max_turns: int, interactive: bool,
    skill_label: str, include_data: bool,
) -> AsyncIterator[dict[str, Any]]:
    """唯一 ReAct 循环核心（harness 与受控步骤共用）。

    - interactive=True：启用 ask_user/propose_plan 拦截（结束回合等用户）
    - DML REVIEW→confirm_token 流内建（run_dml 只出现在受控写步骤工具集，无卡时零开销）
    """
    conn_id = req.connection_id
    try:
        conn_name = state.connections.get(conn_id).name
    except Exception:
        conn_name = conn_id

    yield {"type": "turn_start", "connection": conn_id}
    yield {"type": "stage", "stage": "intent", "value": context_meta.get("intent", [])}
    yield {"type": "stage", "stage": "retrieval", "tables": context_meta.get("candidate_tables", []),
           "vec_tables": context_meta.get("vec_tables", [])}
    # B1 出网清单：随 SSE 下发（ManifestView 消费）
    manifest = build_manifest(state, conn_id, context_meta, messages, include_data,
                              provider_cfg, context)
    if redactions:
        manifest["redactions"] = redactions[:5]
    yield {"type": "manifest", "manifest": manifest}
    yield {"type": "scene_start", "scene": skill_label, "skill_id": None}

    _tools_used: list[str] = []
    _executed_sqls: list[str] = []
    _turn_id = uuid.uuid4().hex[:12]
    _t0 = time.monotonic()
    _turn_events: list[dict] = []

    def _dec(t: str) -> str:
        try:
            from app.safety.codify import decodify_text as _dt
            from app.config import get_env as _ge3

            return _dt(_ge3().data_dir, conn_id, t)
        except Exception:
            return t

    _tool_names = {t["function"]["name"] for t in tools}

    for _ in range(max_turns):
        tool_calls: list = []
        turn_text = ""
        _attempt = 0
        while True:  # provider 重试包装
            try:
                async for chunk in provider.chat_stream(messages, tools, ctx={
                    "conn_id": conn_id, "connection": conn_name, "skill": skill_label,
                    "session_id": getattr(req, "session_id", None),
                    "source": "egress", "status": "egress",
                    "include_data": include_data,
                    "redactions": redactions[:5] if redactions else [],
                    "context_meta": context_meta,
                }):
                    if chunk.delta or chunk.content:
                        _raw = chunk.delta or chunk.content
                        turn_text += _raw
                        _ev = {"type": "text", "content": _dec(_raw)}
                        yield _ev
                        _turn_events.append(_ev)
                    elif chunk.tool_calls:
                        tool_calls = list(chunk.tool_calls)
                break
            except Exception as _pe:
                _attempt += 1
                if _attempt > 2:
                    raise
                _note = f"\n\n[连接中断，正在重试（第 {_attempt} 次）…]\n\n"
                yield {"type": "text", "content": _note}
                _turn_events.append({"type": "text", "content": _note})
                messages.append({"role": "user",
                                 "content": f"[turn_retried] 上次模型调用失败（{str(_pe)[:120]}），请从中断处继续。"})
                await asyncio.sleep(2 ** (_attempt - 1))

        if not tool_calls:
            break
        messages.append({
            "role": "assistant",
            "content": turn_text or None,
            "tool_calls": [
                {"id": t.id, "type": "function",
                 "function": {"name": t.name, "arguments": json.dumps(t.arguments, ensure_ascii=False)}}
                for t in tool_calls
            ],
        })
        if (time.monotonic() - _t0) > WALL_CLOCK:
            _budget = "\n\n[已达单次请求时间上限，停止本轮工具循环]\n"
            yield {"type": "text", "content": _budget}
            _turn_events.append({"type": "text", "content": _budget})
            break

        _end_turn = False        # ask_user / propose_plan 成功 → 回合结束（仅 interactive）
        _awaiting_confirm = False  # run_dml REVIEW → 等 DML 确认，模型不再发言
        for tc in tool_calls:
            if tc.name not in _tool_names:
                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "name": tc.name,
                    "content": json.dumps({"ok": False, "error": f"工具 {tc.name} 不可用（当前工具集外/写操作请走 propose_plan）。"},
                                          ensure_ascii=False),
                })
                continue
            _tools_used.append(tc.name)
            _sub_id = f"st_{len(_tools_used)}_{tc.name}"
            yield {"type": "subtask_start", "id": _sub_id, "tool": tc.name, "label": tc.name, "status": "running"}
            yield {"type": "think", "text": f"调用 {tc.name}"}
            _turn_events.append({"type": "think", "text": f"调用 {tc.name}"})
            _args_preview = ""
            try:
                _args_preview = json.dumps(tc.arguments or {}, ensure_ascii=False)[:200]
            except Exception:
                pass
            if _args_preview:
                yield {"type": "subtask_progress", "id": _sub_id, "tool": tc.name,
                       "delta": f"调用 {tc.name}：{_args_preview}"}
            _sess_tok = _set_active_session(req.session_id)
            try:
                outcome = await execute_tool(state, tc.name, tc.arguments, conn_id,
                                             include_data=include_data)
            finally:
                _reset_active_session(_sess_tok)
            yield {"type": "subtask_progress", "id": _sub_id, "tool": tc.name,
                   "delta": outcome.think or f"{tc.name} 完成"}
            # 覆盖率采集
            try:
                _sql = None
                if outcome.card and outcome.card.get("sql"):
                    _sql = outcome.card["sql"]
                elif tc.arguments and tc.arguments.get("sql"):
                    _sql = tc.arguments.get("sql")
                if _sql:
                    _executed_sqls.append(str(_sql))
            except Exception:
                pass
            # 卡片 SQL 展示还原
            try:
                from app.safety.codify import decodify_text
                from app.config import get_env as _ge4

                _dd = _ge4().data_dir
                if outcome.card and outcome.card.get("sql"):
                    outcome.card["sql"] = decodify_text(_dd, conn_id, outcome.card["sql"])
                if outcome.result and isinstance(outcome.result, dict) and outcome.result.get("sql"):
                    outcome.result["sql"] = decodify_text(_dd, conn_id, outcome.result["sql"])
            except Exception:
                pass
            if outcome.think:
                yield {"type": "think", "text": outcome.think}
                _turn_events.append({"type": "think", "text": outcome.think})
            # WS4 T4.2：run_dml REVIEW → 生成 confirm_token，pending_dml 落会话（豁免压缩），
            # 确认卡附 token/expires_in；确认只是"人看过预览"的凭证（T4.3 执行时仍重新过闸门）
            _dml_review = bool(
                outcome.card
                and outcome.card.get("tier") == "dml"
                and outcome.card.get("verdict") == "review"
            )
            if _dml_review:
                from app.safety.confirm import create_pending

                _sid = req.session_id or state.chats.upsert(None, conn_id, None)
                req.session_id = _sid
                payload = create_pending(
                    state.chats, _sid,
                    outcome.card.get("sql", ""),
                    outcome.card.get("preview_rows"),
                    outcome.card.get("rollback"),
                    turn_id=_turn_id,
                )
                outcome.card["confirm_token"] = payload["token"]
                outcome.card["expires_in"] = max(0, int(payload["expires_at"]) - int(time.time()))
                outcome.card["needs_confirm"] = True
                # T4.3 preview 审计：与确认执行条目经 confirm_token/turn_id 互相检索（闭环）
                try:
                    try:
                        _conn_name = state.connections.get(conn_id).name
                    except Exception:
                        _conn_name = conn_id
                    state.audit.log(
                        connection=_conn_name, origin="ai", tier="dml", verdict="review",
                        status="需确认", sql=outcome.card.get("sql", ""), source="dml_preview",
                        reasons=outcome.card.get("reasons") or [],
                        confirm_token=payload["token"], turn_id=_turn_id,
                    )
                except Exception:
                    pass
            if outcome.card:
                if "result_id" not in outcome.card:
                    outcome.card["result_id"] = f"r{uuid.uuid4().hex[:10]}"
                yield {"type": "sql_card", "card": outcome.card}
                _turn_events.append({"type": "sql_card", "card": outcome.card})
            try:
                if outcome.result and outcome.result.get("rows") is not None:
                    cols = outcome.result.get("columns") or []
                    rows = outcome.result.get("rows") or []
                    if cols and rows:
                        yield {"type": "block", "id": _sub_id,
                               "block": {"kind": "table", "columns": cols, "rows": rows[:20],
                                         "title": f"{tc.name} 结果"}}
                        if tc.name == "run_query":
                            yield {"type": "block", "id": _sub_id,
                                   "block": {"kind": "chart", "chartType": "bar", "title": "自动图表",
                                             "data": rows[:10], "columns": cols}}
            except Exception:
                pass
            _is_block = bool(outcome.card and outcome.card.get("verdict") == "block")
            yield {"type": "subtask_done", "id": _sub_id, "tool": tc.name,
                   "status": "error" if _is_block else "done", "detail": outcome.think or ""}
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "name": tc.name,
                "content": json.dumps(outcome.result, ensure_ascii=False),
            })
            if _dml_review:
                # 确认协议：本 turn 终止，不进下一轮次，模型不再发言
                _awaiting_confirm = True
                continue
            # ── 交互工具拦截（仅默认对话流；受控步骤非交互） ──
            if interactive and tc.name == "ask_user" and outcome.result.get("ok"):
                _cl = {"type": "clarify", "origin": "ask_user",
                       "question": outcome.result.get("question", ""),
                       "options": outcome.result.get("options") or []}
                yield _cl
                _turn_events.append(_cl)
                _end_turn = True
            elif interactive and tc.name == "propose_plan" and outcome.result.get("ok"):
                _pp = {"type": "plan_pending", "plan_id": outcome.result.get("plan_id"),
                       "title": outcome.result.get("title"),
                       "steps": outcome.result.get("steps") or [],
                       "expires_in": outcome.result.get("expires_in")}
                yield _pp
                _turn_events.append({"type": "text",
                                     "content": f"已提交计划「{outcome.result.get('title')}」等待用户确认。"})
                _end_turn = True
        # 逐轮提交（committed-turn）：本回合已产出的事件落库（断连只丢当前轮）
        if _turn_events:
            yield {"type": "_commit", "messages": _amfe(_turn_events)}
            _turn_events = []
        if _awaiting_confirm:
            break
        if _end_turn:
            break

    # T1.4/T1.5 覆盖率：done 前计算（最终 SQL 表集合 vs candidate_tables）并写审计/回传
    try:
        _candidate = set(context_meta.get("candidate_tables") or [])
        _final_tables: set[str] = set()
        for _sql in _executed_sqls:
            try:
                import sqlglot as _sg
                from app.safety.gate import sqlglot_dialect_for as _dialect_for
                try:
                    _d = _dialect_for(state.connections.get(conn_id).dialect)
                except Exception:
                    _d = "sqlite"
                expr = _sg.parse_one(_sql, read=_d)
                for tbl in expr.find_all(_sg.exp.Table):
                    name = tbl.name if hasattr(tbl, "name") else str(tbl)
                    if name:
                        _final_tables.add(name)
            except Exception:
                continue
        coverage = 0.0
        if _candidate and _final_tables:
            inter = _final_tables & _candidate
            coverage = round(len(inter) / len(_candidate), 3)
            if _final_tables <= _candidate:
                coverage = 1.0 if not inter else coverage
        context_meta["coverage"] = coverage
        try:
            cname2 = state.connections.get(conn_id).name if conn_id else "__intent__"
        except Exception:
            cname2 = conn_id or "__intent__"
        try:
            # 轻审计：不计入 egress 统计（verdict != egress），仅供检索调优
            state.audit.log(
                connection=cname2,
                origin="ai",
                tier="read",
                verdict="allow",
                status="coverage",
                sql=f"[coverage] {coverage}",
                source="coverage",
                manifest={"coverage": coverage, "tools_used": _tools_used, "final_tables": sorted(_final_tables)},
            )
        except Exception:
            pass
    except Exception:
        pass
    # 尾段提交（最终文本回复在无工具轮产生，此处补齐落库）
    if _turn_events:
        yield {"type": "_commit", "messages": _amfe(_turn_events)}
        _turn_events = []
    yield {"type": "scene_done", "scene": skill_label}
    yield {"type": "done", "context_meta": context_meta}


async def harness_stream(state: "AppState", req, server_hist: list[dict],
                         pf=None) -> AsyncIterator[dict[str, Any]]:
    """默认对话流：单循环直答。pf = preflight 结果（tags/skip_retrieval，可为 None）。"""
    provider_cfg = resolve_provider_cfg(state, req)
    provider = gw.build_provider(provider_cfg)
    user_text, redactions, messages, context, context_meta = await _prepare(
        state, req, server_hist, pf)

    # 探索需要行级预览：非 strict 模式下 run_query 带 include_data（行已封顶+脱敏，与报告模式
    # 既有先例一致）；strict 保持铁律 B4——行数据一律不进模型（骨架化）。
    _eff_include_data = bool(req.include_data)
    if not _eff_include_data:
        try:
            _eff_include_data = state.runtime.get().privacy_mode != "strict"
        except Exception:
            _eff_include_data = True

    async for ev in run_react_loop(
        state, req, provider=provider, provider_cfg=provider_cfg,
        user_text=user_text, redactions=redactions, messages=messages,
        context=context, context_meta=context_meta,
        tools=harness_tool_schemas(), max_turns=MAX_TURNS, interactive=True,
        skill_label="harness", include_data=_eff_include_data,
    ):
        yield ev


async def controlled_step_stream(state: "AppState", req) -> AsyncIterator[dict[str, Any]]:
    """受控计划步骤执行器（propose_plan 确认后逐段执行的单个步骤）。

    步骤指令经 req._task_target 注入（_prepare 内）；工具集按 req._step_action 收窄；
    非交互（无 ask_user/propose_plan——计划已获人审，步骤只执行不反问）。
    """
    provider_cfg = resolve_provider_cfg(state, req)
    provider = gw.build_provider(provider_cfg)
    user_text, redactions, messages, context, context_meta = await _prepare(
        state, req, getattr(req, "_server_history", None), None)
    action = str(getattr(req, "_step_action", "") or "query")
    async for ev in run_react_loop(
        state, req, provider=provider, provider_cfg=provider_cfg,
        user_text=user_text, redactions=redactions, messages=messages,
        context=context, context_meta=context_meta,
        tools=controlled_tool_schemas(action), max_turns=STEP_MAX_TURNS,
        interactive=False, skill_label=f"plan:{action}",
        include_data=bool(req.include_data),
    ):
        yield ev
