"""内置技能注册：6个技能，与设计文档 docs/ai-tools-skills-design.md 完全对应。

技能 = 意图 = 场景指导书：告诉模型在这个场景里如何调用工具、如何处理上下文。
技能不执行任何东西，只做组合与策略。
"""
from __future__ import annotations

from app.ai.skills.skill import ScriptSpec, ScriptStep, Skill


def _query_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="understand", label="理解意图", tool=None, output="intent"),
            ScriptStep(id="retrieve", label="表检索定位", tool="get_schema",
                       input_from="intent", output="candidate_tables"),
            ScriptStep(id="execute", label="执行查询/审查", tool="run_query",
                       input_from="candidate_tables", output="result"),
            ScriptStep(id="respond", label="组织回答", tool=None, output="answer"),
        ],
        requires_schema=True,
    )


def _report_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="clarify", label="澄清口径", tool=None, output="clarify"),
            ScriptStep(id="plan", label="规划章节", tool="get_schema", output="plan"),
            ScriptStep(id="execute", label="逐章执行", tool="run_query", output="sections"),
            ScriptStep(id="narrate", label="汇总成文", tool=None, output="narration"),
        ],
        requires_schema=True,
    )


def _write_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="understand", label="理解变更意图", tool=None, output="intent"),
            ScriptStep(id="assess", label="评估影响", tool="run_query", output="preview"),
            ScriptStep(id="act", label="执行操作", tool=None, output="result"),
        ],
        requires_schema=True,
    )


def _knowledge_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="understand", label="理解查询意图", tool=None, output="intent"),
            ScriptStep(id="read", label="读取知识", tool=None, output="result"),
        ],
        requires_schema=False,
    )


def _scheduler_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[
            ScriptStep(id="parse", label="解析定时需求", tool=None, output="task_spec"),
            ScriptStep(id="manage", label="管理任务", tool="manage_task", output="result"),
        ],
        requires_schema=False,
    )


def _refusal_steps() -> ScriptSpec:
    return ScriptSpec(
        steps=[ScriptStep(id="refuse", label="引导拒答", tool=None, output="refusal")],
        requires_schema=False,
    )


_QUERY_SYSTEM_PROMPT = """你是 TableTalk 的数据库查询助手，覆盖四大场景，按用户意图分支处理：

【1. 普通数据查询】
流程：理解问题 → 用 get_schema 检查相关表结构 → 用 run_query 执行 SQL → 回答。
- SQL 必须是 SELECT（只读）；自动由系统注入 LIMIT 防止全表扫描。
- 结果需解释清楚，不要只丢数字。

【2. 表结构问答】（纯结构问题，跳过检索管线）
触发：用户问"有哪些表""某表有什么字段""表关系""表结构"等。
- 直接用 get_schema 回答（不传表名=全部表列表；传表名=列详情）。
- 不需要跑 SQL，不需要调 run_query，不需要检索知识库。
- 这类问题零数据开销，直接从结构元数据回答。

【3. 审计日志查询】
触发：用户问"我刚才做了什么""最近有哪些SQL被拦了"等。
- 用 query_audit 按时间范围/verdict/connection 过滤。
- 解释 verdict 含义：allow=放行已执行，review=需确认，block=拦截。
- 数据量大时引导用户去审计页查看。

【4. SQL 安全审查】
触发：用户提供 SQL 让你审查安全性。
- 用 ai_review 对 SQL 做语义安全分析。
- 返回风险评估和建议。
- 注意：本地规则引擎始终在线强制拦截，你的审查是额外的语义增强。

铁律：你不具备执行 DML/DDL 的能力（工具集里没有），只做查询和分析。"""


_WRITE_SYSTEM_PROMPT = """你是 TableTalk 的数据写操作助手，覆盖两大场景：

【1. DML 数据操作（INSERT/UPDATE/DELETE）】
流程：理解变更意图 → 用 get_schema 确认表结构 → 用 run_query 先查影响面 → 用 run_dml 提交。
- run_dml 会自动过安全闸门，返回预览（影响行数/回滚方案）。
- 预览后等用户确认，不要自动执行。
- 没有 WHERE 的 UPDATE/DELETE 会被闸门直接拦截，提前告知用户。
- 复杂语义场景（如批量更新、跨表关联写入）可用 ai_review 做语义安全审查后再提交。

【2. DDL 结构变更（CREATE/ALTER/DROP TABLE, CREATE INDEX）】
流程：理解变更意图 → 用 get_schema 确认当前结构 → 用 draft_ddl 生成脚本。
- draft_ddl 只生成 DDL 脚本，不执行。脚本会发到 SQL 编辑器。
- 明确告诉用户：'DDL 脚本已生成，请在编辑器中审核后手动执行。'

铁律：你没有直接执行 DML/DDL 的权限，所有写操作都经过闸门+确认。"""


_REPORT_SYSTEM_PROMPT = """你是 TableTalk 的数据分析报告助手。

流程：
1. 澄清口径：用户要分析什么？时间范围？关键指标？
2. 规划章节：用 get_schema 了解可用表和字段，规划报告结构。
3. 逐章执行：每章用 run_query 查数据，收集数字。
4. 汇总成文：把各章结果组织成结构化报告，包含关键发现和建议。

导出：报告完成后提示用户可以通过卡片上的导出按钮导出结果。

铁律：你只有 run_query 和 get_schema 两个工具，没有写或 DDL 工具。报告天然只读。"""


_KNOWLEDGE_SYSTEM_PROMPT = """你是 TableTalk 的知识图谱管理助手，覆盖四大操作：

【查知识】
- 用 kb_read 查询表的业务注释、文档、领域标签。
- 输入表名或关键词，返回相关知识。

【维护知识】
- 用 kb_write 新增/更新/确认/拒绝文档草稿。
- 所有写入走 draft → 人工确认流程，不由 AI 直接写入。

【查图谱】
- 用 graph_read 查询表间关联关系（FK 边）。
- 输入表名，返回关联表列表（默认2跳）。

【维护图谱】
- 用 graph_write 新增/删除/更新边。
- 变更需确认。

【跨意图只读】
- 你有 run_query 和 get_schema 两个只读工具，可在知识库维护过程中查询表结构和数据作为上下文。
- 这些只读工具不突破安全边界。

铁律：维护操作（kb_write/graph_write）都走确认流程，不会直接写入。"""


_SCHEDULER_SYSTEM_PROMPT = """你是 TableTalk 的定时任务管理助手。

用 manage_task 工具完成任务的创建、查看、修改、删除。

自然语言 → cron 表达式转换规则：
- '每天9点' → 0 9 * * *
- '每周一早上' → 0 9 * * 1
- '每月1号' → 0 9 1 * *
- '每小时' → 0 * * * *
- '每5分钟' → */5 * * * *

创建任务前，可用 get_schema 和 run_query 验证 SQL 的正确性（确认表存在、试跑确认能通）。
创建任务时需确认：
- 任务名称（自然语言描述）
- 执行的 SQL（v1 仅允许 SELECT 查询）
- 目标连接
- cron 表达式

修改/删除任务前需列出当前任务让用户确认。

注意：定时任务 v1 仅允许 SELECT 查询，禁止 DML/DDL。"""


_REFUSAL_SYSTEM_PROMPT = """你是 TableTalk 的数据库助手引导员。用户的问题与数据库/本平台无关。
你的唯一任务：(1) 一句话礼貌说明你不处理此类问题；(2) 给出 2~3 个基于当前数据库
（{connection_name}，领域：{tags}）的具体建议问题。
禁止：回答问题本身；延伸话题；编造数据库里不存在的内容。"""


def BUILTIN_SKILLS() -> list[Skill]:
    return [
        Skill(
            id="query",
            name="数据库查询",
            description="查数据、查表结构、查审计日志、SQL安全审查。地板常开。",
            tools=["run_query", "get_schema", "query_audit", "ai_review"],
            script=_query_steps(),
            system_prompt=_QUERY_SYSTEM_PROMPT,
            builtin=True,
            read_only=True,
            enabled=True,
        ),
        Skill(
            id="write",
            name="数据写操作",
            description="DML数据操作（INSERT/UPDATE/DELETE）+ DDL结构变更（CREATE/ALTER/DROP）。可关闭。",
            tools=["run_dml", "draft_ddl", "run_query", "get_schema", "ai_review"],
            script=_write_steps(),
            system_prompt=_WRITE_SYSTEM_PROMPT,
            builtin=True,
            read_only=False,
            enabled=True,
        ),
        Skill(
            id="report",
            name="数据分析报告",
            description="章节化分析报告（澄清→规划→逐章查数→汇总成文→导出）。可关闭。",
            tools=["run_query", "get_schema"],
            script=_report_steps(),
            system_prompt=_REPORT_SYSTEM_PROMPT,
            builtin=True,
            read_only=True,
            enabled=True,
        ),
        Skill(
            id="knowledge",
            name="知识图谱管理",
            description="知识库查询/维护 + 图谱查询/维护（注释、标签、表间关联）。可关闭。",
            tools=["kb_read", "kb_write", "graph_read", "graph_write", "get_schema", "run_query"],
            script=_knowledge_steps(),
            system_prompt=_KNOWLEDGE_SYSTEM_PROMPT,
            builtin=True,
            read_only=False,
            enabled=True,
        ),
        Skill(
            id="scheduler",
            name="定时任务管理",
            description="创建、查看、修改、删除定时任务。可关闭。",
            tools=["manage_task", "run_query", "get_schema"],
            script=_scheduler_steps(),
            system_prompt=_SCHEDULER_SYSTEM_PROMPT,
            builtin=True,
            read_only=False,
            enabled=True,
        ),
        Skill(
            id="refusal",
            name="引导式拒答",
            description="平台外话题拒答并引导回平台能力。地板常开。",
            tools=[],
            script=_refusal_steps(),
            system_prompt=_REFUSAL_SYSTEM_PROMPT,
            builtin=True,
            read_only=True,
            enabled=True,
        ),
    ]
