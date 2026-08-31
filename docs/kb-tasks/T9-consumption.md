# T9 · 消费流程整合（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T3 + T6 + T7 + T8
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

重构 `app/ai/context.py::assemble_context_full` 的检索链路，对齐设计 §4.1 + §10：

```
现状：标签路由 ∪ 向量召回 → FK 扩展 → summarize(表集合) + 全局 KB 检索
目标：种子表 → 图扩展 path_strings（缩范围）→ 语义检索【限定候选表内】→
      组装：schema 摘要 + 路径串 + 概念 + 会话变量清单 + 表级过滤器
```

关键修正：**语义检索必须限定在图选定的表集合内**（现状全局检索，可能从无关表捞出 amount 字段）。

## 2. 现状（读码后核实）

`app/ai/context.py::assemble_context_full`（56-175 行）现状流程：

```
1. get_schema → filter_sensitive → codify（B3 代号化）
2. classify_tags（preflight 传入或内部 LLM）→ route_tables（标签路由）
3. vector_route_tables（向量召回 top6）
4. seeds = tag_tables ∪ vec_tables（+ followup_tables）
5. expand_tables(seeds, hops=2) → 候选表（封顶 20）
6. summarize(schema, routed) → 表结构摘要
7. state.knowledge.to_context(conn_id, query) → KB 检索（**全局**，问题点）
8. 组装 meta（intent/candidate_tables/...）
```

问题：
- 步骤 7 的 KB 检索**不受候选表限制**（可能召回无关表字段）
- 步骤 5 只给表集合，不给 join 路径串（T6 未接入）
- 无会话变量清单、无表级过滤器

## 3. 目标流程（实现必须对齐）

```
1. get_schema → filter_sensitive → codify（保留）
2. classify_tags → route_tables（保留，作为种子来源之一）
3. vector_route_tables（保留，作为种子来源之二）
4. seeds = tag_tables ∪ vec_tables ∪ followup_tables
5. 图扩展：path_strings(edges, seeds, hops=2) → 候选表 + 路径串（T6 接入，替代 expand_tables 单用）
   候选表封顶逻辑保留（20 张）
6. 语义检索【限定候选表内】：检索表卡/概念时过滤 table ∈ 候选表
   - KB 检索：to_context 增加候选表过滤参数（或检索后过滤）
   - 概念检索：get_for_column 限定候选表（T7 接入）
7. 组装 prompt：
   - 【表结构】summarize(schema, routed)（保留）
   - 【关联路径】path_strings 输出（新增）
   - 【语义】限定范围内的 KB 内容 + 概念（新增）
   - 【会话变量】session_vars_prompt()（T8 接入）
   - 【表级过滤器】涉及表的 filter 谓词（T8 接入）
8. 组装 meta（保留 + 增加 paths/filters 字段）
```

## 4. 接口调整

```python
# context.py 内部新增/调整
async def assemble_context_full(state, conn_id, table=None, query="",
                                tags=None, followup_tables=None,
                                skip_retrieval=False) -> tuple[str, dict]:
    """返回 (context_text, meta)
    meta 增加：paths(list[str])、filters(dict[table, list[str]])"""

# KnowledgeBase 需要的新接口（由 T6/T7/T8 提供，门面委托）：
#   state.knowledge.path_strings(conn_id, seeds, hops=2) -> list[str]      # T6
#   state.knowledge.get_filters(conn_id, tables) -> dict[str, list[str]]   # T8
#   session_vars_prompt()                                                  # T8
```

## 5. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | `skip_retrieval=True`（结构问答） | 跳过整个检索管线（保留 WS2 行为）——路径串/语义/变量都不加 |
| 2 | 图无路径（无 join 边） | 路径串为空，不阻塞（单表查询） |
| 3 | 语义检索结果为空 | 降级为纯 schema 摘要（现状行为） |
| 4 | 候选表封顶（>20） | 保留现有排序（标签 > 向量 > 外围），路径串只对入选表输出 |
| 5 | 敏感表代号化 | 路径串/过滤器中的表名同样过 codify（保持 B3 不变式） |
| 6 | 会话变量清单注入 | 只注入名称/含义/类型，**不注入真实值** |
| 7 | 过滤器注入失败 | 告警 + 不注入（宁缺勿错，T8 §4 降级规则） |

## 6. 日志要求（必须加）

```python
logger = logging.getLogger("ai.context")

logger.info("[context] conn=%(conn)s 种子=%d(标签%d/向量%d) 候选=%d 路径=%d 语义命中=%d",
            ..., len(seeds), len(tag_tables), len(vec_tables), len(routed),
            len(paths), kb_docs)
# 检索链路摘要：每个环节的量（对齐现有四步展示）
logger.debug("[context] 路径: %s", paths)
logger.debug("[context] 过滤器: %s", filters)
logger.warning("[context] conn=%(conn)s 候选表外召回被过滤：%(dropped)s 条", ...)
# 语义检索被限定过滤掉的数量（验证修正生效）
```

## 7. 测试设计（tests/ai/test_context_integration.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_retrieval_scoped_to_candidates` | 无关表含同名列（amount），不在候选表内 | 语义检索**不**召回该列（修正生效） |
| `test_path_strings_in_prompt` | demo 库有 FK | context 文本含 `"orders.user_id = users.id"` 形式路径 |
| `test_session_vars_in_prompt` | 任意 | context 含 `:current_tenant` 说明 |
| `test_filters_in_prompt` | 候选表含 orders（有 is_deleted filter） | context 含过滤谓词 |
| `test_skip_retrieval_no_paths` | skip_retrieval=True | 无路径/语义/变量（保留 WS2） |
| `test_sensitive_table_codified` | 敏感表在候选内 | 路径串/过滤器中表名为代号 |
| `test_meta_fields` | 任意 | meta 含 paths/filters 字段 |

## 8. 执行步骤

- [x] 1. `context.py`：检索顺序重构（图扩展 → 限定范围语义）
- [x] 2. 接入 `path_strings`（T6）+ 路径串进 prompt
- [x] 3. 接入概念检索（T7，限定候选表）
- [x] 4. 接入会话变量清单 + 表级过滤器（T8）
- [x] 5. 敏感表代号化覆盖路径串/过滤器
- [x] 6. 日志就位
- [x] 7. 测试 + **全量回归（重点 AI 相关测试）**

```
cd backend && .venv/bin/python -m pytest -q
```

## 9. 验收标准（我 review 时逐条检查）

1. 检索顺序正确：图先缩范围、语义后填（`test_retrieval_scoped_to_candidates` 真实断言）
2. prompt 含路径串 + 会话变量清单 + 过滤器谓词
3. `skip_retrieval` 行为不回归（WS2）
4. 敏感表代号化覆盖新增内容（B3 不变式）
5. meta 增加 paths/filters 字段（前端四步展示用）
6. 全量回归通过（现有 AI 相关测试不能红）

## 10. 完成定义

- [x] 检索顺序重构 + 限定范围
- [x] 路径串/概念/变量/过滤器接入
- [x] 日志就位
- [x] 7 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §4.1（检索顺序）/ §10（消费流程）
- 现状代码：`ai/context.py::assemble_context_full`
- 依赖接口：T6 `path_strings`、T7 概念查询、T8 `SESSION_VARS`/`get_filters`
- 风险：改 `context.py` 影响所有 AI 查询——必须全量回归（243 pytest）


## 偏差记录（2026-08-30 修复轮 R2/R13）

1. **敏感表代号化覆盖已补全**：路径串段、表级过滤段（key + 谓词）、few-shot 问答段（R7）全部代号化出网，B3 不变式恢复；`test_sensitive_table_codified`/`test_fewshot_sensitive_codified` 覆盖。
2. **skip_retrieval 真正短路**：to_context（KB 向量召回）已纳入守卫——结构问答零向量召回（埋点测试断言零调用）。
3. **链路摘要日志已落**（§6 格式）：`[context] conn=… 种子=N(标签N/向量N) 候选=N 路径=N 语义命中=N fewshot=N`。
