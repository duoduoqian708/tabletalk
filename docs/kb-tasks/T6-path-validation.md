# T6 · 路径序列化 + 图校验（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T3
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

实现 `graph/traverse.py` 两个核心能力：

1. **`path_strings`**：从种子表 BFS 产出 LLM 可直接用的 join 路径串（替代现在只给表集合的 `expand_tables`）
2. **`validate_join`**：图校验（plausibility gate）——判断给定 join 是否在图里，拦截幻觉 join

对齐设计 §10②③⑤：路径串是"图作为检索器"的输出形态；图校验是"图作为校验器"的落点。

## 2. 现状（读码后核实）

`store.py` 现有 BFS 缺陷（T1 后迁入 `graph/store.py`）：

```python
def _fk_adj(self, conn_id): ...          # 只按表建邻接，丢列对信息
def expand_tables(self, conn_id, seeds, hops=2): ...  # 只返回表集合（set），吞平行边
```

- `_fk_adj` 用 `set` 建邻接 → 同一对表的多条平行边（user_id/shipped_by/approved_by）被合并，**丢列对信息**
- `expand_tables` 只返回表名集合 → 消费端不知道"通过哪两列连"

## 3. 接口定义（实现必须对齐）

```python
# backend/app/knowledge/graph/traverse.py
"""图遍历：路径序列化 + join 校验。纯函数（edges 由调用方传入），无 I/O。"""

def path_strings(edges: list[GraphEdge], seeds: set[str], hops: int = 2) -> list[str]:
    """从种子表 BFS（按边区分，不按表去重），输出路径串列表。

    输出格式：
    - 普通边：  "orders.user_id = users.id (n:1)"
    - 复合键：  "orders.order_id = users.id AND orders.line_no = users.line_no"
    - 守卫边：  "X.code = table1.code AND X.type = 1 (n:1)"
    - 自环：    正常输出（visited 集合防死循环）
    - 排序：    高 confidence 优先；同 confidence 按 (source_table, target_table)
    """

def validate_join(edges: list[GraphEdge], from_table: str, from_col: str,
                  to_table: str, to_col: str) -> bool:
    """给定 join 列对（双向任一方向）是否存在于图中（任意 confidence）。

    - 匹配规则：存在某条边 cols 含 (from_col, to_col) 或 (to_col, from_col)
    - 守卫边也参与匹配（存在即合法候选）
    - 纯函数，可穷举单测
    """
```

**BFS 规则**（修正 `_fk_adj` 缺陷）：

```
- 邻接按 (source_table, target_table) + cols 建立（每条边一个邻居项，不合并）
- visited 按表名去重（防环），但邻居遍历按边展开（保平行边）
- 自环边：visited 已含 source，不会重复入队（天然防死循环）
- hops 边界：与现有 expand_tables 一致（默认 2）
```

## 4. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | seeds 为空 | 返回 `[]` |
| 2 | 种子表不在图中 | 只输出该表自身可达的边（或返回空，注释说明） |
| 3 | 平行边 | 全保留（不合并）——这是本任务修正的核心 |
| 4 | 复合键 | AND 连接多对列 |
| 5 | 守卫边 | 输出尾部追加 ` AND {guard}` |
| 6 | 自环 | 输出正常，BFS 不死循环（visited 兜底） |
| 7 | 边方向相反 | `validate_join` 双向匹配；`path_strings` 只沿存储方向输出 |
| 8 | 低置信度边（confidence < 0.5） | 路径输出中不排除（图校验兜底用），但排序靠后 |

## 5. 日志要求（必须加）

```python
logger = logging.getLogger("kb.graph_traverse")

logger.info("[graph_traverse] seeds=%s hops=%d 输出路径 %d 条", seeds, hops, len(out))
# 每次路径生成：种子数/跳数/路径条数
logger.debug("[graph_traverse] 路径: %s", path)
# 每条路径（debug 级，供消费链路排查）
logger.info("[graph_traverse] validate_join %s.%s = %s.%s → %s", ...)
# 每次校验：入参 + 结果（图校验命中/未命中，供 T9 打回重写排查）
```

## 6. 测试设计（tests/knowledge/test_graph_traverse.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_parallel_edges_all_output` | orders↔users 三条边（user_id/shipped_by/approved_by） | 输出 3 条路径（不全被吞） |
| `test_composite_key_path` | cols 两对列 | 输出含 `AND` |
| `test_guarded_path` | guard="X.type = 1" | 输出尾部含 `AND X.type = 1` |
| `test_self_loop_no_deadlock` | 自环边 + 普通边 | BFS 正常返回，不死循环 |
| `test_hops_bound` | 3 跳可达的链 | hops=2 时不含第 3 跳 |
| `test_empty_seeds` | seeds=set() | 返回 `[]` |
| `test_validate_join_hit` | 边 cols=[("user_id","id")] | validate_join(orders,user_id,users,id) → True |
| `test_validate_join_reverse` | 同上 | validate_join(users,id,orders,user_id) → True（双向） |
| `test_validate_join_miss` | 无此边 | → False |
| `test_validate_join_guarded` | 守卫边 | → True（存在即合法候选） |

## 7. 执行步骤

- [x] 1. 实现 `path_strings`（BFS 按边展开 + visited 防环 + 输出格式）
- [x] 2. 实现 `validate_join`（双向匹配）
- [x] 3. 修正 `_fk_adj`/`expand_tables` 缺陷（或直接由 `path_strings` 取代，标注旧函数废弃）
- [x] 4. 加日志（§5）
- [x] 5. 测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 8. 验收标准（我 review 时逐条检查）

1. `path_strings`/`validate_join` 签名与 §3 一致
2. BFS 不再按表去重吞平行边（`test_parallel_edges_all_output` 真实断言 3 条）
3. 输出格式符合 §3 规则（复合 AND / 守卫追加 / 自环不死循环）
4. `validate_join` 双向匹配正确
5. 日志就位（路径条数 + 校验结果）
6. 全量 pytest 通过

## 9. 完成定义

- [x] `path_strings` 实现（BFS 修正）
- [x] `validate_join` 实现
- [x] 日志就位
- [x] 10 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §10②③⑤（消费流程）/ §3.2（边属性）
- 现状代码：`store.py::_fk_adj`/`expand_tables`（T1 后迁入 graph/store.py）
- 下游消费者：T9 消费流程（context.py 路径串进 prompt + 校验接入）


## 偏差记录（2026-08-30 修复轮 R3）

1. **expand_tables 已重写**：委托 `traverse.reachable_tables`（按边 BFS、精确表名匹配、`kinds={"fk"}`），旧 `_fk_adj`（lower() 错配 + set 合并吞平行边）已删；大小写错配回归测试到位。
2. **validate_join 生产接线仍缺**：图作为「校验器」的落点（拦截幻觉 join）目前无生产调用方（仅门面暴露 + 穷举单测）。接线到 AI 循环（plausibility gate）需要与安全闸门交互设计，列入后续工作。
