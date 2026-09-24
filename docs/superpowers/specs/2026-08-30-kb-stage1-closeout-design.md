# 知识库收尾设计（阶段 1）：图校验器接线 + 多态分组采样 + 反馈循环补全

> 日期：2026-08-30 · 状态：已批准 · 定位：对齐 `docs/knowledge-and-engine-design.md` 的剩余差异收敛
> 前置：T1–T10 全部完成（全量 675 passed）；方案 A（高危断链）与方案 B（中危）已交付

---

## 1. 背景与目标

设计文档（§1–§11）的 ✅ 项已全部达成，⚠️ 项中"视图作普通节点"已天然覆盖（schema.py:249）。
剩余差异集中三处：

1. **§10⑤ 图校验器未接线**——validate_join 只被单测覆盖，AI 循环不调用（设计最强能力"拦幻觉 join"未生效）
2. **§3.4 多态守卫边生产不产**——`_grouped` 分组采样缺失，polymorphic 边只在手工构造样本时产
3. **§10⑥ 执行反馈循环部分缺失**——报错反馈已有（有界纠错），空结果提示未补

本阶段收敛这三处 + 收尾小项（T5 截断/小表信号、few-shot 上限配置、日志补全）。

## 2. R1 · 图校验器接线（§10⑤）

### 2.1 落点

`app/ai/tools/sql.py::_run_query`（AI 查询唯一入口，report/db_read 均经此），
在 `prepare_query_sql` 加工后、安全闸门评估前。

### 2.2 实现

- **抽取共享纯函数** `extract_join_pairs(sql) -> list[(t1, c1, t2, c2)]`：
  从 `log_mining.py` 现有 JOIN 解析逻辑提取（别名映射、等值列对、无表限定跳过），
  供 R1 与 T10 日志挖掘复用（单一实现）
- 对每对列对调 `state.knowledge.validate_join(conn_id, t1, c1, t2, c2)`
- **全部命中才放行**；未命中 → 返回"打回重写"反馈（非安全 BLOCK）

### 2.3 打回语义

- 反馈格式复用有界纠错模式（sql.py:93-108）：`result={"ok": False, "error": ..., "hint": ...}` +
  card 附说明，模型在下一 MAX_TURNS 轮次重写
- **同 turn 连续失败 >2 次 → 降级放行 + warning**（防死循环）
- **mock 降级**：mock 硬编码 SQL 图外 join → 放行 + warning（演示模式不拦）
- 无 JOIN 的单表 SQL 不校验（零开销）

### 2.4 测试（tests/ai/test_plausibility_gate.py）

| 用例 | 断言 |
|---|---|
| 图内 join 放行 | 正常执行 |
| 图外 join 打回 | 返回 ok=False + hint，未执行 |
| 别名 join | `orders o JOIN users u ON o.user_id = u.id` 放行 |
| 复合键 join | 逐列校验，全命中放行 |
| 同 turn 连续失败降级 | 第 3 次失败后放行 + 执行 |
| mock 图外 join | 放行 + 不炸 |

## 3. R2 · 多态分组采样（§3.4）

### 3.1 落点

`app/core/schema.py::sample_values`。

### 3.2 实现

- 检测判别器模式：表同时含 低基数（distinct ≤ 20）判别器列（`type/_type/kind`）+
  引用列（`_id/_code` 后缀，且不是主键）→ 追加分组查询
  `SELECT disc, ref FROM table GROUP BY disc`（每组 LIMIT 采样上限）
- 产出约定与 builder.py:262-268 已定义格式一致：

  ```
  out["_grouped"] = {"type": {"__ref__": "code", 1: ["T1-A", ...], 2: ["T2-A", ...]}}
  ```

- 无判别器模式 → 零额外查询（现状不变）；查询失败静默降级（该表不产 _grouped）

### 3.3 测试

- `tests/core/test_schema_sample.py`：判别器表产 `_grouped`；非判别器表不产；失败降级
- `tests/knowledge/test_graph_builder.py`：真实 sample_values 输出 → `build_polymorphic_edges` 产守卫边

## 4. R3 · 空结果反馈（§10⑥）

- 查询成功但 `row_count == 0` → card 附 `empty_hint`（"返回空，可能条件过严，可尝试放宽"）
- 不阻断、不改 verdict；模型自检后自行决定重写
- 测试：`tests/ai/test_plausibility_gate.py` 补空结果用例

## 5. R4 · T5 采样小项

- `serialize_value`（query.py:73）：TEXT/JSON 字符串 > 60 字符截断（T5 §4#3）
- `sample_values`：小表（行数 ≤ 50）全表 DISTINCT（T5 §3 信号，经 list_columns 无行数 →
  用 count_rows 或退化：表行数未知时跳过该信号，仅在有 row_count 时生效）
- 补 `test_null_handled`（NULL 剔除语义）

## 6. R5 · 收尾小项

- few-shot 上限 env 化：`TABLETALK_FEWSHOT_MAX`（默认 500），读取点 `fewshot.py::_MAX_PER_CONN`
- T4 跳过原因日志补全：命名推断（类型族/自环/泛型名）、overlap（低基数/PK 排除/去重）debug 日志
- `_docs` 归属偏差补记 T1-skeleton.md 偏差记录

## 7. 明确不做（后置）

- 引擎重构（Plan-and-Execute，阶段 2）
- 一列多义条件归属（§11 ⚠️，概念字典人工确认已兜底）
- 跨 schema 表身份（§11 ⚠️，演示库无场景）
- 字段改名孤儿检测（§4.2，低优先级）
- 非等值/范围 join（§11 ⏳，设计明确不建模）

## 8. 验收

- 每个 R 项对应测试真实断言（无空断言）
- 全量 pytest 通过（基线 675，预期 +8~12）
- 重启 sidecar，health 正常；demo 库冒烟（filters/concepts API 可用）