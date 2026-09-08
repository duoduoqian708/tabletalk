"""上下文组装：system prompt + 当前连接 schema 摘要 + 知识库检索。

隐私红线：只发结构（表/列/类型/外键/注释）+ 用户标注，绝不含行数据。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.schema import get_schema, summarize
from app.ai.prompts import render

if TYPE_CHECKING:
    from app.state import AppState


def system_prompt() -> str:
    return render("main_chat")


def report_system_prompt() -> str:
    """报告模式 system prompt：分析副驾角色 + 章节三件套 + 数字回溯 + 聚合优先。

    报告天然只读：工具集只有 get_schema/describe_table/run_query；
    叙述里的数字必须来自真实查询结果（不编造），并标注来源 result_id。
    """
    return render("main_report")


def _codify_table_name(conn_id: str, table: str) -> str:
    """敏感表名 → B3 代号（出网无明文，回显还原）。"""
    from app.safety.codify import codify_table
    from app.config import get_env
    return codify_table(get_env().data_dir, conn_id, table)


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
    raw_schema = await get_schema(state, conn_id)
    # B3 敏感度路由：敏感表代号化（出网无明文，回显还原；判定走 confirmed "sensitive" 标签）
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
    # （引擎合一后 tags 由调用方恒显式传入——harness/preflight 提供关键词标签，不再有 LLM 分类回退）
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
    # T9：候选 join 路径串（图作为检索器：不 dump 邻居列表，dump 路径）
    paths: list[str] = []
    if routed and not skip_retrieval:
        try:
            # B3/R15：路径串限定封顶后的候选表内（不输出候选子图外的边）
            paths = state.knowledge.path_strings(conn_id, seeds, hops=2, allowed=set(routed))
        except Exception as e:  # pragma: no cover - 路径串失败不阻塞
            logging.getLogger("ai.context").warning("[context] conn=%s 路径串生成失败：%s", conn_id, e)
        if paths:
            # B3：路径串中的敏感表名代号化（出网无明文，回显还原）。
            # P1-7：只替换含敏感表的路径，非敏感路径保留原文（此前被空内层推导丢弃）
            if sensitive_tables:
                def _codify_path(p: str) -> str:
                    for _tbl in sensitive_tables:
                        if _tbl in p:
                            p = p.replace(_tbl, _codify_table_name(conn_id, _tbl))
                    return p
                paths = [_codify_path(p) for p in paths]
            parts.append("【关联路径】")
            parts.extend(f"- {p}" for p in paths)
    # T9：表级过滤器（软删除/租户）——候选表涉及的表注入谓词说明
    filters: dict[str, list[str]] = {}
    if routed and not skip_retrieval:
        try:
            filters = state.knowledge.filter_store.get_filters(conn_id, set(routed))
        except Exception as e:  # pragma: no cover
            logging.getLogger("ai.context").warning("[context] conn=%s 过滤器获取失败：%s", conn_id, e)
        if filters:
            # B3：过滤器表名（key + 谓词中的表名）代号化
            if sensitive_tables:
                codified: dict[str, list[str]] = {}
                for t, preds in filters.items():
                    ct = _codify_table_name(conn_id, t) if t in sensitive_tables else t
                    codified[ct] = [p.replace(t, ct) for p in preds] if t in sensitive_tables else preds
                filters = codified
            parts.append("【表级过滤】以下表一般需要带过滤条件（参考知识，非强制；"
                         "如用户明确说明该表特殊情况，以用户要求为准）：")
            for t, preds in filters.items():
                parts.append(f"- {t}: {' AND '.join(preds)}")
    # T9：会话变量清单（LLM 引用名字，执行层替换真实值）
    if not skip_retrieval:
        from app.knowledge.filters import session_vars_prompt
        parts.append(session_vars_prompt(state, conn_id))
    # B2/T9：概念字典（限定候选表内：成员表 ∈ routed；confirmed 才出网；B3 代号化）。
    # S2-2：kind=constant 是全局业务常量（不随表路由限制，LLM 直接引用值）
    if routed and not skip_retrieval:
        try:
            concepts = [
                c for c in state.knowledge.concept_store.list(conn_id)
                if c.status == "confirmed"
                and (c.kind == "constant"
                     or any(m.get("table") in set(routed) for m in c.members))
            ]
        except Exception:  # pragma: no cover - 概念段失败不阻塞
            concepts = []
        if concepts:
            seg_parts = ["【概念字典】以下枚举/维度的值与含义（写 SQL 时按此映射值）："]
            for c in concepts:
                codes = "，".join(
                    f"{e.get('code', '')}={e.get('label', '')}"
                    for e in c.canonical_enum if e.get("code")
                )
                tables = ", ".join(sorted({m.get("table", "") for m in c.members}))
                if sensitive_tables:
                    tables = ", ".join(
                        _codify_table_name(conn_id, t) if t in sensitive_tables else t
                        for t in sorted({m.get("table", "") for m in c.members})
                    )
                if c.kind == "constant":
                    # S2-2：常量直接给值（非敏感明文，LLM 写 SQL 时引用）
                    seg_parts.append(f"- 常量 {c.name} = {codes or c.name}（来源: {tables or '全局'}）")
                else:
                    seg_parts.append(f"- {c.name}（{c.kind}，涉及表: {tables}）：{codes}")
            parts.append("\n".join(seg_parts))
    # T10：few-shot 召回（历史成功问答对）——B3 代号化后出网
    fewshot_hits: list[dict] = []
    if not skip_retrieval:
        try:
            for h in state.knowledge.fewshot_store.recall(conn_id, query, k=3):
                _s = h.get("sql", "")
                _jp = list(h.get("join_path") or [])
                if sensitive_tables:
                    for tbl in sensitive_tables:
                        _s = _s.replace(tbl, _codify_table_name(conn_id, tbl))
                        _jp = [p.replace(tbl, _codify_table_name(conn_id, tbl)) for p in _jp]
                fewshot_hits.append({"q": h.get("question", ""), "sql": _s, "join_path": _jp})
        except Exception:
            fewshot_hits = []
        if fewshot_hits:
            parts.append("【相似问答】历史成功问答（仅参考，表名/值已按当前敏感设置代号化）：")
            for h in fewshot_hits:
                parts.append(f"- 问：{h['q']}")
                parts.append(f"  SQL：{h['sql']}")
                if h["join_path"]:
                    parts.append(f"  路径：{'; '.join(h['join_path'])}")
    # T9：语义检索限定在图选定的候选表内（双写 §4.1，避免无关表召回）；
    # R13：skip_retrieval 真正短路（结构问答零向量召回，WS2）
    kb = ""
    if not skip_retrieval:
        kb = await state.knowledge.to_context(
            conn_id, query, table,
            table_whitelist=set(routed) if routed else None,
        )
    kb_docs = 0
    if kb:
        # B3：知识库文本中的敏感表名也代号化
        if sensitive_tables:
            for tbl in sensitive_tables:
                kb = kb.replace(tbl, _codify_table_name(conn_id, tbl))
        parts.append(kb)
        kb_docs = kb.count("\n- [")
    # B3：manifest 的 candidate_tables 也需代号化（出网无明文）
    manifest_tables = routed or []
    if sensitive_tables and manifest_tables:
        manifest_tables = [
            _codify_table_name(conn_id, t) if t in sensitive_tables else t
            for t in manifest_tables
        ]
    # T9 §6：检索链路摘要日志（每个环节的量，对齐前端四步展示）
    logging.getLogger("ai.context").info(
        "[context] conn=%s 种子=%d(标签%d/向量%d) 候选=%d 路径=%d 语义命中=%d fewshot=%d",
        conn_id, len(tag_tables | set(vec_tables) | set(followup_tables or [])),
        len(tag_tables), len(vec_tables), len(routed or []), len(paths),
        kb_docs, len(fewshot_hits))
    meta = {"intent": tags or [], "candidate_tables": manifest_tables, "vec_tables": vec_tables, "kb_docs": kb_docs, "sensitive_tables": list(sensitive_tables), "capped": _capped, "paths": paths, "filters": filters, "fewshot": len(fewshot_hits)}
    return "\n".join(parts), meta
