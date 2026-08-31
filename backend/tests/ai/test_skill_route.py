"""E5 skill 路由测试（设计 §15.3/§15.4）：route 穷举 + general 兜底 + match 声明。"""
from __future__ import annotations

import pytest

from app.ai.skills.builtin import BUILTIN_SKILLS
from app.ai.skills.route import GENERAL_SKILL, route
from app.ai.skills.skill import Skill


# ---------- route 穷举（§15.4 8 条规则） ----------

def test_route_query_answer():
    assert route("query", "answer") == "query"


def test_route_query_analyze():
    assert route("query", "analyze") == "query"


def test_route_query_report():
    assert route("query", "report") == "report"


def test_route_query_automate():
    assert route("query", "automate") == "scheduler"  # 设计命名 schedule


def test_route_write_any_modality():
    assert route("write", "answer") == "write"
    assert route("write", "report") == "write"
    assert route("write", None) == "write"


def test_route_ddl():
    assert route("ddl") == "ddl"  # F4：独立 ddl 技能（§15.3）


def test_ddl_skill_no_exec_tools():
    """F4：ddl 技能白名单无执行工具（墙1：只有 draft_ddl/get_schema/run_query 只读面）。"""
    ddl = {s.id: s for s in BUILTIN_SKILLS()}["ddl"]
    assert "run_dml" not in ddl.tools
    assert "draft_ddl" in ddl.tools
    assert ddl.read_only is False  # 可生成 DDL 脚本（不执行）


def test_route_kb():
    assert route("kb") == "knowledge"


def test_route_schedule():
    assert route("schedule") == "scheduler"


def test_route_unknown_general():
    assert route("unknown") == GENERAL_SKILL
    assert route("banana") == GENERAL_SKILL  # 非法 action 兜底
    assert route(None) == GENERAL_SKILL


# ---------- Skill.match 匹配 ----------




# ---------- 内置技能声明齐全（含 general 兜底） ----------

def test_builtin_skills_have_match_and_general():
    skills = {s.id: s for s in BUILTIN_SKILLS()}
    assert "general" in skills  # 第 7 个兜底技能
    assert "query" in skills and "write" in skills and "report" in skills
    assert "knowledge" in skills and "scheduler" in skills and "refusal" in skills
    # 自动路由技能都有 match 声明
    for sid in ("query", "write", "report", "knowledge", "scheduler"):
        assert skills[sid].match, f"{sid} 缺 match 声明"


def test_general_skill_read_only_tools():
    g = {s.id: s for s in BUILTIN_SKILLS()}["general"]
    assert g.read_only is True
    assert set(g.tools) <= {"get_schema", "kb_read", "graph_read", "run_query"}


def test_builtin_route_coverage():
    """每个内置可路由技能都能被 route 规则命中（无孤儿技能）。"""
    skills = {s.id: s for s in BUILTIN_SKILLS() if s.match}
    for sid, s in skills.items():
        m = s.match
        action = m.get("action", "query")
        modality = m.get("modality", "answer")  # 无 modality 声明 → 用典型 answer 试探
        assert route(action, modality) == sid, f"{sid} 无法被 route 命中"


# ---------- F2：声明式配置接线 ----------

def test_route_consistency_with_match():
    """F2：route 产出的组合必须命中对应技能的 match 声明（承接映射白名单除外）。

    承接映射（route 表职责，不需技能 match 声明）：
    - (query, automate) → scheduler：自动化请求由 schedule 技能承接（§15.4）
    - (ddl, *) → write：DDL 由 write 技能承接（draft_ddl 在其白名单）
    - (unknown, *) → general：兜底技能无 match
    """
    skills = {s.id: s for s in BUILTIN_SKILLS()}
    _delegated = {("query", "automate"), ("query", None)}
    for action in ("query", "write", "ddl", "kb", "schedule", "unknown"):
        for modality in (None, "answer", "analyze", "report", "automate"):
            sid = route(action, modality)
            if sid == "general":
                continue  # 兜底技能无 match
            s = skills[sid]
            m = s.match or {}
            if m.get("action") and m["action"] != action:
                # 仅允许承接白名单内的跨 action 映射
                assert (action, modality) in _delegated, \
                    f"{sid}.match.action={m['action']} 不覆盖 route({action},{modality})"


def test_termination_max_turns_consumed():
    """F2：termination.max_turns 可读；缺省回退全局默认。"""
    from app.ai.skills.route import termination_max_turns
    assert termination_max_turns("query") == 6
    assert termination_max_turns("report") == 8
    assert termination_max_turns("scheduler") == 4
    assert termination_max_turns("nonexistent") == 6  # 缺省回退


def test_degradation_readable():
    """F2：degradation 文案可读（技能降级提示）。"""
    from app.ai.skills.route import degradation_for
    assert "知识库" in degradation_for("report")
    assert degradation_for("nonexistent") == ""