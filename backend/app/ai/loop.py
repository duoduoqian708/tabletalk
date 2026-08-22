"""function-calling 循环：聊天请求 → 组装上下文 → 调网关 → 执行工具 → SSE 事件。

服务端无状态：前端每次带完整消息历史。DB 操作全部过安全闸门。

按 mode 分流：query（默认，单查询）走 chat_stream；report（分析报告：澄清→计划→
逐章执行→汇总）走 report_stream（app/ai/report.py）。意图分类未显式指定 mode 时决定。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.ai import gateway as gw
from app.ai.agent.dispatcher import dispatch_skill
from app.ai.context import assemble_context_full, system_prompt
from app.ai.intent import MODE_QUERY, MODE_REPORT
from app.ai.manifest import build_manifest
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.report import report_stream
from app.ai.dto import ChatRequest
from app.ai.skills.registry import get_skill, skill_tool_schemas
from app.ai.tools import execute_tool
from app.core.schema import get_schema
from app.core.sensitive import filter_sensitive

if TYPE_CHECKING:
    from app.state import AppState

MAX_TURNS = 6


async def stream(state: "AppState", req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    """统一入口：显式 mode 优先，否则 preflight 统一意图（WS1）。

    report 走 report_stream，其余走 chat_stream。preflight 产出 intent/tags/followup，
    dispatcher 感知 enabled 集合做降级。
    """
    if req.mode in (MODE_REPORT, MODE_QUERY):
        mode = req.mode
    else:
        user_text = _last_user_text(_normalize_messages(req.messages))
        # WS1 preflight 统一 owner（单次脱敏/清单/审计，≤2s）
        try:
            from app.ai.preflight import preflight as _pf
            from app.ai.agent.dispatcher import resolve_skill_from_intent as _resolve_skill
            history_tail = _normalize_messages(req.messages)
            pf = await _pf(state, req.connection_id, user_text, history_tail=history_tail)
            # 缓存于 req 供 chat_stream 复用（避免二次 preflight）
            req._preflight = pf  # type: ignore[attr-defined]
            skill_id, degraded, msg = _resolve_skill(pf.intent)
            if degraded:
                # 降级应答：不进检索/工具，直接 SSE 文案
                async def _degraded_stream():
                    yield {"type": "turn_start", "connection": req.connection_id}
                    yield {"type": "text", "content": msg or "此能力已关闭，可在设置中开启"}
                    yield {"type": "done"}
                if pf.intent == "offtopic":
                    # offtopic 即使被降级也走 refusal 文案（若 refusal 可用则已路由到 refusal）
                    # 此分支仅处理 write/ddl 等被禁用情况
                    pass
                # 若降级且目标非 query/refusal（需用户显式开启），直接返回降级流
                if degraded:
                    async for ev in _degraded_stream():
                        yield ev
                    return
            # T2.1 strict 离线拒答：完全离线档下 offtopic 不调任何模型，本地固定文案
            if pf.intent == "offtopic" and _strict_offline(state):
                async def _strict_refusal_stream():
                    yield {"type": "turn_start", "connection": req.connection_id}
                    yield {"type": "text", "content": _strict_refusal_text()}
                    yield {"type": "done"}
                async for ev in _strict_refusal_stream():
                    yield ev
                return
            req.skill_id = skill_id
            mode = MODE_REPORT if skill_id == "report" else MODE_QUERY
        except Exception:
            # preflight 异常兜底：旧 dispatcher 路径
            skill_id = await dispatch_skill(state, user_text)
            mode = MODE_REPORT if skill_id == "report" else MODE_QUERY
            req.skill_id = skill_id
    if mode == MODE_REPORT:
        async for ev in report_stream(state, req):
            yield ev
        return
    async for ev in chat_stream(state, req):
        yield ev


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


# T2.1 strict 离线拒答：本地固定文案（不调模型、不出网）。中文默认（后端生成的文本与现有 mock/错误文案一致）。
_STRICT_REFUSAL_ZH = "这不在我的职责范围内。我是一个数据库与平台助手，只处理与当前数据源相关的查询、分析或平台操作。"


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

    # 知识库：未构建过才构建（真实库不每次对话重采样）；schema 变化由显式 rebuild 刷新
    if not state.knowledge.is_built(conn_id):
        try:
            schema = filter_sensitive(await get_schema(state, conn_id), state.connections.get(conn_id).sensitive)
            await state.knowledge.build(conn_id, schema)
        except Exception:
            pass

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
        }
        yield {"type": "turn_start", "connection": conn_id}
        yield {"type": "stage", "stage": "intent", "value": ["question_library"]}
        yield {"type": "stage", "stage": "retrieval", "tables": matched.get("tables", []), "vec_tables": []}
        yield {"type": "manifest", "manifest": {"tables": matched.get("tables", []), "kb_docs": 0, "history_turns": _hist_q, "include_data": False, "redactions": [], "mode": _mode_q, "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "model": "question_library", "provider": "local"}}
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
    _norm_msgs = _normalize_messages(req.messages)
    try:
        from app.safety.redact import get_salt as _gs2, redact_text as _rt2
        from app.config import get_env as _ge2
        _slt = _gs2(_ge2().data_dir)
        try:
            _sens_hist = state.connections.get(conn_id).sensitive
        except Exception:
            _sens_hist = []
        for _m in _norm_msgs:
            if _m.get("role") == "user" and _m.get("content"):
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
    # T2.2 schema 结构问答：跳过检索管线（零向量召回），只给结构摘要；工具白名单自然保证零 SQL 卡
    _skip_retrieval = getattr(_pf, "intent", None) == "schema"
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
    # 审计：egress 事件（单管道约束：context→manifest→gateway）— 经 logger 加锁
    try:
        try:
            conn_name = state.connections.get(conn_id).name
        except Exception:
            conn_name = conn_id
        state.audit.log(
            connection=conn_name,
            origin="ai",
            tier="read",
            verdict="egress",
            status="egress",
            sql=f"[manifest] {user_text[:60]}",
            source="egress",
            tables=manifest.get("tables"),
            manifest=manifest,
        )
    except Exception:
        pass

    yield {"type": "turn_start", "connection": conn_id}
    # 四步展示的阶段元数据：意图 + 候选表（始终下发，未命中路由则如实空态——结构恒可见、零定制）
    # WS1：intent 来自 preflight（若有），否则回退到 context_meta
    _pf_intent = getattr(getattr(req, "_preflight", None), "intent", None)
    # 将 preflight intent 回填到 context_meta 供前端与审计
    if _pf_intent and not context_meta.get("intent"):
        context_meta["intent"] = [_pf_intent]
    elif _pf_intent:
        # 保证 intent 字段为 preflight 的单值（供 T1.4 审计对比）
        context_meta["preflight_intent"] = _pf_intent
    yield {"type": "stage", "stage": "intent", "value": context_meta.get("intent", [])}
    yield {"type": "stage", "stage": "retrieval", "tables": context_meta.get("candidate_tables", []),
           "vec_tables": context_meta.get("vec_tables", [])}
    yield {"type": "manifest", "manifest": manifest}
    # T1.4/T1.5 度量：记录实际工具与最终表集合
    _tools_used: list[str] = []
    _executed_sqls: list[str] = []
    # 铁律3·禁止靠缺席的**执行层**强制：技能工具集外的工具调用一律拒绝执行。
    # 只收窄 schema 只对模型是"建议"；此处杜绝 mock 的硬编码 tool_call 或模型幻觉
    # 在只读技能（如 query）下误触发 run_dml/draft_ddl。
    _scoped_names = {t["function"]["name"] for t in skill_tool_schemas(req.skill_id)}
    for _ in range(MAX_TURNS):
        tool_calls: list = []
        async for chunk in provider.chat_stream(messages, skill_tool_schemas(req.skill_id)):
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
            # 越权工具：不在本技能工具集内 → 拒绝执行，回给模型"不可用"（禁止靠缺席）
            if tc.name not in _scoped_names:
                yield {"type": "think", "text": f"拒绝调用 {tc.name}（不在技能 {req.skill_id or '默认'} 工具集内）"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps({"ok": False, "error": f"工具 {tc.name} 不在当前技能可用范围内，已拒绝执行。"}, ensure_ascii=False),
                })
                continue
            _tools_used.append(tc.name)
            yield {"type": "think", "text": f"调用 {tc.name}"}
            outcome = await execute_tool(state, tc.name, tc.arguments, conn_id, include_data=req.include_data)
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
            if outcome.card:
                yield {"type": "sql_card", "card": outcome.card}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.name,
                "content": json.dumps(outcome.result, ensure_ascii=False),
            })
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
    yield {"type": "done", "context_meta": context_meta}
