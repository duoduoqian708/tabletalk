"""E2 意图分解层（设计 §13.2/§13.3）：LLM TaskPlan / 关键词快判 / mock 降级。

意图是剖面不是标签：单次 LLM 调用产出 TaskPlan（长度 ≥1，复合请求拆多个独立任务）。
plan 级辅助字段（tags/followup_tables/skip_retrieval）沿用 preflight 检测逻辑；
脱敏/清单/审计由调用方（loop.stream）统一走 preflight 管道（本模块不做第二次）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.ai.plan import TaskPlan, TaskSpec

if TYPE_CHECKING:
    from app.state import AppState

# 关键词快判表（高特异性 → 低特异性，query 兜底最后）。
# 每项：(compiled regex, action, modality)
KEYWORD_PLAN_SPECS: list[tuple[re.Pattern, str, str]] = [
    # offtopic：平台外话题 → unknown（general 承接拒答引导）
    (re.compile(r"你好|谢谢|天气|笑话|你是谁|自我介绍|写诗|讲笑话|闲聊", re.IGNORECASE), "unknown", "answer"),
    # report：章节化报告/趋势分析（modality 升降级）
    (re.compile(r"报告|出一份|趋势分析|概览|分析报告|总结报告|dashboard|insights?", re.IGNORECASE), "query", "report"),
    # knowledge → kb
    (re.compile(r"知识|注释|注解|图谱|关联图|表关系|标签|domain|knowledge", re.IGNORECASE), "kb", "answer"),
    # scheduler → schedule
    (re.compile(r"定时|每天|每周|每月|调度|cron|周期|自动跑|定期", re.IGNORECASE), "schedule", "answer"),
    # ddl：结构变更（P2-9：与 write 分离，对齐 LLM 路径的 ddl action → 独立 ddl 技能）
    (re.compile(r"建表|建一张表|加.*列|新增字段|加.*索引|create\s+table|alter\s+table|drop\s+table|create\s+index|索引|加.*索引|删除.*表|删表", re.IGNORECASE), "ddl", "answer"),
    # write：DML 写操作（保守判定，"看看"不算写）
    (re.compile(r"删掉|删除|改成|改为|更新.*为|插入|写入|修改.*为|涨价|提价|update|delete\s+from|insert\s+into", re.IGNORECASE), "write", "answer"),
    # query：兜底数据面
    (re.compile(r"查|统计|多少|平均|分组|排序|列表|按.*月|查询|select\s|有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全", re.IGNORECASE), "query", "answer"),
]

# 结构问答/审计类 → skip_retrieval（跳过向量检索管线）
_SKIP_RETRIEVAL_RE = re.compile(
    r"有哪些表|什么结构|表结构|schema|describe|show\s+tables|审计|我刚才|操作记录|被拦|被.*拦截|audit|审查|安全",
    re.IGNORECASE,
)


def _keyword_plan(question: str) -> TaskPlan:
    """关键词快判 → 单任务 TaskPlan（零 LLM，高特异性优先）。"""
    q = (question or "").strip()
    if not q:
        return TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                        degraded=True, raw_question=q)
    # 特殊反例：问候语 + 查询词 → query（高特异性优先，"你好，查一下订单"不是闲聊）
    if re.search(r"你好", q, re.IGNORECASE) and re.search(r"查|统计|多少|select|查询", q, re.IGNORECASE):
        return TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                        degraded=True, raw_question=q)
    for pat, action, modality in KEYWORD_PLAN_SPECS:
        if pat.search(q):
            # 写/DDL 边界反例：含"看看"且无强写词 → 降 query（查 schema 的意图）
            if action in ("write", "ddl") and "看看" in q and not re.search(
                    r"删掉|改成|删除|插入|更新|提价|涨价|alter|drop|create",
                    q, re.IGNORECASE):
                action = "query"
            return TaskPlan(tasks=[TaskSpec(action=action, modality=modality)],
                            degraded=True, raw_question=q)
    return TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                    degraded=True, raw_question=q)


def _parse_llm_plan(text: str, confirmed_tags: list[str]) -> TaskPlan:
    """LLM JSON → TaskPlan。非法/空任务 → query 兜底；tags 只留已确认。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        data = json.loads(t)
        if not isinstance(data, dict):
            raise ValueError("not dict")
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
                    history_tail: list[dict] | None = None) -> TaskPlan:
    """意图分解统一入口：高特异关键词快判 → LLM TaskPlan → mock 降级。

    - 高特异度关键词（write/kb/schedule/report/offtopic）→ 直接单任务（零 LLM）
    - query 类低特异度 → 非 mock 时放行 LLM 分解（复合请求可达，§13.3）；mock 单任务
    - plan 级辅助字段：tags（已确认过滤）、followup_tables、skip_retrieval
    """
    q = (question or "").strip()
    if not q:
        return TaskPlan(degraded=True)

    # 追问轮种子（沿用 preflight 检测）
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

    # 高特异度关键词快判：命中即单任务（零 LLM，write/kb/schedule/report/offtopic）
    kw = _keyword_plan(q)
    if kw.tasks[0].action != "query" or kw.tasks[0].modality != "answer":
        kw.tags = _kw_tags
        kw.followup_tables = followup_tables
        kw.skip_retrieval = skip_retrieval
        return kw

    # 判断是否 mock（mock 下 query 类也零 LLM 单任务）
    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg as _resolve
        provider_cfg = _resolve(state, None)  # P3：resolve 仅 getattr(req,"model_id")，None 即可
        is_mock = gw.is_effective_mock(provider_cfg)
    except Exception:
        try:
            from app.ai import gateway as gw
            provider_cfg = state.runtime.get().provider_config()
            is_mock = gw.is_effective_mock(provider_cfg)
        except Exception:
            is_mock = True

    if is_mock:
        # F3：query 类在 mock 下单任务（零 LLM 降级）
        p = TaskPlan(tasks=[TaskSpec(action="query", modality="answer")],
                     tags=_kw_tags, followup_tables=followup_tables,
                     skip_retrieval=skip_retrieval, degraded=True, raw_question=q)
        return p

    # 真实 LLM 单次调用 → TaskPlan（query 类放行：复合请求可拆，§13.3 语义分解）
    prompt = _build_prompt(q, confirmed_tags)
    try:
        from app.ai import gateway as gw
        provider = gw.build_provider(provider_cfg)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
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
        '只返回 JSON：{"tasks": [{"action": "query", "modality": "answer", "target": {"tables": []}}], "tags": ["标签"]}\n'
        "示例：\n"
        'Q: 查一下订单总数 → {"tasks":[{"action":"query","modality":"answer","target":{"tables":["orders"]}}],"tags":[]}\n'
        'Q: 生成销售趋势报告 → {"tasks":[{"action":"query","modality":"report"}],"tags":[]}\n'
        'Q: 查订单，顺便看下用户增长 → {"tasks":[{"action":"query","modality":"answer","target":{"tables":["orders"]}},{"action":"query","modality":"analyze","target":{"tables":["users"]}}],"tags":[]}\n'
        'Q: 删掉测试订单 → {"tasks":[{"action":"write","modality":"answer"}],"tags":[]}\n'
        "拿不准 action 返回 unknown；tasks 不能为空。"
    )