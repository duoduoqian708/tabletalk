"""技能注册表：可注册/列表/按 id 获取；内置 query+report 技能存在；只读标记正确。"""

from app.ai.skills.registry import get_skill, list_skills


def test_registry_builtin():
    ids = [s.id for s in list_skills()]
    assert "query" in ids and "report" in ids


def test_query_skill_readwrite():
    s = get_skill("query")
    assert s.read_only is False          # query 含写(需确认)
    assert "run_query" in s.tools and "run_dml" in s.tools


def test_report_skill_readonly():
    s = get_skill("report")
    assert s.read_only is True           # report 物理只读
    assert "run_dml" not in s.tools
