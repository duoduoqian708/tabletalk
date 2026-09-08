"""引擎统一入口：聊天请求 → 历史压缩 → preflight → 分发（引擎合一 2026-09）。

- 默认（含显式 mode=query）：问题库快路径（零模型）→ harness 单循环（app/ai/harness.py）
- 显式 mode=report：受控报告流（execute_plan 外层 + report_stream）
- 受控计划步骤（propose_plan 确认后）：execute_plan 外层 + controlled_step_stream

服务端无状态：前端每次带完整消息历史。DB 操作全部过安全闸门。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.ai.context_object import Context
from app.ai.plan import TaskPlan, TaskSpec
from app.ai.report import report_stream
from app.ai.dto import ChatRequest

if TYPE_CHECKING:
    from app.state import AppState

MODE_QUERY = "query"
MODE_REPORT = "report"


async def stream(state: "AppState", req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    """统一入口：preflight（脱敏/清单/tags）→ 分发。

    - 显式 mode=report → 受控报告流（execute_plan + report_stream，UI 直触，只读无需人审）
    - 其余（含显式 mode=query）→ 问题库快路径（零模型确认卡）→ harness 单循环
      （readonly 工具常开 + ask_user/propose_plan；意图分解由模型在循环内自主完成）
    """
    from app.ai.executor import execute_plan
    from app.ai.harness import harness_stream

    # WS3 T3.1 + 历史统一：服务端历史是唯一历史来源（默认流/显式 mode/报告模式共用）。
    # 前端 req.messages 仅提供本轮最新 user 问句（localStorage 降级为展示缓存）。
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

    # 显式 mode=report：受控报告流（UI 直触）
    if req.mode == MODE_REPORT:
        plan = TaskPlan(tasks=[TaskSpec(action="query", modality="report")])
        from app.ai.context_object import Context

        ctx = Context(conn_id=req.connection_id, plan=plan, session_id=req.session_id,
                      include_data=bool(req.include_data))
        async for ev in execute_plan(state, plan, ctx,
                                     lambda s, c, t: task_runner(s, req, c, t)):
            yield ev
        yield {"type": "done"}
        return

    user_text = _last_user_text(_normalize_messages(req.messages))
    # preflight：脱敏/清单/tags/followup/skip_retrieval
    pf = None
    try:
        from app.ai.preflight import preflight as _pf
        pf = await _pf(state, req.connection_id, user_text,
                       history_tail=req._server_history or _normalize_messages(req.messages))
        req._preflight = pf  # type: ignore[attr-defined]
    except Exception:
        pf = None
    # T2.1 strict 离线拒答：完全离线档下平台外话题不调任何模型，本地固定文案。
    if _strict_offline(state) and _offline_offtopic(user_text):
        async def _strict_refusal_stream():
            yield {"type": "turn_start", "connection": req.connection_id}
            yield {"type": "text", "content": _strict_refusal_text()}
            yield {"type": "done"}
        async for ev in _strict_refusal_stream():
            yield ev
        return
    # T5.4 问题库快路径：原文命中 → 零模型确认卡（不进 harness 循环）
    try:
        matched = state.questions.match(req.connection_id, user_text)
    except Exception:
        matched = None
    if matched:
        async for ev in _question_library_stream(state, req, matched):
            yield ev
        return
    # 默认：harness 单循环（自带 manifest/scene_done/done，不再补发）
    async for ev in harness_stream(state, req, req._server_history, pf):
        yield ev


async def _question_library_stream(state: "AppState", req: ChatRequest,
                                    matched: dict) -> AsyncIterator[dict[str, Any]]:
    """T5.4 问题库命中：亮 SQL 确认卡（用户点执行才跑），零模型调用。"""
    conn_id = req.connection_id
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
    yield {"type": "manifest", "manifest": {"tables": matched.get("tables", []), "kb_docs": 0, "history_turns": _hist_q, "include_data": False, "redactions": [], "mode": _mode_q, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": "question_library", "provider": "question_library"}}
    yield {"type": "sql_card", "card": card_q}
    yield {"type": "text", "content": f"已从问题库命中“{matched.get('question','')[:24]}”，请确认后执行（零模型调用）。"}
    yield {"type": "_commit", "messages": [
        {"role": "assistant", "kind": "sql_card", "content": json.dumps(card_q, ensure_ascii=False),
         "sql": card_q.get("sql", ""), "verdict": card_q.get("verdict", "")},
        {"role": "assistant", "kind": "text", "content": f"已从问题库命中“{matched.get('question','')[:24]}”，请确认后执行（零模型调用）。"},
    ]}
    yield {"type": "done"}


async def task_runner(state: "AppState", req: ChatRequest, ctx: Context,
                      task: TaskSpec) -> AsyncIterator[dict[str, Any]]:
    """单任务执行器：TaskSpec → report_stream / controlled_step_stream。

    - modality=report → 受控报告管线（保留 report_id/章节事件）
    - 其余 → 受控步骤循环（步骤指令注入 + 按动作收窄工具集，见 harness.py）
    - 前序任务结果经 ctx 注入子请求（req._prior_results），由 _prepare 组装进上下文
    """
    from app.ai.harness import controlled_step_stream
    # 任务级 target 传递（_prepare 并入检索种子/步骤指令）
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
    if task.modality == "report":
        req.mode = MODE_REPORT
        async for ev in report_stream(state, req):
            # done 延迟到 task_result 之后（协议兼容）
            if ev.get("type") == "done":
                continue
            yield ev
    else:
        req.mode = MODE_QUERY
        req._step_action = task.action or "query"  # type: ignore[attr-defined]
        try:
            req._prior_results = {  # type: ignore[attr-defined]
                k: (v.to_dict() if hasattr(v, "to_dict") else v)
                for k, v in ctx.task_results.items()
            }
        except Exception:
            req._prior_results = {}  # type: ignore[attr-defined]
        async for ev in controlled_step_stream(state, req):
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
    # M3 修复：骨架补 options（S3-1 追加项）与 empty_hint（空结果反馈），追问轮模型可感知
    if card.get("options"):
        legend += f"；options={card['options']}"
    if card.get("empty_hint"):
        legend += f"；empty_hint={card['empty_hint']}"
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


# ── 断连 carry-over（chat_ws/<sid>/state.json）：中断摘要一次性注入，用后即清 ──
def _chat_ws_dir(state: "AppState", sid: str | None):
    if not sid:
        return None
    try:
        from app.config import get_env as _ge

        d = _ge().data_dir / "chat_ws" / sid
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _pop_carryover_note(state: "AppState", sid: str | None) -> str:
    """读取并清除上一轮中断摘要（有则返回注入文本，无则空串）。"""
    d = _chat_ws_dir(state, sid)
    if d is None:
        return ""
    f = d / "state.json"
    try:
        import json as _json

        st = _json.loads(f.read_text(encoding="utf-8"))
        note = st.pop("last_interrupt", None)
        if note:
            f.write_text(_json.dumps(st, ensure_ascii=False), encoding="utf-8")
            ts = note.get("ts") or ""
            return (f"⚠ 上一轮回复因连接中断未完成（{ts}）。"
                    f"请基于已有上下文继续回答用户的上一个问题，不要重复寒暄。")
    except Exception:
        pass
    return ""


def _save_carryover(state: "AppState", sid: str | None, conn_id: str) -> None:
    """断连时记录中断摘要（供下一次请求注入）。"""
    d = _chat_ws_dir(state, sid)
    if d is None:
        return
    f = d / "state.json"
    try:
        import json as _json

        try:
            st = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        st["last_interrupt"] = {
            "reason": "客户端断开或请求超时",
            "conn_id": conn_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        f.write_text(_json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _ai_messages_from_events(events: list[dict]) -> list[dict]:
    """committed-turn 转换器（实现在 events_codec，避免 loop↔report 循环导入）。"""
    from app.ai.events_codec import ai_messages_from_events

    return ai_messages_from_events(events)
