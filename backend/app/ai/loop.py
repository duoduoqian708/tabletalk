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
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.report import report_stream
from app.ai.dto import ChatRequest
from app.ai.skills.registry import skill_tool_schemas
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
    # TODO: 测试后删除
    from app.debuglog import dbg
    dbg("[chat] conn=", conn_id, "model_id=", req.model_id, "reasoning=", req.reasoning,
        "user=", user_text[:40])
    context, context_meta = await assemble_context_full(state, conn_id, req.table, user_text)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": context},
        *_normalize_messages(req.messages),
    ]

    yield {"type": "turn_start", "connection": conn_id}
    # 四步展示的阶段元数据：意图 + 候选表（始终下发，未命中路由则如实空态——结构恒可见、零定制）
    yield {"type": "stage", "stage": "intent", "value": context_meta.get("intent", [])}
    yield {"type": "stage", "stage": "retrieval", "tables": context_meta.get("candidate_tables", [])}
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
