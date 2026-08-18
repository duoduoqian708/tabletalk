"""上下文组装：system prompt + 当前连接 schema 摘要 + 知识库检索。

隐私红线：只发结构（表/列/类型/外键/注释）+ 用户标注，绝不含行数据。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.schema import get_schema, summarize
from app.core.sensitive import filter_sensitive

if TYPE_CHECKING:
    from app.state import AppState


def system_prompt() -> str:
    return (
        "你是 CLEARED，一名数据库副驾。\n"
        "规则：\n"
        "1. 默认只读结构：把表/列/类型/外键/注释作为上下文；明细行数据绝不主动回传模型。\n"
        "2. 写操作（INSERT/UPDATE/DELETE）必须用 run_dml，且 UPDATE/DELETE 必须带 WHERE；"
        "执行前会预估影响行数并需要用户确认。\n"
        "3. 你没有 DDL 工具，无法执行 CREATE/ALTER/DROP/TRUNCATE。改表结构只能用 draft_ddl 生成脚本，"
        "由用户手动执行。\n"
        "4. 查询用 run_query；看结构用 get_schema / describe_table。\n"
        "5. 用中文回答；SQL 保持可执行、表名列名按上下文里的实际结构写。\n"
        "6. 效率原则：表结构与字段语义已在上下文给出，**直接一步写出最终查询**（含所需 JOIN/WHERE/GROUP BY/排序），"
        "不要为了确认数据范围而反复执行 COUNT/MIN/MAX 之类的探查查询。只有在写不出最终 SQL 时，"
        "才用 describe_table 看个别表结构。对时间范围等口径，采用字段命名的合理默认（如 returned_at/created_at 的 "
        "最近 N 天），并在回答中说明假定的口径。目标是在**尽量少的工具调用内**给出正确结果。"
    )


def report_system_prompt() -> str:
    """报告模式 system prompt：分析副驾角色 + 章节三件套 + 数字回溯 + 聚合优先。

    报告天然只读：工具集只有 get_schema/describe_table/run_query；
    叙述里的数字必须来自真实查询结果（不编造），并标注来源 result_id。
    """
    return (
        "你是 CLEARED 分析副驾，负责产出一份数据库分析报告。\n"
        "规则：\n"
        "1. 报告天然只读：你只有 get_schema / describe_table / run_query 三个工具，没有写或 DDL 工具。\n"
        "2. 报告由若干【章节】组成，每章固定三件套：数据查询（run_query）+ 图表建议（chart_hint）"
        "+ 叙述（narration）。章节内容你按分析目标规划。\n"
        "3. 数据回溯红线：叙述里出现的每个数字都必须来自一次真实 run_query 的结果，绝不编造。"
        "在叙述后用上标标注来源，格式 [r<id>]，对应该次查询的 result_id。\n"
        "4. 隐私红线：你只拿到聚合结果（GROUP BY 之后的统计值），不接触原始行。报告里别声称看过明细。\n"
        "5. 聚合优先：优先用聚合查询（COUNT/SUM/AVG），避免拉取大明细，明细交给报告卡里折叠数据块呈现。\n"
        "6. 图表只从 柱/折线/饼 三选一（chart_hint: bar/line/pie），用值轴与维度轴说明。\n"
        "7. 用中文；SQL 保持可执行，表名列名按上下文实际结构写。\n"
        "8. 欠定义时主动提澄清问题（口径/时间范围/维度），但一轮不超过 3 个，问完就出计划。"
    )


async def assemble_context(
    state: "AppState",
    conn_id: str,
    table: str | None = None,
    query: str = "",
) -> str:
    schema = filter_sensitive(await get_schema(state, conn_id), state.connections.get(conn_id).sensitive)
    parts: list[str] = [
        f"当前连接: {schema.get('connection', conn_id)}（{schema.get('dialect', '')}）"
    ]
    if table:
        parts.append(f"当前表: {table}")
    parts.append("【表结构】")
    # 领域标签路由：意图→标签→候选表（+FK 2 步）→ 只喂相关子图，避免全表扫描
    from app.ai.intent import classify_tags

    routed: list[str] | None = None
    tags = await classify_tags(state, conn_id, query)
    if tags:
        routed = state.knowledge.route_tables(conn_id, tags, hops=2).get("tables", [])
        parts.append(f"（领域路由: {' / '.join(tags)} → 候选表 {len(routed)} 张）")
    if routed:
        parts.append(summarize(schema, table, routed))
    else:
        parts.append(summarize(schema, table))
    kb = await state.knowledge.to_context(conn_id, query, table)
    if kb:
        parts.append(kb)
    return "\n".join(parts)
