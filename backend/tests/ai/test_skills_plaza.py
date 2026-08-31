"""P6 技能广场：注册表持久化/组合校验/工具集过滤 + 触发词路由 + API CRUD。"""

from __future__ import annotations

import pytest

from app.ai.skills.registry import (
    _tool_names,
    get_skill,
    list_skills,
    register_custom,
    remove_skill,
    skill_tool_schemas,
    update_skill,
    validate_skill,
)
from app.ai.skills.skill import Skill


def _custom() -> Skill:
    return Skill(
        id="", name="对账", description="核对订单与回款",
        tools=["run_query", "get_schema"], read_only=True, triggers=["对账", "回款"],
    )


def test_custom_skill_persists_across_instances(tmp_path):
    from app.ai.skills import registry as reg
    reg.load_custom(tmp_path)
    s = register_custom(_custom())
    sid = s.id
    assert sid.startswith("sk_")
    assert get_skill(sid).triggers == ["对账", "回款"]

    # 模拟重启：新注册表实例加载同一目录
    reg2 = reg
    reg2._custom.clear()
    reg2._custom_path = None
    reg2.load_custom(tmp_path)
    loaded = reg2.get_skill(sid)
    assert loaded is not None and loaded.name == "对账"
    assert loaded.read_only is True
    assert loaded.enabled is True


def test_validate_skill_rules():
    assert validate_skill("", "d", ["run_query"], [], False)
    assert validate_skill("x", "d", [], [], False)
    assert validate_skill("x", "d", ["not_a_tool"], [], False)
    # 只读技能挂写工具 → 拒绝
    assert validate_skill("x", "d", ["run_dml"], [], True)
    # 合法组合通过
    assert not validate_skill("对账", "d", ["run_query", "get_schema"], ["对账"], True)


def test_update_skill_toggle_and_tools():
    from app.ai.skills import registry as reg
    reg.load_custom(tmp_path := __import__("tempfile").mkdtemp())
    s = register_custom(_custom())
    updated = update_skill(s.id, {"enabled": False, "tools": ["run_query"]})
    assert updated.enabled is False
    assert updated.tools == ["run_query"]
    assert get_skill(s.id).enabled is False
    # 内置技能可更新 enabled/tools，不可删除；地板技能 query 不可禁用（T1.2/08§4.5）
    up = update_skill("query", {"enabled": False})
    assert up is not None and up.enabled is True  # 地板保持启用
    up2 = update_skill("report", {"enabled": False})
    assert up2 is not None and up2.enabled is False
    # 恢复 report，避免影响后续测试
    update_skill("report", {"enabled": True})
    assert remove_skill("query") is False
    assert remove_skill(s.id) is True
    assert get_skill(s.id) is None


def test_skill_tool_schemas_filtering():
    all_names = {t["function"]["name"] for t in skill_tool_schemas(None)}
    assert "run_dml" in all_names
    ro = skill_tool_schemas("report")
    assert {t["function"]["name"] for t in ro} == {"get_schema", "run_query", "kb_read", "graph_read"}
    # 未知技能回退全量
    assert len(skill_tool_schemas("nope")) == len(all_names)


def test_route_effective_disabled_skill_falls_back():
    """P1-1：技能禁用后 route_effective 落到 general 兜底（地板 query 不受影响）。"""
    from app.ai.skills.route import route_effective
    prev = get_skill("write").enabled
    update_skill("write", {"enabled": False})
    try:
        assert route_effective("write") == "general"
        assert route_effective("query", "answer") == "query"  # 地板不受影响
        assert route_effective("write", "answer") == "general"
    finally:
        update_skill("write", {"enabled": prev})


def test_skill_tool_schemas_disabled_returns_empty():
    """P1-1：禁用技能的工具契约收窄为零（禁止靠缺席，模型拿不到任何动词）。"""
    prev = get_skill("write").enabled
    update_skill("write", {"enabled": False})
    try:
        assert skill_tool_schemas("write") == []
    finally:
        update_skill("write", {"enabled": prev})
