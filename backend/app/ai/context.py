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
        "你是 tabletalk，一名数据库副驾。\n"
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
        "你是 tabletalk 分析副驾，负责产出一份数据库分析报告。\n"
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
    text, _meta = await assemble_context_full(state, conn_id, table, query)
    return text


async def assemble_context_full(
    state: "AppState",
    conn_id: str,
    table: str | None = None,
    query: str = "",
    tags: list[str] | None = None,
    followup_tables: list[str] | None = None,
    skip_retrieval: bool = False,
) -> tuple[str, dict]:
    """组装给模型的上下文文本 + 沿路产出的阶段元数据（意图/候选表），供前端四步展示。

    WS1：tags/followup_tables 可由 preflight 传入，避免重复 LLM；未传时回退到 classify_tags。
    WS2（T2.2）：skip_retrieval=True 时跳过表级语意路由/向量召回/图谱扩展（schema 结构问答用），
    只给整体结构摘要——零向量召回、零候选表，工具白名单自然保证零 SQL 卡。
    """
    raw_schema = filter_sensitive(await get_schema(state, conn_id), state.connections.get(conn_id).sensitive)
    # B3 敏感度路由：敏感表代号化（出网无明文，回显还原）
    schema = raw_schema
    sensitive_tables: set[str] = set()
    try:
        from app.safety.codify import codify_schema, is_sensitive_table
        # 收集所有表的敏感状态
        for t in raw_schema.get("tables", []):
            tbl = t.get("name", "")
            if is_sensitive_table(state, conn_id, tbl):
                sensitive_tables.add(tbl)
        if sensitive_tables:
            from app.config import get_env
            schema = codify_schema(get_env().data_dir, conn_id, raw_schema, sensitive_tables)
    except Exception:
        pass
    state.knowledge.ensure_loaded(conn_id)
    await state.knowledge.reembed_if_needed(conn_id)  # 嵌入模型配置变化 → 向量重嵌
    parts: list[str] = [
        f"当前连接: {schema.get('connection', conn_id)}（{schema.get('dialect', '')}）"
    ]
    if table:
        parts.append(f"当前表: {table}")
    parts.append("【表结构】")
    # 双通道融合路由：标签路由（精确）× 向量召回（语义泛化）→ FK 扩展保证连通
    routed: list[str] | None = None
    if tags is None:
        from app.ai.intent import classify_tags
        tags = await classify_tags(state, conn_id, query)
    else:
        tags = list(tags or [])
    tag_tables: set[str] = set()
    # WS2（T2.2）：schema 结构问答跳过整个检索管线（标签路由/向量召回/FK 扩展），零候选表
    routed: list[str] | None = None
    vec_tables: list[str] = []
    _capped = False
    if not skip_retrieval:
        if tags:
            tag_tables = set(state.knowledge.route_tables(conn_id, tags, hops=2).get("tables", []))
        # 向量通道：问题语义 → top-K 表（无标签/标签未命中也召回，补齐标签覆盖率短板）
        vec_hits = await state.knowledge.vector_route_tables(conn_id, query, top_k=6)
        vec_tables = [t for t, _ in vec_hits]
        seeds = tag_tables | set(vec_tables)
        # T1.3 追问轮：L2 种子并入上轮 card.tables
        if followup_tables:
            seeds |= set(followup_tables)
        if seeds:
            # 融合种子 + FK 2 跳扩展 → 候选子图（结构连通，可 JOIN）
            routed = sorted(state.knowledge.expand_tables(conn_id, seeds, hops=2))
            # T5.1 封顶 20：标签 > 向量 > 外围
            if routed and len(routed) > 20:
                _capped = True
                # 排序键：标签命中 1，向量命中 1，外围 0
                def _score(tbl: str) -> tuple[int, int, int]:
                    tag_hit = 1 if tbl in tag_tables else 0
                    vec_hit = 1 if tbl in vec_tables else 0
                    # 稳定排序：标签优先，其次向量，最后按原序（用 routed 初始顺序的 index）
                    return (-tag_hit, -vec_hit, routed.index(tbl))
                routed = sorted(routed, key=_score)[:20]
        if routed:
            parts.append(
                f"（领域路由: {' / '.join(tags) if tags else '未命中'} · 向量召回 +{len(vec_tables)} · FK 扩展 → 候选表 {len(routed)} 张{', 已封顶20' if _capped else ''}）"
            )
    if routed:
        parts.append(summarize(schema, table, routed))
    else:
        parts.append(summarize(schema, table))
    kb = await state.knowledge.to_context(conn_id, query, table)
    kb_docs = 0
    if kb:
        # B3：知识库文本中的敏感表名也代号化
        if sensitive_tables:
            try:
                from app.safety.codify import codify_table
                from app.config import get_env
                dd = get_env().data_dir
                for tbl in list(sensitive_tables):
                    code = codify_table(dd, conn_id, tbl)
                    kb = kb.replace(tbl, code)
            except Exception:
                pass
        parts.append(kb)
        kb_docs = kb.count("\n- [")
    # B3：manifest 的 candidate_tables 也需代号化（出网无明文）
    manifest_tables = routed or []
    if sensitive_tables and manifest_tables:
        try:
            from app.safety.codify import codify_table
            from app.config import get_env
            dd = get_env().data_dir
            manifest_tables = [codify_table(dd, conn_id, t) if t in sensitive_tables else t for t in manifest_tables]
        except Exception:
            pass
    meta = {"intent": tags or [], "candidate_tables": manifest_tables, "vec_tables": vec_tables, "kb_docs": kb_docs, "sensitive_tables": list(sensitive_tables), "capped": _capped}
    return "\n".join(parts), meta
