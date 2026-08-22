"""技能注册表：可注册/列表/按 id 获取；内置 query+report 技能存在；只读标记正确。"""

from app.ai.skills.registry import get_skill, list_skills


def test_registry_builtin():
    ids = [s.id for s in list_skills()]
    assert "query" in ids and "report" in ids


def test_query_skill_readonly():
    s = get_skill("query")
    assert s.read_only is True          # 08 §4.4：query 只读（写由 write 技能承接）
    assert "run_query" in s.tools and "get_schema" in s.tools and "describe_table" in s.tools
    assert "run_dml" not in s.tools and "draft_ddl" not in s.tools


def test_report_skill_readonly():
    s = get_skill("report")
    assert s.read_only is True           # report 物理只读
    assert "run_dml" not in s.tools
