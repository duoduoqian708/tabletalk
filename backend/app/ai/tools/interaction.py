"""交互/受控流工具：ask_user（结构化澄清）+ propose_plan（受控流入口）。

仅 Harness 默认对话流向模型暴露（受控步骤非交互，工具集不含它们）。
- ask_user：模型主动澄清，结束当前回合等用户回答（服务端历史自动衔接）。
- propose_plan：模型判断需要写操作/编排时提议计划 → 落 pending_plans → 人审确认后
  才进入受控执行器（先审后动）。evidence_summary 强制：没看过证据不许提计划。
"""
from __future__ import annotations

import uuid

from app.ai.tools.registry import ToolOutcome, register_tool


async def _ask_user(state, args, conn_id, include_data=False):
    q = (args or {}).get("question", "").strip()
    options = [str(o).strip() for o in (args or {}).get("options") or [] if str(o).strip()]
    if not q:
        return ToolOutcome(result={"ok": False, "error": "question 不能为空"})
    return ToolOutcome(
        result={"ok": True, "question": q, "options": options[:6],
                "note": "问题已提交用户。结束回合等用户回答即可，不要自问自答。"},
        think="向用户澄清",
    )


# 受控流允许的动作（steps[].action 封闭集；未来新域在此扩展）
PLAN_ACTIONS = ("write", "report", "knowledge", "schedule")


async def _propose_plan(state, args, conn_id, include_data=False):
    from app.ai.tools.registry import get_active_session

    ptype = (args or {}).get("type", "").strip() or "write_pipeline"
    title = (args or {}).get("title", "").strip()
    evidence = (args or {}).get("evidence_summary", "").strip()
    steps_raw = (args or {}).get("steps") or []
    sid = get_active_session()

    # 校验：证据强制（防"没查就提计划"）、步骤非空、动作封闭集
    if len(evidence) < 30:
        return ToolOutcome(result={
            "ok": False,
            "error": "evidence_summary 过短（<30 字）：必须先在循环里查证（run_query/get_schema），"
                     "把已收集的事实摘要填入 evidence_summary 再提计划。不要提空洞计划。",
        })
    if not title:
        return ToolOutcome(result={"ok": False, "error": "title 不能为空"})
    steps: list[dict] = []
    for s in steps_raw[:6]:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action", "")).strip()
        desc = str(s.get("description", "")).strip()
        if action not in PLAN_ACTIONS or not desc:
            continue
        item = {"action": action, "description": desc[:200]}
        if s.get("sql"):
            item["sql"] = str(s.get("sql"))[:2000]
        steps.append(item)
    if not steps:
        return ToolOutcome(result={
            "ok": False,
            "error": f"steps 无效：每步需 action（{'/'.join(PLAN_ACTIONS)}）+ description",
        })
    if not sid:
        return ToolOutcome(result={"ok": False, "error": "缺少会话上下文（session）"})

    plan_id = f"plan_{uuid.uuid4().hex[:12]}"
    payload = state.chats.create_pending_plan(
        plan_id, sid, ptype, title[:80], steps, evidence[:1500])
    return ToolOutcome(result={
        "ok": True, "plan_id": plan_id, "type": ptype, "title": title[:80],
        "steps": steps, "expires_in": payload["expires_in"],
        "note": "计划已提交用户确认。结束回合等待；用户确认后平台会执行计划。",
    }, think=f"提议计划：{title[:40]}（{len(steps)} 步）")


def register():
    register_tool(
        "ask_user",
        "向用户提出结构化澄清问题并结束当前回合等回答。当需求有歧义/缺关键信息时使用，"
        "不要自问自答、不要基于猜测继续。",
        {
            "question": {"type": "string", "description": "要澄清的问题（一句话）"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "候选答案（2-4 个，可选）"},
        },
        ["question"],
        _ask_user,
        trust="readonly",
    )
    register_tool(
        "propose_plan",
        "提议一份需要用户确认的执行计划（写数据/知识库维护等多步骤有副作用操作）。"
        "铁律：必须先在循环里查证收集证据，evidence_summary 填已确认的事实；"
        "计划提交后结束回合等用户确认，确认前绝不能假设已获批准。",
        {
            "type": {"type": "string",
                     "description": "计划类型：write_pipeline（写数据管道）/ knowledge_edit（知识库维护）"},
            "title": {"type": "string", "description": "计划标题（一句话）"},
            "steps": {"type": "array", "items": {"type": "object",
                      "properties": {"action": {"type": "string",
                                                "description": "write | report | knowledge | schedule"},
                                     "description": {"type": "string", "description": "这一步做什么"},
                                     "sql": {"type": "string", "description": "（写操作）要执行的 SQL"}}},
                      "description": "执行步骤（1-6 步，按顺序）"},
            "evidence_summary": {"type": "string",
                                 "description": "已查证的事实摘要（≥30 字，含关键数字/表名），证明计划基于真实数据"},
        },
        ["type", "title", "steps", "evidence_summary"],
        _propose_plan,
        trust="readonly",
    )
