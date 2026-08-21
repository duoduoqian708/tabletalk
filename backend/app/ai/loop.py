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
    """统一入口：显式 mode 优先，否则按 dispatcher 意图路由技能。

    report 走 report_stream（report 技能剧本），其余走 chat_stream（query 技能剧本）。
    """
    if req.mode in (MODE_REPORT, MODE_QUERY):
        mode = req.mode
    else:
        user_text = _last_user_text(_normalize_messages(req.messages))
        skill_id = await dispatch_skill(state, user_text)
        mode = MODE_REPORT if skill_id == "report" else MODE_QUERY
        req.skill_id = skill_id  # 供 chat_stream 按技能组合过滤工具集
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

    user_text = _last_user_text(_normalize_messages(req.messages))
    # C6 问题库命中（零模型调用）：先查本地问题库，若命中则直接执行已保存 SQL
    try:
        matched = state.questions.match(conn_id, user_text)
    except Exception:
        matched = None
    if matched:
        sql = matched.get("sql", "")
        # 命中后直接走闸门与执行，不调模型
        from app.safety import gate as _gate
        from app.safety.models import Origin as _Origin
        # 取方言
        try:
            _cfg = state.connections.get(conn_id)
            _dialect = _gate.sqlglot_dialect_for(_cfg.dialect)
        except Exception:
            _dialect = "sqlite"
        _assess = _gate.assess_sql(sql, _dialect, _Origin.AI)
        if _assess.verdict == _assess.verdict.__class__.ALLOW or _assess.verdict.value == "allow":
            from app.core.query import execute as _exec
            try:
                res = await _exec(state, conn_id, sql)
                # 审计
                try:
                    cname = state.connections.get(conn_id).name
                except Exception:
                    cname = conn_id
                state.audit.log(connection=cname, origin="ai", tier="read", verdict="allow", status="问题库命中", sql=sql, source="question_library")
                card = {
                    "tier": "read", "verdict": "allow", "sql": sql, "sub": "问题库命中 · 零模型调用",
                    "result": {"columns": res["columns"], "types": res["types"], "rows": res["rows"], "row_count": res["row_count"], "truncated": res["truncated"], "elapsed_ms": res["elapsed_ms"]},
                    "question_library": True,
                    "question_id": matched.get("id"),
                }
                # 构造与主链路一致的 manifest（供 B1 校验）
                try:
                    _mode = state.runtime.get().privacy_mode
                except Exception:
                    _mode = "standard"
                _hist = sum(1 for m in req.messages if getattr(m, "role", m.get("role") if isinstance(m, dict) else "") in ("user", "assistant", "tool"))
                # 若 req.messages 是 pydantic 对象，需解包
                if _hist == 0:
                    try:
                        _hist = len(req.messages)
                    except Exception:
                        _hist = 1
                yield {"type": "turn_start", "connection": conn_id}
                yield {"type": "stage", "stage": "intent", "value": ["question_library"]}
                yield {"type": "stage", "stage": "retrieval", "tables": matched.get("tables", []), "vec_tables": []}
                yield {"type": "manifest", "manifest": {"tables": matched.get("tables", []), "kb_docs": 0, "history_turns": _hist, "include_data": False, "redactions": [], "mode": _mode, "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "model": "question_library", "provider": "local"}}
                yield {"type": "sql_card", "card": card}
                yield {"type": "text", "content": f"已从问题库命中“{matched.get('question','')[:24]}”，直接执行（零模型调用）。"}
                yield {"type": "done"}
                return
            except Exception as e:
                # 命中但执行失败则回退到正常流程
                pass
        # 若评估非 ALLOW 或执行失败，继续走正常流程（将 matched 置空，避免无限循环）
        matched = None

    context, context_meta = await assemble_context_full(state, conn_id, req.table, user_text)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": context},
        *_normalize_messages(req.messages),
    ]
    # 技能注入：自定义技能的 system_prompt（使用指导书）在上下文后追加，指导本次执行
    if req.skill_id:
        skill = get_skill(req.skill_id)
        if skill is not None and skill.system_prompt:
            messages.insert(2, {"role": "system", "content": skill.system_prompt})

    # B1 出网清单：生成后校验 payload 一致性，随 SSE 发送并写审计（可枚举 100%）
    provider_cfg_for_manifest = resolve_provider_cfg(state, req)
    manifest = build_manifest(state, conn_id, context_meta, messages, bool(req.include_data), provider_cfg_for_manifest, context)
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
    yield {"type": "stage", "stage": "intent", "value": context_meta.get("intent", [])}
    yield {"type": "stage", "stage": "retrieval", "tables": context_meta.get("candidate_tables", []),
           "vec_tables": context_meta.get("vec_tables", [])}
    yield {"type": "manifest", "manifest": manifest}
    for _ in range(MAX_TURNS):
        tool_calls: list = []
        async for chunk in provider.chat_stream(messages, skill_tool_schemas(req.skill_id)):
            if chunk.delta:
                yield {"type": "text", "content": chunk.delta}
            elif chunk.content:
                yield {"type": "text", "content": chunk.content}
            elif chunk.tool_calls:
                tool_calls = chunk.tool_calls
        if not tool_calls:
            break
        for tc in tool_calls:
            yield {"type": "think", "text": f"调用 {tc.name}"}
            outcome = await execute_tool(state, tc.name, tc.arguments, conn_id, include_data=req.include_data)
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
    yield {"type": "done"}
