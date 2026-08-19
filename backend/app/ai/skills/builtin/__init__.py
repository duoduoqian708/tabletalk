"""内置技能注册：query（主场景，读写合并）与 report（数据分析报告，物理只读）。

这些是 TABLETALK 的"出厂技能"。query 的剧本即四步（意图→检索→SQL→评估），
report 的剧本是章节化分析。新增内置技能在此追加即可。
"""
from __future__ import annotations

from app.ai.skills.skill import ScriptSpec, ScriptStep, Skill


def _query_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="intent", label="意图分解", tool=None, output="intent"),
            ScriptStep(id="retrieval", label="表检索定位", tool="get_schema",
                       input_from="intent", output="candidate_tables"),
            ScriptStep(id="sql_gen", label="SQL 生成", tool="run_query",
                       input_from="candidate_tables", output="sql"),
            ScriptStep(id="gate", label="安全评估与执行", tool="run_query",
                       input_from="sql", output="verdict"),
        ],
        requires_schema=True,
    )


def _report_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="clarify", label="澄清口径", tool=None, output="clarify"),
            ScriptStep(id="plan", label="规划章节", tool="get_schema", output="plan"),
            ScriptStep(id="execute", label="逐章执行", tool="run_query", output="sections"),
            ScriptStep(id="narrate", label="汇总成文", tool="run_query", output="narration"),
        ],
        requires_schema=True,
    )


def BUILTIN_SKILLS() -> list[Skill]:
    return [
        Skill(
            id="query",
            name="执行 SQL",
            description="查数据或改数据（读写合并，写需人工确认）。覆盖主场景的自然语言转 SQL。",
            tools=["run_query", "run_dml", "get_schema", "describe_table", "draft_ddl"],
            script=_query_steps(),
            builtin=True,
            read_only=False,
        ),
        Skill(
            id="report",
            name="数据分析报告",
            description="产出一份章节化分析报告（澄清口径→规划→逐章查询→图表→结论），物理只读。",
            tools=["run_query", "get_schema", "describe_table"],
            script=_report_steps(),
            builtin=True,
            read_only=True,
        ),
    ]
