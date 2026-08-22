"""内置技能注册：query（主场景，只读）、write/ddl（写与结构变更，独立承接）与 report（数据分析报告，物理只读）。

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


def _refusal_steps() -> ScriptSpec:
    return ScriptSpec(steps=[ScriptStep(id="refuse", label="引导拒答", tool=None, output="refusal")], requires_schema=False)


def _schema_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="schema", label="结构问答", tool="get_schema", output="schema"),
            ScriptStep(id="describe", label="表详情", tool="describe_table", output="columns"),
        ],
        requires_schema=True,
    )


def _write_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="assess", label="写前评估", tool="run_dml", output="preview"),
            ScriptStep(id="confirm", label="人工确认", tool=None, output="confirmed"),
        ],
        requires_schema=True,
    )


def _ddl_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="draft", label="草案生成", tool="draft_ddl", output="ddl"),
            ScriptStep(id="review", label="人工复核", tool=None, output="review"),
        ],
        requires_schema=True,
    )


def BUILTIN_SKILLS() -> list[Skill]:
    return [
        Skill(
            id="query",
            name="执行 SQL",
            description="查数据（只读查询，覆盖主场景的自然语言转 SQL）。写操作由 write 技能承接（需人工确认）。",
            tools=["get_schema", "describe_table", "run_query", "load_result"],  # 08 §4.4：只读 + WS3 工件引用
            script=_query_steps(),
            builtin=True,
            read_only=True,  # 常开地板技能，但只读：写/DDL 由独立技能承接
        ),
        Skill(
            id="report",
            name="数据分析报告",
            description="产出一份章节化分析报告（澄清口径→规划→逐章查询→图表→结论），物理只读。",
            tools=["run_query", "get_schema", "describe_table", "load_result"],
            script=_report_steps(),
            builtin=True,
            read_only=True,
        ),
        Skill(
            id="refusal",
            name="引导式拒答",
            description="平台外话题拒答并引导回平台能力（无工具，云端引导式；strict 本地固定文案）。",
            tools=[],
            script=_refusal_steps(),
            system_prompt=(
                "你是 TableTalk 的数据库助手引导员。用户的问题与数据库/本平台无关。\n"
                "你的唯一任务：①一句话礼貌说明你不处理此类问题；②给出 2~3 个基于当前数据库\n"
                "（{connection_name}，领域：{tags}）的具体建议问题。\n"
                "禁止：回答问题本身；延伸话题；编造数据库里不存在的内容。"
            ),
            builtin=True,
            read_only=True,
            enabled=True,
        ),
        Skill(
            id="schema",
            name="结构问答",
            description="直接回答库表结构（跳过检索管线，不执行 SQL）。",
            tools=["get_schema", "describe_table"],
            script=_schema_steps(),
            builtin=True,
            read_only=True,
        ),
        Skill(
            id="write",
            name="写操作",
            description="写操作（REVIEW 态势前置，需确认）。",
            tools=["run_dml", "run_query", "describe_table", "get_schema"],
            script=_write_steps(),
            system_prompt="这是写操作，将先预览后人工确认。请说明影响并等待确认。",
            builtin=True,
            read_only=False,
        ),
        Skill(
            id="ddl",
            name="DDL 草稿",
            description="DDL 草案（永不执行，发编辑器人工执行）。",
            tools=["draft_ddl", "get_schema", "describe_table"],
            script=_ddl_steps(),
            system_prompt="这是结构变更草案边界：仅生成脚本，不执行。",
            builtin=True,
            read_only=False,
        ),
    ]
