"""function-calling 循环：聊天请求 → 组装上下文 → 调网关 → 执行工具 → SSE 事件。

服务端无状态：前端每次带完整消息历史。DB 操作全部过安全闸门。

按 mode 分流：query（默认，单查询）走 chat_stream；report（分析报告：澄清→计划→
逐章执行→汇总）走 report_stream（app/ai/report.py）。意图分类未显式指定 mode 时决定。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.ai import gateway as gw
from app.ai.context import assemble_context_full, system_prompt
from app.ai.context_object import Context
from app.ai.intent import MODE_QUERY, MODE_REPORT
from app.ai.manifest import build_manifest
from app.ai.plan import TaskPlan, TaskSpec
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.report import report_stream
from app.ai.dto import ChatRequest
from app.ai.skills.registry import get_skill, skill_tool_schemas
from app.ai.tools import execute_tool
from app.ai.tools.registry import reset_active_session as _reset_active_session
from app.ai.tools.registry import set_active_session as _set_active_session

if TYPE_CHECKING:
    from app.state import AppState

MAX_TURNS = 6  # P3：兜底轮数；技能级 max_turns 优先（route.termination_max_turns）


async def stream(state: "AppState", req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    """统一入口（E7）：preflight（plan 级字段）→ decompose(TaskPlan) → 任务循环。

    单任务计划（绝大多数请求）→ 一个 ReAct（chat_stream/report_stream 单任务执行器）；
    复合计划 → 顺序执行 + 失败即停 + 编号结果。事件协议向后兼容（增量 task_* 事件）。
    """
    from app.ai.context_object import Context
    from app.ai.executor import execute_plan
    from app.ai.plan import TaskPlan, TaskSpec
    from app.ai.skills.route import route_effective

    # 显式 mode 兼容：report/query 显式指定 → 单任务 plan
    if req.mode in (MODE_REPORT, MODE_QUERY):
        action = "query"
        modality = "report" if req.mode == MODE_REPORT else "answer"
        plan = TaskPlan(tasks=[TaskSpec(action=action, modality=modality)])
        req.skill_id = route_effective(action, modality)
        ctx = Context(conn_id=req.connection_id, plan=plan, session_id=req.session_id,
                      include_data=bool(req.include_data))
        async for ev in execute_plan(state, plan, ctx,
                                     lambda s, c, t: task_runner(s, req, c, t)):
            yield ev
        yield {"type": "done"}
        return

    # WS3 T3.1：服务端历史优先（同原逻辑，preflight 与执行器共用）
    req._server_history = []
    if req.session_id:
        try:
            _expire_stale_pending(state, req.session_id)
            req._server_history = state.chats.get_messages(req.session_id) or []
            if req._server_history:
                try:
                    from app.ai.compress import compress_hist
                    _ch = await compress_hist(state, req.connection_id, req._server_history)
                    req._server_history = _ch["rows"]
                    req._compress_stats = _ch["stats"]
                except Exception:
                    pass
        except Exception:
            req._server_history = []
    user_text = _last_user_text(_normalize_messages(req.messages))
    # preflight：plan 级辅助字段（tags/followup/skip_retrieval）+ 脱敏/清单/审计管道
    plan: TaskPlan | None = None
    try:
        from app.ai.preflight import preflight as _pf
        pf = await _pf(state, req.connection_id, user_text, history_tail=req._server_history or _normalize_messages(req.messages))
        req._preflight = pf  # type: ignore[attr-defined]  # chat_stream 复用
        # decompose → TaskPlan（2026-09：意图统一由 LLM 产出，无关键词层；
        # 复用 preflight 的脱敏原文 + 清单，全链路 LLM 出网隐私/记账一致）
        from app.ai.decompose import decompose as _decompose
        plan = await _decompose(state, req.connection_id, user_text,
                                confirmed_tags=pf.tags or None,
                                history_tail=req._server_history or _normalize_messages(req.messages),
                                redacted_q=pf.redacted_q or user_text,
                                manifest=pf.manifest)
        # 意图回填事件/度量（scene_start/stage/intent_mismatch 消费 preflight.intent）
        pf.intent = plan.tasks[0].action if plan.tasks else "query"
        # preflight 的 plan 级字段优先（追问轮检测更完整）
        if pf.is_followup:
            plan.followup_tables = pf.followup_tables or []
        if pf.skip_retrieval:
            plan.skip_retrieval = True
    except Exception:
        # preflight/decompose 异常兜底：单任务 query
        from app.ai.plan import TaskPlan as _TP, TaskSpec as _TS
        plan = _TP(tasks=[_TS(action="query", modality="answer")], degraded=True)
    req.skill_id = route_effective(plan.tasks[0].action, plan.tasks[0].modality)
    # T2.1 strict 离线拒答：完全离线档下 offtopic/unknown 不调任何模型，本地固定文案。
    # 2026-09：意图识别已无关键词层（LLM 唯一），离线无 LLM 判不出 unknown → 这里用一个
    # 仅限离线模式的窄拒答护栏兜底（平台外话题兜底，非通用意图识别，不恢复关键词层）。
    if _strict_offline(state) and _offline_offtopic(user_text):
        async def _strict_refusal_stream():
            yield {"type": "turn_start", "connection": req.connection_id}
            yield {"type": "text", "content": _strict_refusal_text()}
            yield {"type": "done"}
        async for ev in _strict_refusal_stream():
            yield ev
        return
    if plan.tasks[0].action == "unknown" and _strict_offline(state):
        async def _strict_refusal_stream():
            yield {"type": "turn_start", "connection": req.connection_id}
            yield {"type": "text", "content": _strict_refusal_text()}
            yield {"type": "done"}
        async for ev in _strict_refusal_stream():
            yield ev
        return
    # offtopic（unknown）：走 refusal 技能引导拒答（不进任务循环/检索/工具）
    if plan.tasks[0].action == "unknown" and len(plan.tasks) == 1:
        from app.ai.skills.registry import get_skill as _gs
        _ref = _gs("refusal")
        if _ref is not None and _ref.enabled:
            req.skill_id = "refusal"
            async for ev in chat_stream(state, req):
                yield ev
            return
    ctx = Context(conn_id=req.connection_id, plan=plan, session_id=req.session_id,
                  include_data=bool(req.include_data))
    async for ev in execute_plan(state, plan, ctx,
                                 lambda s, c, t: task_runner(s, req, c, t)):
        yield ev
    # 协议兼容：任务循环结束统一补发 done（chat_stream/report_stream 的 done 已延迟）
    yield {"type": "done"}


async def task_runner(state: "AppState", req: ChatRequest, ctx: Context,
                      task: TaskSpec) -> AsyncIterator[dict[str, Any]]:
    """单任务执行器（E7）：TaskSpec → skill_id → chat_stream / report_stream。

    - modality=report → report 技能执行器（report_stream，保留 report_id/章节事件）
    - 其余 → chat_stream（单任务 ReAct）
    - 前序任务结果经 ctx 注入子请求（req._prior_results），由 chat_stream 组装进上下文
    """
    from app.ai.skills.route import route_effective as _route
    skill_id = _route(task.action, task.modality)
    # 任务级 skill_id 注入（chat_stream 用它过滤工具集 + scene_start 事件）
    req.skill_id = skill_id
    # F5：任务级 target 传递（chat_stream 并入检索种子/上下文）
    req._task_target = task.target or {}  # type: ignore[attr-defined]
    # P2-13/§18：Context 跨层共享字段填充——selected_tables（任务 target + 追问表集）、
    # session_vars（连接级配置真实值，执行层替换用）
    try:
        _tbls: list[str] = []
        _tgt = task.target or {}
        if isinstance(_tgt.get("tables"), list):
            _tbls.extend(t for t in _tgt["tables"] if isinstance(t, str))
        _pf = getattr(req, "_preflight", None)
        if _pf is not None and getattr(_pf, "followup_tables", None):
            _tbls.extend(getattr(_pf, "followup_tables"))
        if _tbls:
            ctx.selected_tables = list(dict.fromkeys(_tbls))
        try:
            _sv = state.connections.get(req.connection_id).session_vars
            ctx.session_vars = dict(_sv or {})
        except Exception:
            ctx.session_vars = {}
    except Exception:
        pass
    if skill_id == "report":
        req.mode = MODE_REPORT
        async for ev in report_stream(state, req):
            # done 延迟到 task_result 之后（协议兼容）
            if ev.get("type") == "done":
                continue
            yield ev
    else:
        req.mode = MODE_QUERY
        try:
            req._prior_results = {  # type: ignore[attr-defined]
                k: (v.to_dict() if hasattr(v, "to_dict") else v)
                for k, v in ctx.task_results.items()
            }
        except Exception:
            req._prior_results = {}  # type: ignore[attr-defined]
        async for ev in chat_stream(state, req):
            # done 延迟到 task_result 之后（协议兼容）
            if ev.get("type") == "done":
                continue
            yield ev
    # 任务结束：done 统一由任务循环末尾补发（stream 层）


def _normalize_messages(messages: list) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, list):  # 多段内容降级为文本
            text = " ".join(p.get("text", "") for p in m.content if isinstance(p, dict))
            content = text
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


def _expire_stale_pending(state: "AppState", sid: str | None) -> None:
    """WS4 T4.4：上一轮遗留的过期 pending_dml 惰性清除，并写回系统消息（下轮模型上下文可见）。"""
    if not sid:
        return
    try:
        p = state.chats.get_pending_dml(sid)
        if p and int(time.time()) > int(p.get("expires_at", 0)):
            state.chats.clear_pending_dml(sid)
            state.chats.append_messages(sid, [{
                "role": "assistant", "kind": "system",
                "content": "上一次写操作确认已过期，需重新预览后才能执行。",
            }])
    except Exception:
        pass


# ---- WS3 T3.1：服务端历史 → 模型可见消息 ----

def _card_skeleton(row: dict) -> str:
    """D7 骨架：result_id/表名/verdict/rowcount + sql。铁律1：落库的 sql_card 原始 content 含 result.rows，
    feed 回模型等于行数据出网绕过脱敏管道——只准骨架，绝不带 columns/rows。"""
    card: dict = {}
    raw = row.get("content")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            card = json.loads(raw)
        except Exception:
            card = {}
    res = card.get("result") or {}
    pieces: list[str] = []
    if card.get("result_id"):
        pieces.append(f"result_id={card['result_id']}")
    if card.get("verdict") or row.get("verdict"):
        pieces.append(f"verdict={card.get('verdict') or row.get('verdict')}")
    if res.get("row_count") is not None:
        pieces.append(f"row_count={res['row_count']}")
    elif card.get("row_count") is not None:
        pieces.append(f"row_count={card['row_count']}")
    sql = row.get("sql") or card.get("sql") or ""
    legend = "；".join(pieces)
    if sql:
        legend += f"；sql={sql}" if legend else f"sql={sql}"
    return f"[上轮结果卡] {legend or '(未取到骨架)'}"


def _server_history_for_model(rows: list[dict]) -> list[dict]:
    """WS3 T3.1：落库的服务端历史还原为模型可见消息。
    - user 原样；assistant 的 text/clarify/report 为正文/叙述原文。
    - sql_card 只喂 D7 骨架（result_id/表名/verdict/rowcount），绝不带原始行数据（铁律1）。
    - think/stage 是 UI/审计元数据，非对话，跳过。"""
    out: list[dict] = []
    for m in rows:
        role = m.get("role", "")
        kind = m.get("kind", "text")
        content = m.get("content") or ""
        if role == "user":
            out.append({"role": "user", "content": content})
        elif kind in ("text", "report", "clarify"):
            out.append({"role": "assistant", "content": content})
        elif kind == "system":
            # WS4 T4.4：取消/过期等系统信号——模型下轮可见（assistant 视角，避免打断 user 轮次）
            out.append({"role": "assistant", "content": f"[系统] {content}"})
        elif kind == "sql_card":
            out.append({"role": "assistant", "content": _card_skeleton(m)})
        # think / stage / gate 跳过（非模型对话）
    return out


# T2.1 strict 离线拒答：本地固定文案（不调模型、不出网）。中文默认（后端生成的文本与现有 mock/错误文案一致）。
_STRICT_REFUSAL_ZH = "这不在我的职责范围内。我是一个数据库与平台助手，只处理与当前数据源相关的查询、分析或平台操作。"


def _offline_offtopic(q: str) -> bool:
    """仅离线模式用的平台外话题窄判定（不是意图识别，是离线安全网兜底）。"""
    try:
        return bool(re.search(r"你好|谢谢|再见|天气|笑话|你是谁|自我介绍|写诗|闲聊|股票|新闻|游戏", q or "", re.IGNORECASE))
    except Exception:
        return False


def _strict_offline(state) -> bool:
    try:
        return getattr(state.runtime.get(), "privacy_mode", "standard") == "strict"
    except Exception:
        return False


def _strict_refusal_text() -> str:
    return _STRICT_REFUSAL_ZH


async def chat_stream(state: "AppState", req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    provider = gw.build_provider(resolve_provider_cfg(state, req))
    conn_id = req.connection_id

    raw_user_text = _last_user_text(_normalize_messages(req.messages))
    user_text = raw_user_text
    # T5.3 原文匹配：问题库匹配在脱敏**前**（修 loop.py:108 顺序），脱敏只管出网
    try:
        matched = state.questions.match(conn_id, raw_user_text)
    except Exception:
        matched = None
    if matched:
        # T5.4 命中改确认卡（亮 SQL，用户点执行才跑），不直接执行；provider=local 清单
        try:
            _mode_q = state.runtime.get().privacy_mode
        except Exception:
            _mode_q = "standard"
        _hist_q = sum(1 for m in req.messages if getattr(m, "role", m.get("role") if isinstance(m, dict) else "") in ("user", "assistant", "tool"))
        if _hist_q == 0:
            try:
                _hist_q = len(req.messages)
            except Exception:
                _hist_q = 1
        card_q = {
            "tier": "read", "verdict": "review", "sql": matched.get("sql", ""), "sub": "问题库命中 · 请确认后执行",
            "question_library": True,
            "question_id": matched.get("id"),
            "needs_confirm": True,
            "result_id": f"r{uuid.uuid4().hex[:10]}",
        }
        yield {"type": "turn_start", "connection": conn_id}
        yield {"type": "stage", "stage": "intent", "value": ["question_library"]}
        yield {"type": "stage", "stage": "retrieval", "tables": matched.get("tables", []), "vec_tables": []}
        yield {"type": "manifest", "manifest": {"tables": matched.get("tables", []), "kb_docs": 0, "history_turns": _hist_q, "include_data": False, "redactions": [], "mode": _mode_q, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": "question_library", "provider": "local"}}
        yield {"type": "sql_card", "card": card_q}
        yield {"type": "text", "content": f"已从问题库命中“{matched.get('question','')[:24]}”，请确认后执行（零模型调用）。"}
        yield {"type": "done"}
        return
    # B2 对话文本脱敏：用户问题里贴的敏感串先打码（计入 redactions 用于清单）
    _text_redactions: list[str] = []
    try:
        from app.safety.redact import get_salt, redact_text
        from app.config import get_env as _get_env2
        _salt2 = get_salt(_get_env2().data_dir)
        try:
            _sens2 = state.connections.get(conn_id).sensitive
        except Exception:
            _sens2 = []
        # 对当前轮 user_text 脱敏（用于上下文与历史）
        _rt, _mp = redact_text(user_text, _salt2, _sens2)
        if _mp:
            _text_redactions = list(_mp.keys())[:5]
            user_text = _rt
    except Exception:
        pass

    # 历史消息同样脱敏（避免原文出网）
    # WS3 T3.1：有服务端历史则从服务端组装（+ 本轮最新 user 问题），否则用 req.messages 兼容通道
    _server_hist = getattr(req, "_server_history", None) or []
    _norm_msgs = _server_history_for_model(_server_hist) if _server_hist else _normalize_messages(req.messages)
    if _server_hist:
        _new_q = _last_user_text(_normalize_messages(req.messages))
        if _new_q:
            _norm_msgs.append({"role": "user", "content": _new_q})
    try:
        from app.safety.redact import get_salt as _gs2, redact_text as _rt2
        from app.config import get_env as _ge2
        _slt = _gs2(_ge2().data_dir)
        try:
            _sens_hist = state.connections.get(conn_id).sensitive
        except Exception:
            _sens_hist = []
        for _m in _norm_msgs:
            # P3：user 与 assistant 均脱敏——assistant 回复中可能回显真实值，同样不能出网
            if _m.get("role") in ("user", "assistant") and _m.get("content"):
                _rc, _mp2 = _rt2(_m["content"], _slt, _sens_hist)
                if _mp2:
                    # 合并到总 redactions（去重，上限 5）
                    for _k in _mp2.keys():
                        if _k not in _text_redactions and len(_text_redactions) < 5:
                            _text_redactions.append(_k)
                    _m["content"] = _rc
    except Exception:
        pass
    # WS1：复用 preflight 的 tags 与追问 seeds，避免重复 LLM
    _pf = getattr(req, "_preflight", None)
    _pf_tags = getattr(_pf, "tags", None) if _pf else None
    _followup_tables = getattr(_pf, "followup_tables", None) if _pf else None
    # F5：任务级 target.tables 并入检索种子（§13.2#4 target 影响 prompt 组装）
    try:
        _tgt = getattr(req, "_task_target", None) or {}
        _tgt_tables = [t for t in (_tgt.get("tables") or []) if isinstance(t, str)]
        if _tgt_tables:
            _followup_tables = list(dict.fromkeys((_followup_tables or []) + _tgt_tables))
    except Exception:
        pass
    # skip_retrieval：结构问答/审计类意图跳过向量检索管线（由 preflight.skip_retrieval 控制）
    _skip_retrieval = getattr(_pf, "skip_retrieval", False)
    context, context_meta = await assemble_context_full(
        state, conn_id, req.table, user_text, tags=_pf_tags,
        followup_tables=_followup_tables, skip_retrieval=_skip_retrieval,
    )
    # T1.5 覆盖率占位（done 时补真实值，此处先写 0 供前端占位）
    if "coverage" not in context_meta:
        context_meta["coverage"] = 0.0
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": context},
        *_norm_msgs,
    ]
    # E7：前序任务结果注入（复合计划共享 Context，§18）
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
    # 技能注入：自定义技能的 system_prompt（使用指导书）在上下文后追加，指导本次执行
    if req.skill_id:
        skill = get_skill(req.skill_id)
        if skill is not None and skill.system_prompt:
            messages.insert(2, {"role": "system", "content": skill.system_prompt})

    # B1 出网清单：生成后校验 payload 一致性，随 SSE 发送并写审计（可枚举 100%）
    provider_cfg_for_manifest = resolve_provider_cfg(state, req)
    manifest = build_manifest(state, conn_id, context_meta, messages, bool(req.include_data), provider_cfg_for_manifest, context)
    if _text_redactions:
        manifest["redactions"] = _text_redactions[:5]
    # 出网清单审计改由中央记账拦截器（gateway）统一写；此处仅保留连接名供 ctx.connection
    try:
        conn_name = state.connections.get(conn_id).name
    except Exception:
        conn_name = conn_id

    yield {"type": "turn_start", "connection": conn_id}
    _pf_intent = getattr(getattr(req, "_preflight", None), "intent", None)
    if _pf_intent and not context_meta.get("intent"):
        context_meta["intent"] = [_pf_intent]
    elif _pf_intent:
        context_meta["preflight_intent"] = _pf_intent
    yield {"type": "stage", "stage": "intent", "value": context_meta.get("intent", [])}
    yield {"type": "stage", "stage": "retrieval", "tables": context_meta.get("candidate_tables", []),
           "vec_tables": context_meta.get("vec_tables", [])}
    yield {"type": "manifest", "manifest": manifest}
    yield {"type": "scene_start", "scene": getattr(req, "skill_id", None) or "query", "intent": _pf_intent, "skill_id": getattr(req, "skill_id", None)}
    # T1.4/T1.5 度量：记录实际工具与最终表集合
    _tools_used: list[str] = []
    _executed_sqls: list[str] = []
    # WS4 T4.3：本轮 turn 标识（preview 审计与确认执行审计经 token+turn_id 闭环）
    _turn_id = uuid.uuid4().hex[:12]
    # WS4 T4.2：run_dml REVIEW → 本 turn 到此为止（模型不再发言，防幻觉"已执行"）
    _awaiting_confirm = False
    # 铁律3·禁止靠缺席的**执行层**强制：技能工具集外的工具调用一律拒绝执行。
    # 只收窄 schema 只对模型是"建议"；此处杜绝 mock 的硬编码 tool_call 或模型幻觉
    # 在只读技能（如 query）下误触发 run_dml/draft_ddl。
    _scoped_names = {t["function"]["name"] for t in skill_tool_schemas(req.skill_id)}
    # F2：技能级终止条件（termination.max_turns），缺省回退全局 MAX_TURNS
    from app.ai.skills.route import termination_max_turns as _tmt
    _max_turns = _tmt(req.skill_id) if req.skill_id else MAX_TURNS
    for _ in range(_max_turns):
        tool_calls: list = []
        # 中央记账：拦截器在流式 finally 落一条（llm_log + cost + egress），ctx 带全上下文
        async for chunk in provider.chat_stream(messages, skill_tool_schemas(req.skill_id), ctx={
            "conn_id": conn_id, "connection": conn_name, "skill": getattr(req, "skill_id", None),
            "session_id": getattr(req, "session_id", None), "source": "egress", "status": "egress",
            "include_data": bool(req.include_data),
            "redactions": _text_redactions[:5] if _text_redactions else [],
            "context_meta": context_meta,
            "manifest": manifest,
        }):
            # B3 还原：叙述文本中的代号还原为真名（展示层）
            def _dec_text(t: str) -> str:
                try:
                    from app.safety.codify import decodify_text as _dt
                    from app.config import get_env as _ge3
                    return _dt(_ge3().data_dir, conn_id, t)
                except Exception:
                    return t
            if chunk.delta:
                yield {"type": "text", "content": _dec_text(chunk.delta)}
            elif chunk.content:
                yield {"type": "text", "content": _dec_text(chunk.content)}
            elif chunk.tool_calls:
                tool_calls = chunk.tool_calls
        if not tool_calls:
            break
        for tc in tool_calls:
            if tc.name not in _scoped_names:
                _blk_id = f"st_{len(_tools_used)+1}_{tc.name}_blocked"
                yield {"type": "subtask_start", "id": _blk_id, "tool": tc.name, "label": tc.name, "status": "running"}
                yield {"type": "think", "text": f"拒绝调用 {tc.name}（不在技能 {req.skill_id or '默认'} 工具集内）"}
                yield {"type": "subtask_done", "id": _blk_id, "tool": tc.name, "status": "error", "detail": "工具不在技能白名单，已拒绝"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps({"ok": False, "error": f"工具 {tc.name} 不在当前技能可用范围内，已拒绝执行。"}, ensure_ascii=False),
                })
                continue
            _tools_used.append(tc.name)
            _sub_id = f"st_{len(_tools_used)}_{tc.name}"
            yield {"type": "subtask_start", "id": _sub_id, "tool": tc.name, "label": tc.name, "status": "running"}
            try:
                from app.safety.gate import sqlglot_dialect_for as _gdf
                _dia = _gdf(state.connections.get(conn_id).dialect)
            except Exception:
                _dia = "sqlite"
            try:
                _sql_preview = (tc.arguments or {}).get("sql", "")
                if _sql_preview:
                    yield {"type": "block", "id": _sub_id, "block": {"kind": "sql_editor", "sql": _sql_preview, "dialect": _dia}}
            except Exception:
                pass
            yield {"type": "think", "text": f"调用 {tc.name}"}
            _args_preview = ""
            try:
                import json as _json
                _args_preview = _json.dumps(tc.arguments or {}, ensure_ascii=False)[:200]
            except Exception:
                pass
            yield {"type": "subtask_progress", "id": _sub_id, "tool": tc.name,
                   "delta": (f"调用 {tc.name}" + (f"：{_args_preview}" if _args_preview else " ..."))}
            _sess_tok = _set_active_session(req.session_id)
            try:
                outcome = await execute_tool(state, tc.name, tc.arguments, conn_id, include_data=req.include_data)
            finally:
                _reset_active_session(_sess_tok)
            yield {"type": "subtask_progress", "id": _sub_id, "tool": tc.name, "delta": outcome.think or f"{tc.name} 完成"}
            # 收集执行过的 SQL 供覆盖率
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
            # B3 回显还原：代号化 SQL 还原为原文展示
            try:
                from app.safety.codify import decodify_text
                from app.config import get_env
                dd = get_env().data_dir
                if outcome.card and outcome.card.get("sql"):
                    outcome.card["sql"] = decodify_text(dd, conn_id, outcome.card["sql"])
                if outcome.result and isinstance(outcome.result, dict) and outcome.result.get("sql"):
                    outcome.result["sql"] = decodify_text(dd, conn_id, outcome.result["sql"])
            except Exception:
                pass
            if outcome.think:
                yield {"type": "think", "text": outcome.think}
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
            try:
                if outcome.result and outcome.result.get("rows") is not None:
                    cols = outcome.result.get("columns") or []
                    rows = outcome.result.get("rows") or []
                    if cols and rows is not None:
                        yield {"type": "block", "id": _sub_id, "block": {"kind": "table", "columns": cols, "rows": rows[:20], "title": f"{tc.name} 结果"}}
                        if tc.name in ("run_query", "db_read") and rows:
                            yield {"type": "block", "id": _sub_id, "block": {"kind": "chart", "chartType": "bar", "title": "自动图表", "data": rows[:10], "columns": cols}}
                if outcome.card and outcome.card.get("needs_confirm"):
                    yield {"type": "block", "id": _sub_id, "block": {"kind": "confirm", "prompt": f"确认执行 {tc.name}？", "confirmLabel": "确认", "cancelLabel": "取消"}}
            except Exception:
                pass
            # P1-5：verdict=block 的卡（图校验打回/严格模式拦截/只读拦截）→ 工具失败信号，
            # executor 归纳为 TaskResult ok=False → 失败即停对后续写任务生效（§14）
            _is_block = bool(outcome.card and outcome.card.get("verdict") == "block")
            yield {"type": "subtask_done", "id": _sub_id, "tool": tc.name,
                   "status": "error" if _is_block else "done", "detail": outcome.think or ""}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.name,
                "content": json.dumps(outcome.result, ensure_ascii=False),
            })
            if _dml_review:
                # 确认协议：本 turn 终止，不进下一 MAX_TURNS 轮次，模型不再发言
                _awaiting_confirm = True
        if _awaiting_confirm:
            break
    # T1.4 意图质量 + T1.5 覆盖率：done 前计算并写审计/回传
    try:
        # 覆盖率：最终 SQL 表集合 vs candidate_tables
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
        if _candidate:
            inter = _final_tables & _candidate if _final_tables else set()
            # 若无执行表（纯问答），覆盖率按 0；有执行但候选为空则 0
            coverage = round(len(inter) / len(_candidate), 3) if _candidate else 0.0
            # 若执行表完全在候选内且非空，覆盖率应为 1.0 的近似；空执行保持 0
            if _final_tables and _final_tables <= _candidate:
                coverage = 1.0 if not inter else coverage
        context_meta["coverage"] = coverage
        # 意图 mismatch：preflight intent vs 实际 skill/tools
        _intent_mismatch = False
        try:
            if _pf_intent:
                from app.ai.agent.dispatcher import resolve_skill_from_intent as _rsi
                _expected_skill, _, _ = _rsi(_pf_intent)
                _actual_skill = getattr(req, "skill_id", None) or "query"
                if _expected_skill != _actual_skill:
                    _intent_mismatch = True
                # 额外：query 意图却走了 write 工具 等
                if _pf_intent == "query" and "run_dml" in _tools_used:
                    _intent_mismatch = True
                if _pf_intent == "write" and "run_dml" not in _tools_used and _tools_used:
                    # 仅当有工具调用时才判 mismatch，避免无工具轮误判
                    _intent_mismatch = True
        except Exception:
            pass
        context_meta["intent_mismatch"] = _intent_mismatch
        # 写审计：coverage + mismatch 随 egress 审计补充（不新增条目，仅在 manifest 审计后追加一条轻审计便于查询）
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
                sql=f"[coverage] {coverage} mismatch={_intent_mismatch}",
                source="coverage",
                manifest={"coverage": coverage, "intent_mismatch": _intent_mismatch, "tools_used": _tools_used, "final_tables": sorted(_final_tables)},
            )
        except Exception:
            pass
    except Exception:
        pass
    yield {"type": "scene_done", "scene": getattr(req, "skill_id", None) or "query"}
    yield {"type": "done", "context_meta": context_meta}
