"""Preflight 统一意图层：关键词快判 → 单次 LLM {intent, tags} → 超时降级，单次脱敏/清单/审计。

对应 08 §3 / implementation T1.1-T1.3。KB 冷启动与超时数值 two points 冻结：超时默认 2.0s 可配。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.state import AppState

# ---- 常量 ----
VALID_INTENTS = {"query", "write", "report", "knowledge", "scheduler", "offtopic"}
# 关键词快判表（集中顶部便于测试）。顺序：高特异性 → 低特异性，query 兜底最后。
# 每项：(compiled regex, intent)
_KEYWORD_RULES: list[tuple[re.Pattern, str]] = [
    # offtopic：平台外话题 → refusal 技能（最先匹配，避免"你好查一下"被判 query）
    (re.compile(r"你好|谢谢|天气|笑话|你是谁|自我介绍|写诗|讲笑话|闲聊", re.IGNORECASE), "offtopic"),
    # report：章节化报告/趋势分析
    (re.compile(r"报告|出一份|趋势分析|概览|分析报告|总结报告|dashboard|insights?", re.IGNORECASE), "report"),
    # knowledge：知识库/图谱相关
    (re.compile(r"知识|注释|注解|图谱|关联图|表关系|标签|domain|knowledge", re.IGNORECASE), "knowledge"),
    # scheduler：定时任务
    (re.compile(r"定时|每天|每周|每月|调度|cron|周期|自动跑|定期", re.IGNORECASE), "scheduler"),
    # write：写操作（保守判定，"看看"不算写）
    (re.compile(r"删掉|删除|改成|更新.*为|插入|写入|修改.*为|涨价|提价|update|delete\s+from|insert\s+into|建表|加.*列|新增字段|create\s+table|alter\s+table|drop\s+table|create\s+index|索引", re.IGNORECASE), "write"),
    # query：兜底数据面（含结构问答、审计回顾、安全审查）
    (re.compile(r"查|统计|多少|平均|分组|排序|列表|按.*月|查询|select\s|有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全", re.IGNORECASE), "query"),
]

PREFLIGHT_TIMEOUT = 2.0  # 秒，待 Q2 确认；可配 via env TABLETALK_PREFLIGHT_TIMEOUT


def _get_timeout() -> float:
    try:
        import os
        v = os.getenv("TABLETALK_PREFLIGHT_TIMEOUT", "")
        if v:
            return float(v)
    except Exception:
        pass
    return PREFLIGHT_TIMEOUT


@dataclass
class PreflightResult:
    intent: str  # query|write|report|knowledge|scheduler|offtopic
    tags: list[str] = field(default_factory=list)
    degraded: bool = False  # True = 走了关键词降级（未调 LLM 或超时/解析失败）
    is_followup: bool = False
    followup_tables: list[str] = field(default_factory=list)
    skip_retrieval: bool = False  # True = 结构问答/审计类，跳过向量检索管线


def _get_confirmed_tags(state: "AppState", conn_id: str) -> list[str]:
    try:
        lib = state.knowledge.tags(conn_id).get("library", [])
        return [t["name"] for t in lib if t.get("status") == "confirmed" and t.get("name")]
    except Exception:
        return []


def _keyword_intent(question: str) -> str | None:
    q = (question or "").strip()
    if not q:
        return None
    for pat, intent in _KEYWORD_RULES:
        if pat.search(q):
            return intent
    return None


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


def _build_prompt(redacted_q: str, history_text: str, confirmed: list[str]) -> str:
    confirmed_str = ", ".join(confirmed) if confirmed else "（暂无已确认标签）"
    return (
        "你是 TableTalk 的意图分类器。只做两件事：判意图 + 选领域标签。\n"
        f"用户当前问题（已脱敏）：{redacted_q}\n"
        f"{history_text}"
        f"可用领域标签（只从中选 0~3 个，已确认）：{confirmed_str}\n"
        "意图集（单标签，封闭）：\n"
        "- query：数据查询 / 表结构问答 / 审计回顾 / SQL安全审查（查、统计、多少、有哪些表、审计、审查）\n"
        "- write：数据写操作 + 表结构变更（删掉、改成、更新为、插入、建表、加列、建索引）→ 需确认\n"
        "- report：要一份结构化分析报告/概览/趋势（报告、出一份、趋势分析）\n"
        "- knowledge：知识库 / 图谱相关（注释、标签、表间关联、知识）\n"
        "- scheduler：定时任务管理（定时、每天、周期、自动跑）\n"
        "- offtopic：平台外话题（你好、天气、笑话、写诗）→ 拒答引导\n"
        "相邻对边界：\n"
        "- query/report：'看看订单'是query不是report；'生成月度报告'是report\n"
        "- query/write：'看看测试订单'是query（'看看'不算写），'删掉测试订单'才是write；宁可漏进query\n"
        "- query/knowledge：'表有哪些注释'是query；'帮我加个注释'是knowledge\n"
        "- offtopic：'你好'是offtopic，'你好，查一下订单'是query\n"
        '只返回 JSON：{"intent": "query|write|report|knowledge|scheduler|offtopic", "tags": ["标签1"]}。拿不准 intent 返回 query。\n'
        "示例：\n"
        'Q: 查一下订单总数 → {"intent":"query","tags":[]}\n'
        'Q: 看看测试订单 → {"intent":"query","tags":[]}\n'
        'Q: 有哪些表 → {"intent":"query","tags":[]}\n'
        'Q: 最近有什么操作被拦了 → {"intent":"query","tags":[]}\n'
        'Q: 生成销售趋势报告 → {"intent":"report","tags":[]}\n'
        'Q: 删掉测试订单 → {"intent":"write","tags":[]}\n'
        'Q: 加一列备注 → {"intent":"write","tags":[]}\n'
        'Q: 这张表的业务含义是什么 → {"intent":"knowledge","tags":[]}\n'
        'Q: 帮我加个注释 → {"intent":"knowledge","tags":[]}\n'
        'Q: 每天9点跑一次统计 → {"intent":"scheduler","tags":[]}\n'
        'Q: 今天天气怎么样 → {"intent":"offtopic","tags":[]}\n'
    )


def _parse_llm(text: str, confirmed: list[str]) -> tuple[str, list[str]]:
    t = (text or "").strip()
    # 去 markdown  fence
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            intent = str(data.get("intent", "")).strip().lower()
            tags_raw = data.get("tags", [])
            if not isinstance(tags_raw, list):
                tags_raw = []
            tags = [str(x).strip() for x in tags_raw if str(x).strip() in confirmed]
            # 兼容部分模型返回 report/query 外的大小写
            if intent not in VALID_INTENTS:
                # 尝试模糊匹配
                for v in VALID_INTENTS:
                    if v in intent:
                        return v, tags[:3]
                return "query", tags[:3]
            return intent, tags[:3]
    except Exception:
        pass
    # 回退：文本中找意图词
    tl = t.lower()
    for v in VALID_INTENTS:
        if v in tl:
            return v, []
    return "query", []


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
        manifest = {"tables": [], "kb_docs": 0, "history_turns": 1, "include_data": False, "redactions": redactions, "mode": "standard", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": provider_cfg.get("model",""), "provider": provider_cfg.get("provider","mock")}

    # 出网清单审计已由中央记账拦截器（gateway）统一写：LLM 调用一次，egress-intent 一 条
    # （关键词快判路径不调 LLM，故不再产生 egress 行 —— 无出网即无清单）

    # 关键词快判：多数请求 0 次 LLM 直出（高置信直接返回）
    kw = _keyword_intent(redacted_q if redacted_q else q)
    # 若命中且非"query 兜底"的模糊情况，直接返回（downgrade=True）
    if kw is not None:
        # 防误判：query 的兜底不应在"拿不准"时直接命中，需确认非 mock 下 LLM 仍有机会；
        # 但按 08 "多数请求 0 次 LLM" → 关键词命中即直接出
        tags = _keyword_tags(q, confirmed)
        # 追问轮：若是 write 且为 followup 短句，保守降为 query（写意图保守）
        if kw == "write" and is_followup and len(q) < 20 and q.startswith(("那","按","再")):
            # "那按周统计呢" 不应判 write
            kw = "query"
        # skip_retrieval：结构问答/审计回顾/安全审查 → 跳过向量检索管线
        _skip = bool(re.search(r"有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全", q, re.IGNORECASE)) if kw == "query" else False
        return PreflightResult(intent=kw, tags=tags, degraded=True, is_followup=is_followup, followup_tables=followup_tables, skip_retrieval=_skip)

    # 无关键词命中 → LLM 路径
    if is_mock:
        # strict / mock 下不调模型，直接 query
        tags = _keyword_tags(q, confirmed)
        _skip = bool(re.search(r"有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全", q, re.IGNORECASE))
        return PreflightResult(intent="query", tags=tags, degraded=True, is_followup=is_followup, followup_tables=followup_tables, skip_retrieval=_skip)

    # 真实 LLM 单次调用（≤2s）
    try:
        from app.ai import gateway as gw
        provider = gw.build_provider(provider_cfg)
        # 对话尾部：最后 user + 上轮 assistant 结尾 200 字
        history_text = ""
        if history_tail:
            last_user = ""
            last_assistant = ""
            for m in reversed(history_tail):
                if not isinstance(m, dict):
                    continue
                role = m.get("role", "")
                content = str(m.get("content", "") or "")
                if role == "user" and not last_user and content.strip():
                    last_user = content.strip()[-300:]
                if role == "assistant" and not last_assistant and content.strip():
                    last_assistant = content.strip()[-200:]
                if last_user and last_assistant:
                    break
            if last_user:
                history_text += f"对话尾部-上轮用户：{last_user}\n"
            if last_assistant:
                history_text += f"对话尾部-上轮助手结尾：{last_assistant}\n"
        prompt = _build_prompt(redacted_q, history_text, confirmed)
        timeout = _get_timeout()
        resp = await asyncio.wait_for(provider.chat(
            [{"role": "user", "content": prompt}], tools=None,
            # 中央记账：拦截器统一写一条 egress-intent（保持原验收契约：非关键词路径恰 1 条）
            ctx={
                "conn_id": conn_id, "connection": conn_id if conn_id else "__intent__",
                "skill": "preflight", "source": "egress", "status": "egress-intent",
                "context_meta": {"candidate_tables": [], "kb_docs": 0},
                "redactions": redactions if redactions else [],
                "manifest": manifest,
            },
        ), timeout=timeout)
        text = (getattr(resp, "content", "") or "").strip()
        intent, tags = _parse_llm(text, confirmed)
        # 非法值兜底：拿不准当 query（D4）
        if intent not in VALID_INTENTS:
            intent = "query"
            return PreflightResult(intent=intent, tags=tags, degraded=True, is_followup=is_followup, followup_tables=followup_tables)
        # 关键跨类反例：query/write 边界（写保守）
        # 若 LLM 误判 write 但问题含 "看看" 且无 "删掉/改成"，降回 query
        if intent == "write" and "看看" in q and not any(k in q for k in ("删掉","改成","删除","插入","更新")):
            intent = "query"
        _skip = bool(re.search(r"有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全", q, re.IGNORECASE)) if intent == "query" else False
        return PreflightResult(intent=intent, tags=tags, degraded=False, is_followup=is_followup, followup_tables=followup_tables, skip_retrieval=_skip)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        tags = _keyword_tags(q, confirmed)
        return PreflightResult(intent="query", tags=tags, degraded=True, is_followup=is_followup, followup_tables=followup_tables)
    except Exception:
        tags = _keyword_tags(q, confirmed)
        return PreflightResult(intent="query", tags=tags, degraded=True, is_followup=is_followup, followup_tables=followup_tables)
