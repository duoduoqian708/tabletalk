"""Preflight 准备层（2026-09 改版）：plan 级辅助字段 + 脱敏/清单/审计。

意图识别**不再在本层做**（关键词与 LLM 都已移除）——意图统一由 decompose 的 LLM
产出 TaskPlan。本层负责：
- 追问轮检测（is_followup/followup_tables）、已确认标签匹配（tags，检索种子）
- skip_retrieval：结构问答/审计类跳过向量检索管线（提示优化，非意图分类）
- 单次脱敏 + 清单 + 审计准备，供 decompose 的 LLM 调用复用（全链路 LLM 出网用脱敏原文）
"""
from __future__ import annotations

import json
import re
from app.core.timeutil import utcnow_iso
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.state import AppState


@dataclass
class PreflightResult:
    intent: str = ""  # 2026-09：意图归 decompose 产出；本层不填（loop 用 plan 回填供事件/度量）
    tags: list[str] = field(default_factory=list)
    degraded: bool = False  # 本层不再调 LLM，恒 False（意图降级由 decompose 的 degraded 表达）
    is_followup: bool = False
    followup_tables: list[str] = field(default_factory=list)
    skip_retrieval: bool = False  # True = 结构问答/审计类，跳过向量检索管线
    redacted_q: str = ""          # 2026-09：脱敏后原文（decompose 的 LLM 调用复用）
    manifest: dict[str, Any] | None = None  # 2026-09：出网清单（decompose 复用）


def _get_confirmed_tags(state: "AppState", conn_id: str) -> list[str]:
    try:
        lib = state.knowledge.tags(conn_id).get("library", [])
        return [t["name"] for t in lib if t.get("status") == "confirmed" and t.get("name")]
    except Exception:
        return []


def _keyword_tags(question: str, confirmed: list[str]) -> list[str]:
    if not confirmed or not question.strip():
        return []
    ql = question.lower()
    out: list[str] = []
    for t in confirmed:
        tl = t.lower()
        if tl in ql or ql in tl:
            out.append(t)
    return out[:3]


def _extract_followup_tables(history_tail: list[dict] | None) -> list[str]:
    if not history_tail:
        return []
    tables: list[str] = []
    # 兼容两种历史形态：① 结构化 card.tables ② tool result JSON 里的 tables ③ candidate_tables
    for m in reversed(history_tail):
        if not isinstance(m, dict):
            continue
        # 直接带 tables 字段（T1.3 过渡期前端 history 形态）
        for key in ("tables", "candidate_tables", "followup_tables"):
            v = m.get(key)
            if isinstance(v, (list, tuple)) and v:
                return [str(x) for x in v][:20]
        # 尝试解析 content 中的 JSON
        content = m.get("content", "")
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                obj = json.loads(content)
                for key in ("tables", "candidate_tables"):
                    v = obj.get(key)
                    if isinstance(v, (list, tuple)) and v:
                        return [str(x) for x in v][:20]
                # sql_card 形态
                card = obj.get("card") if isinstance(obj.get("card"), dict) else None
                if card:
                    for key in ("tables",):
                        v = card.get(key)
                        if isinstance(v, (list, tuple)) and v:
                            return [str(x) for x in v][:20]
            except Exception:
                pass
        # 上轮 card.tables 形态：content 可能是 card JSON 串
        # 也支持 m 本身就是 card（loop 的 messages 里 tool 产物的上游）
        if m.get("card") and isinstance(m["card"], dict):
            v = m["card"].get("tables")
            if isinstance(v, (list, tuple)) and v:
                return [str(x) for x in v][:20]
    return tables


def _detect_followup(question: str, confirmed: list[str], history_tail: list[dict] | None) -> tuple[bool, list[str]]:
    followup_tables = _extract_followup_tables(history_tail)
    if not followup_tables:
        return False, []
    q = (question or "").strip()
    if not q:
        return False, []
    # 以"那|再|换成|也|按"开头 视为追问
    starts_followup = q.startswith(("那", "再", "换成", "也", "按", "改成按"))
    if starts_followup:
        return True, followup_tables
    ql = q.lower()
    has_domain = any(t.lower() in ql for t in confirmed) if confirmed else False
    if has_domain:
        return False, []
    # 无领域词时：仅极短句（<10字）且不含强查询动词才视为追问，避免把"查一下产品销量"误判
    if len(q) < 10:
        # 强查询动词出现则视为新问而非追问
        if any(k in q for k in ("查", "统计", "查询", "看看", "多少", "平均")):
            return False, []
        return True, followup_tables
    return False, []


async def preflight(
    state: "AppState",
    conn_id: str,
    question: str,
    history_tail: list[dict] | None = None,
) -> PreflightResult:
    """统一 preflight：关键词快判 → 单次 LLM {intent,tags} → 超时降级；全程单次脱敏/清单/审计。"""
    q = (question or "").strip()
    if not q:
        return PreflightResult(intent="query", tags=[], degraded=True, is_followup=False, followup_tables=[])

    confirmed = _get_confirmed_tags(state, conn_id)
    is_followup, followup_tables = _detect_followup(q, confirmed, history_tail)

    # 解析 provider_cfg（复用 gateway 的 B4 strict 强制 mock 逻辑）
    provider_cfg: dict[str, Any] = {}
    try:
        from app.ai.provider_cfg import resolve_provider_cfg as _resolve
        class _Req:
            pass
        _req = _Req()
        # 让 resolve 能读到 privacy_mode（严格档强制 mock）
        provider_cfg = _resolve(state, _req)
    except Exception:
        try:
            provider_cfg = state.runtime.get().provider_config()
        except Exception:
            provider_cfg = {"provider": "mock", "model": "mock"}

    is_mock = False
    try:
        from app.ai import gateway as gw
        is_mock = gw.is_effective_mock(provider_cfg)
    except Exception:
        is_mock = provider_cfg.get("provider") == "mock"

    # 单次脱敏（B2）
    redacted_q = q
    redactions: list[str] = []
    try:
        from app.safety.redact import get_salt, redact_text
        from app.config import get_env
        salt = get_salt(get_env().data_dir)
        rq, mp = redact_text(q, salt, [])
        redacted_q = rq
        if mp:
            redactions = list(mp.keys())[:5]
    except Exception:
        redacted_q = q

    # 单次清单 + 审计（B1，source=egress-intent）
    manifest: dict[str, Any] = {}
    try:
        from app.ai.manifest import build_manifest
        manifest = build_manifest(state, conn_id or "__intent__", {"candidate_tables": [], "kb_docs": 0}, [{"role": "user", "content": question}], False, provider_cfg, "")
        if redactions:
            manifest["redactions"] = redactions
    except Exception:
        manifest = {"tables": [], "kb_docs": 0, "history_turns": 1, "include_data": False, "redactions": redactions, "mode": "standard", "ts": utcnow_iso(), "model": provider_cfg.get("model",""), "provider": provider_cfg.get("provider","mock")}

    # 2026-09：意图识别归 decompose（LLM TaskPlan），本层不再做关键词/LLM 意图分类。
    # 只产出 plan 级字段 + 脱敏原文 + 清单，供 loop/decompose 复用。
    _skip = bool(re.search(
        r"有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全",
        q, re.IGNORECASE)) if q else False
    return PreflightResult(tags=_keyword_tags(q, confirmed),
                           is_followup=is_followup,
                           followup_tables=followup_tables,
                           skip_retrieval=_skip,
                           redacted_q=redacted_q,
                           manifest=manifest)
