# T4 · 多来源建边管线（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T3
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

实现 `graph/builder.py` 的 4 个确定性建边函数（FK / 命名推断 / 值重叠 / 判别器感知）+ 1 个日志挖掘占位。**建边是纯逻辑、无 LLM、模型无关**（对齐设计 §6 + §7 Phase A）。

## 2. 现状（读码后核实，可直接迁移的代码）

`backend/app/knowledge/annotator.py` 中已有可复用的确定性逻辑：

| 现有函数/常量 | 位置 | 用途 |
|---|---|---|
| `_generate_candidate_pairs(schema)` | annotator.py:1369 | 命名启发式：`user_id` → `users.id`（含单复数变体 + 类型族校验）——**目前只产候选喂 LLM**，本任务去 LLM 化 |
| `_table_name_variants(base)` | annotator.py:1343 | 表名单复数变体 |
| `_type_family(t)` | annotator.py:1359 | 类型族归类（不同族不配对） |
| `_REF_SUFFIXES` | annotator.py（模块常量） | `_id/_code/_no/_num/_key/_ref` 后缀 |
| `generic_names` | annotator.py:1385 | 通用名排除（id/uuid/status/created_at...） |

## 3. 接口定义（实现必须对齐）

```python
# backend/app/knowledge/graph/builder.py
"""多来源建边：纯函数，无 LLM、无 I/O（samples 由调用方传入）。"""

def build_fk_edges(schema: dict) -> list[GraphEdge]:
    """声明 FK → relation=fk, confidence=1.0, provenance=declared_fk。
    - 复合 FK（多个 ref 列对）→ cols 多对列
    - 现有 store.py::_build_graph 逻辑迁移 + 复合键支持
    """

def build_naming_edges(schema: dict) -> list[GraphEdge]:
    """命名推断 → relation=naming, confidence=0.6, provenance=naming_inference。
    - 迁移 annotator._generate_candidate_pairs，去 LLM 化：直接产出边（不再依赖 LLM 裁决）
    - 保留类型族校验、单复数变体、generic_names 排除、自环跳过逻辑
    """

def build_value_overlap_edges(schema: dict, samples: dict[str, dict[str, list]]) -> list[GraphEdge]:
    """值重叠 → relation=value_overlap, confidence=0.4~0.8（按包含率分档）。
    - 候选列对：同名列对 ∪ 命名推断产出的列对
    - 包含率 = |子表列值 ∩ 父表列值| / |子表列值|（非 Jaccard）
    - 分档：≥0.8 → 0.8；0.5~0.8 → 0.5；<0.5 → 不产边
    - 排除：高基数主键列、超低基数字段（distinct 值数 ≤ 3）、类型不同族
    """

def build_polymorphic_edges(schema: dict, samples: dict[str, dict[str, list]]) -> list[GraphEdge]:
    """判别器感知（多态关联）→ relation=value_overlap, guard="X.type = N", confidence=0.5。
    - 触发：表同时含 低基数字段（type/_type/kind）+ 引用字段（code/_id）
    - 按判别器分组采样 code 值，各组分别与候选目标表算包含率
    - 某组包含率 ≥ 0.8 → 产守卫边 guard=f"{table}.{discriminator} = {group_value}"
    """

def build_query_log_edges(audit_rows: list[dict]) -> list[GraphEdge]:
    """查询日志挖掘 → **委托 T10 的 behavior.log_mining.mine_join_edges(audit_rows)**。
    本任务不实现，仅保留此入口（建边管线 5 来源的统一出口），返回 mine_join_edges 结果。
    若 T10 尚未完成，返回 []（调用方静默降级）。"""
```

## 4. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | `samples` 为空 | 值重叠/判别器感知返回 `[]`（不炸，静默降级） |
| 2 | 列值含 NULL | 包含率计算前剔除 NULL |
| 3 | 类型不同族 | 命名/值重叠均不配对（`_type_family` 返回 None 跳过） |
| 4 | 自环（命名推断 base==表名） | 跳过（沿用现有逻辑） |
| 5 | 重复产边（FK 与命名同时命中同一列对） | 保留 FK（高置信度），命名边跳过（同列对去重：置信度高者胜） |
| 6 | 判别器字段值过多（distinct > 20） | 不触发判别器探测（可能不是判别器） |
| 7 | 采样不足（某组无样本） | 该组不产边 |
| 8 | 墓碑边（用户删过/拒过） | builder 不感知；**调用方（构建编排）在建边后统一过滤墓碑**（沿用 `_is_tombstoned` 逻辑，防复活） |
| 9 | 去重（FK 与命名/值重叠命中同一列对） | 保留 confidence 高者；同列对不重复产边 |

## 5. 日志要求（必须加）

`builder.py` 每个函数用 `logger = logging.getLogger("kb.graph_builder")`：

```python
logger.info("[graph_builder] conn=%(conn)s %(source)s 产边 %(n)d 条", ...)
# 每次调用：来源 + 产边数
logger.info("[graph_builder] fk=%d naming=%d overlap=%d polymorphic=%d", ...)
# 构建汇总：各来源边数（对齐现有 kb.build 汇总日志风格）
logger.debug("[graph_builder] %(source)s 跳过 %(pair)s：%(reason)s", ...)
# 每次跳过记录原因（类型不同族/包含率不足/去重/自环）
```

## 6. 测试设计（tests/knowledge/test_graph_builder.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_fk_edges_basic` | 含 1 条 FK 的 schema | 产 1 条边，confidence=1.0，provenance=declared_fk |
| `test_fk_composite` | 复合 FK（2 列对） | cols 长度 2 |
| `test_naming_user_id` | `orders.user_id`(int) + `users.id`(int) | 产边 confidence=0.6，provenance=naming_inference |
| `test_naming_type_mismatch` | `orders.user_id`(int) + `users.id`(varchar) | 不产边 |
| `test_naming_self_loop_skipped` | 表内列名 == 表名 | 跳过 |
| `test_overlap_high` | 子列值 ⊆ 父列值（包含率 1.0） | 产边 confidence=0.8 |
| `test_overlap_low` | 包含率 0.3 | 不产边 |
| `test_overlap_low_cardinality_excluded` | 性别类列（distinct ≤ 3） | 排除 |
| `test_overlap_null_ignored` | 值含 NULL | NULL 剔除后计算 |
| `test_polymorphic_detected` | X(type, code) + table1(code) + table2(code)，type=1 组全含于 table1 | 产 2 条守卫边，guard 正确 |
| `test_polymorphic_no_samples` | samples 为空 | 返回 [] |
| `test_query_log_stub` | 任意 audit_rows | 返回 []（T10 前） |

## 7. 执行步骤

- [x] 1. 迁移 annotator 的命名推断依赖（`_table_name_variants`/`_type_family`/`_REF_SUFFIXES`/`generic_names`）到 builder.py（或共享 util 模块），annotator 改为引用
- [x] 2. 实现 `build_fk_edges`（含复合键）
- [x] 3. 实现 `build_naming_edges`（去 LLM 化）
- [x] 4. 实现 `build_value_overlap_edges`（包含率 + 分档 + 排除规则）
- [x] 5. 实现 `build_polymorphic_edges`（分组采样 + 守卫边）
- [x] 6. `build_query_log_edges` 留签名返回 []
- [x] 7. 每函数加日志（§5）
- [x] 8. 测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 8. 验收标准（我 review 时逐条检查）

1. 5 个函数签名与 §3 一致，返回 `list[GraphEdge]`
2. **annotator 不再靠 LLM 裁决命名边**——`_generate_candidate_pairs` 的候选生成逻辑迁出，annotator 相关代码删除或改为引用 builder
3. 12 个测试用例真实断言（我抽查空断言）
4. 日志：构建时能看到各来源边数汇总（我检查 kb 构建日志输出）
5. 全量 pytest 通过

## 9. 完成定义

- [x] 5 个建边函数实现（query_log 为占位）
- [x] 命名推断从 annotator 迁出、去 LLM 化
- [x] 日志就位（来源汇总 + 跳过原因）
- [x] 12 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §6（建图管线）/ §3.4（守卫边）/ §7 Phase A
- 现状代码：`annotator.py:1343-1437`（`_table_name_variants`/`_type_family`/`_generate_candidate_pairs`）、`store.py::_build_graph`


## 偏差记录（2026-08-30 修复轮 R5）

1. **五来源已接入构建**：`GraphStore.build_graph`（facade.build/incremental_build 均走此入口）依次产 fk/naming/overlap/polymorphic/query_log 边，跨源去重按 §4#9「confidence 严格高者胜、并列时来源优先级 fk>naming>overlap>polymorphic>query_log」，墓碑统一过滤，来源汇总日志 `[graph_builder] conn=… 汇总 N 条：fk=… naming=… value_overlap=… query_log=…`。
2. **annotator 命名推断改引用 builder**：`_REF_SUFFIXES/_table_name_variants/_type_family` re-export 自 graph.builder（单一实现），`_generate_candidate_pairs` 委托 `build_naming_edges`。
3. **偏差：polymorphic 依赖 `samples[table]["_grouped"]` 分组采样**，当前 sample_values 不产分组 → 管线中 polymorphic 实际产 0 边（静默降级符合 §4#1，待 T5 分组采样补充）。
4. **偏差：路由 `expand_tables` 保持 `kinds={"fk"}`**——推断边（naming/overlap）不参与候选表路由（防误扩散），但在 graph()/path_strings/validate_join 中生效。
