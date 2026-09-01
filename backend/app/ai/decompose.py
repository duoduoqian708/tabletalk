"""E2 意图分解层（设计 §13.2/§13.3）：意图识别 = LLM 产出 TaskPlan。

2026-09 修订：意图识别不再用关键词匹配（关键词误判多、复合请求无法拆），
统一由 LLM 产出 TaskPlan（长度 ≥1，复合请求拆多个独立任务；offsetopic→unknown 走拒答）。
- mock / 严格离线（无 LLM）：确定性降级为单任务 query（不调模型）
- 脱敏/清单/审计在 preflight 准备（redacted_q/manifest），本模块的 LLM 调用复用——
  全链路 LLM 出网都用脱敏原文 + 记清单（隐私红线）
- 未来向量层：语义命中直接复用执行路径（相同语义问题跳过 LLM），关键词层不恢复
plan 级辅助字段（tags/followup_tables/skip_retrieval）沿用 preflight 检测逻辑。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.ai.plan import TaskPlan, TaskSpec

if TYPE_CHECKING:
    from app.state import AppState

# 结构问答/审计类 → skip_retrieval（跳过向量检索管线；提示优化，非意图分类）
_SKIP_RETRIEVAL_RE = re.compile(
    r"有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全",
    re.IGNORECASE,
)


def _parse_llm_plan(text: str, confirmed_tags: list[str]) -> TaskPlan:
    """LLM JSON → TaskPlan。非法/空任务 → query 兜底；tags 只留已确认。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        data = json.loads(t)
        if not isinstance(data, dict):
            raise ValueError("not dict")
        # §13.4 意图澄清优先：LLM 返回 clarify 候选问题（意图不完整/不清晰）→
        # 空任务 + 澄清清单（执行前刹停），不校验 tasks
        clarify = [str(x).strip() for x in data.get("clarify", []) if str(x).strip()][:3]
        if clarify:
            tags_raw = data.get("tags", [])
            tags = [str(x).strip() for x in tags_raw if str(x).strip() in confirmed_tags][:3]
            return TaskPlan(tasks=[], clarify=clarify, tags=tags, raw_question="")
        raw_tasks = data.get("tasks", [])
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError("empty tasks")
        tasks = [TaskSpec.from_dict({**x, "id": f"t{i+1}"})
                 for i, x in enumerate(raw_tasks) if isinstance(x, dict)]
        if not tasks:
            raise ValueError("no valid tasks")
        tags_raw = data.get("tags", [])
        tags = [str(x).strip() for x in tags_raw if str(x).strip() in confirmed_tags][:3]
        return TaskPlan(tasks=tasks, tags=tags, raw_question="")
    except Exception:
        return TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                        degraded=True)


async def decompose(state: "AppState", conn_id: str, question: str,
                    confirmed_tags: list[str] | None = None,
                    history_tail: list[dict] | None = None,
                    redacted_q: str | None = None,
                    manifest: dict[str, Any] | None = None) -> TaskPlan:
    """意图分解统一入口（2026-09：LLM 唯一意图路径，无关键词层）。

    - LLM 产出 TaskPlan（长度 ≥1，复合请求可拆；offtopic → unknown → refusal 拒答）
    - mock / 严格离线（无 LLM）：确定性单任务 query 降级（零模型调用）
    - 出网隐私：prompt 用 preflight 脱敏后的 redacted_q；清单/审计随 manifest 走中央记账
      （loop.stream 已先跑 preflight，此处不重复脱敏/清单）
    - 未来向量层：语义命中直接复用执行路径（相同语义问题跳过 LLM），不恢复关键词层
    """
    q = (question or "").strip()
    if not q:
        return TaskPlan(degraded=True)

    # plan 级辅助字段（沿用 preflight 检测逻辑）
    followup_tables: list[str] = []
    skip_retrieval = bool(_SKIP_RETRIEVAL_RE.search(q)) if q else False
    try:
        if history_tail:
            from app.ai.preflight import _extract_followup_tables
            followup_tables = _extract_followup_tables(history_tail) or []
    except Exception:
        pass

    if confirmed_tags is None:
        confirmed_tags = []
        try:
            lib = state.knowledge.tags(conn_id).get("library", [])
            confirmed_tags = [t["name"] for t in lib if t.get("status") == "confirmed" and t.get("name")]
        except Exception:
            pass
    _kw_tags = [t for t in confirmed_tags if t.lower() in q.lower()][:3]

    # 判断是否 mock / 严格离线（无 LLM：确定性单任务 query，不调模型）
    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg as _resolve
        provider_cfg = _resolve(state, None)
        is_mock = gw.is_effective_mock(provider_cfg)
    except Exception:
        try:
            from app.ai import gateway as gw
            provider_cfg = state.runtime.get().provider_config()
            is_mock = gw.is_effective_mock(provider_cfg)
        except Exception:
            is_mock = True

    if is_mock:
        # 无 LLM 可用：确定性降级单任务 query（LLM 路径在真实 provider 下才可达）
        return TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                        tags=_kw_tags, followup_tables=followup_tables,
                        skip_retrieval=skip_retrieval, degraded=True, raw_question=q)

    # 唯一 LLM 意图调用：产出 TaskPlan（复合请求可拆，§13.3 语义分解）
    prompt = _build_prompt(redacted_q or q, confirmed_tags)
    try:
        from app.ai import gateway as gw
        provider = gw.build_provider(provider_cfg)
        # 出网隐私：复用 preflight 的清单/审计（manifest/redactions），中央记账统一写
        _ctx: dict[str, Any] = {}
        if manifest:
            _ctx["manifest"] = manifest
        if manifest is None:
            _ctx = {"conn_id": conn_id, "connection": conn_id or "__intent__",
                    "skill": "decompose", "source": "egress", "status": "egress-intent",
                    "context_meta": {"candidate_tables": [], "kb_docs": 0}}
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None, ctx=_ctx)
        text = (getattr(resp, "content", "") or "").strip()
        p = _parse_llm_plan(text, confirmed_tags)
        p.followup_tables = followup_tables
        p.skip_retrieval = skip_retrieval
        p.raw_question = q
        return p
    except Exception:
        return TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                        tags=_kw_tags, followup_tables=followup_tables,
                        skip_retrieval=skip_retrieval, degraded=True, raw_question=q)


def _build_prompt(question: str, confirmed: list[str]) -> str:
    confirmed_str = ", ".join(confirmed) if confirmed else "（暂无已确认标签）"
    return (
        "你是 TableTalk 的意图分解器。把用户请求分解为任务列表（TaskPlan）。\n"
        f"用户问题：{question}\n"
        f"可用领域标签（只从中选 0~3 个，已确认）：{confirmed_str}\n"
        "分解规则：\n"
        "- 独立操作才拆任务：'查订单和利润'（同一 SQL 取两列）→ 1 个任务；"
        "'查一下订单，顺便看看用户增长' → 2 个任务（不同查询）\n"
        "- action 封闭枚举：query（查数据/结构/审计）/ write（DML）/ ddl（改结构）/ "
        "kb（知识库图谱）/ schedule（定时任务）/ system / unknown\n"
        "- modality 有序枚举：answer（直接回答）/ analyze（统计分析）/ report（结构化报告）/ automate（自动化）\n"
        "- target：{tables: [表名]} 自由抽取（不确定可空）\n"
        "意图不完整/不清晰时（缺目标、范围不明、空指代、信息冲突）：不输出 tasks，"
        "改为输出 clarify 候选问题（1~3 个），让用户先确认。\n"
        '只返回 JSON：{"tasks": [{"action": "query", "modality": "answer", "target": {"tables": []}}], "tags": ["标签"], "clarify": ["澄清问题"]}\n'
        "示例：\n"
        'Q: 查一下订单总数 → {"tasks":[{"action":"query","modality":"answer","target":{"tables":["orders"]}}],"tags":[]}\n'
        'Q: 生成销售趋势报告 → {"tasks":[{"action":"query","modality":"report"}],"tags":[]}\n'
        'Q: 查订单，顺便看下用户增长 → {"tasks":[{"action":"query","modality":"answer","target":{"tables":["orders"]}},{"action":"query","modality":"analyze","target":{"tables":["users"]}}],"tags":[]}\n'
        'Q: 删掉测试订单 → {"tasks":[{"action":"write","modality":"answer"}],"tags":[]}\n'
        "拿不准 action 返回 unknown；tasks 不能为空。"
    )