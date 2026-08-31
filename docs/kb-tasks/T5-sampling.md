# T5 · 采样分列策略（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T2（独立于 T3/T4，可并行）
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

修正采样偏差：当前 `sample_values` 用 `ORDER BY pk DESC LIMIT n` 只采**最近的行**，导致枚举列漏掉历史/罕见值（已停用的状态码、旧的地区代码）。改造为**分列策略**：枚举/低基数列用 `SELECT DISTINCT`，度量/数值/时间列保持最近行。

## 2. 现状（读码后核实）

`backend/app/core/schema.py::sample_values`（102-128 行）：

```python
async def sample_values(state, conn_id, table, per_column=10) -> dict[str, list]:
    # 整行主键倒序抽样：SELECT * ORDER BY <pk> DESC LIMIT n
    # 无主键表退化为不排序 LIMIT n
    # 返回 {列名: [该列各行值]}
```

问题：
- 枚举列（`status`/`region`/`type`）被时间偏置——`ORDER BY pk DESC` 只采到最近行的值，历史枚举值采不到
- 直接影响：T4 值重叠探测的准确率、T7 概念字典的枚举提取完整性

## 3. 接口与策略

```python
async def sample_values(state, conn_id, table, per_column=10) -> dict[str, list]:
    """分列策略：
    - 枚举/低基数列（见判定规则）→ SELECT DISTINCT col LIMIT n（不按时间偏置）
    - 度量/数值/时间列 → SELECT * ORDER BY pk DESC LIMIT n（最近样本）
    - 无主键表 → 不排序 LIMIT n
    """
```

**枚举列判定规则**（需实现一个启发式 `_looks_like_enum(col)`）：

| 信号 | 判定 |
|---|---|
| 类型为 `bool`/`boolean` | ✅ 枚举 |
| 类型为短 `char`/`varchar` 且 `length ≤ 20` | ⚠️ 候选，需结合基数 |
| 列名含 `status`/`type`/`kind`/`state`/`category`/`flag`/`is_` | ✅ 枚举倾向 |
| 列名含 `_at`/`_time`/`date`/`amount`/`price`/`qty`/`count` | ❌ 度量/时间 |
| 行数 ≤ 50 的小表 | 直接全表 DISTINCT（采样上限内） |

**实现方式建议**（两条 SQL，避免 N+1）：
1. 先查一次列信息（`list_columns` 已有）→ 按规则给每列分类
2. 枚举列：`SELECT DISTINCT col1, col2, ... FROM table LIMIT n`（或逐列 DISTINCT，取决于方言支持）
3. 非枚举列：沿用整行 `ORDER BY pk DESC LIMIT n`
4. 合并：返回 `{列名: [值列表]}`（结构不变，调用方零改动）

## 4. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | DISTINCT 后值数 < n | 全量返回（不补行） |
| 2 | 表无主键 | 非枚举列退化为不排序 LIMIT n（现状行为） |
| 3 | 列值超长（TEXT/JSON） | 截断到 60 字符（对齐现状 `serialize_value` 行为） |
| 4 | 方言不支持 `SELECT DISTINCT` 多列 | 逐列 DISTINCT（DialectAdapter 提供单列查询能力） |
| 5 | 枚举列判定歧义（char 但实际是 ID） | 保守：判定为枚举执行 DISTINCT（DISTINCT 对 ID 类也无害，只是全量去重） |
| 6 | 采样超时/大表 | 沿用现状 try/except，失败返回 `{}`（不炸构建） |

## 5. 日志要求（必须加）

```python
logger.debug("[schema.sample] conn=%(conn)s table=%(table)s enum_cols=%(enum)d metric_cols=%(metric)d", ...)
# 每次采样：枚举列数 / 度量列数（便于发现判定误判）
logger.warning("[schema.sample] conn=%(conn)s table=%(table)s 采样失败：%(err)s", ...)
# 采样异常（沿用现状，但不静默——warning 级别）
```

## 6. 测试设计（tests/core/test_schema_sample.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_enum_col_gets_historical_values` | 表含 status 列，旧值只在旧行（pk 小） | 采样结果包含旧值（DISTINCT 路径生效） |
| `test_metric_col_uses_recent` | amount 列 | 走 ORDER BY pk DESC（结果与现状一致） |
| `test_no_pk_fallback` | 无主键表 | 不排序 LIMIT n |
| `test_null_handled` | 列含 NULL | NULL 在结果中或剔除（二选一，注释说明） |
| `test_enum_detection_rules` | 各类列名（status/type/_at/amount/flag） | 分类结果符合 §3 规则表 |
| `test_sampling_failure_graceful` | mock adapter 抛异常 | 返回 `{}`，不抛 |

## 7. 执行步骤

- [x] 1. 实现 `_looks_like_enum(col)` 判定（§3 规则表）
- [x] 2. 改造 `sample_values`：先分类 → 枚举列 DISTINCT → 非枚举列最近行 → 合并
- [x] 3. 加日志（§5）
- [x] 4. 测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 8. 验收标准（我 review 时逐条检查）

1. `sample_values` 签名不变（调用方零改动），返回值结构不变
2. 枚举列不再被 `ORDER BY pk DESC` 偏置（测试覆盖历史值）
3. 判定规则可解释（`_looks_like_enum` 有注释说明信号来源）
4. 日志输出采样分类信息
5. 全量 pytest 通过

## 9. 完成定义

- [x] `_looks_like_enum` 判定实现
- [x] `sample_values` 分列改造（签名不变）
- [x] 日志就位
- [x] 6 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §6「采样分列策略」设计点
- 现状代码：`core/schema.py::sample_values`（102-128 行）、`core/query.py::serialize_value`
- 调用方（不能破坏）：`api/knowledge.py`（build/sync 抽样）、T4 值重叠探测


## 偏差记录（2026-08-30 修复轮 R4）

1. **采样失败语义已对齐 §4#6**：度量路径异常 → warning 日志 + 返回已采到的部分结果（不炸构建）；枚举路径逐列降级保持现状。
2. **char 长度判定已实现**：`_looks_like_enum` 从 data_type 提取 `(N)`——有长度且 >20 → 走最近行路径（更可能是 ID/描述）；裸类型（无长度信息）保守判枚举（DISTINCT 对 ID 类无害，§4#5）。
3. 每次采样日志补齐度量列数：`enum_cols=%d metric_cols=%d total=%d`（§5）。
