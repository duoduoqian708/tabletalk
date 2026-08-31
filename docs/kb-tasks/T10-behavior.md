# T10 · L3 行为层（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T2 + T9
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

实现 `behavior/` 三个模块：查询日志挖掘建边 + 边加权 + few-shot 库。这是 L3 行为层——让知识库"越用越准"（对齐设计 §2 L3 + §7 Phase D + §10⑦）。

## 2. 现状（读码后核实）

- 无任何 L3 能力（无日志挖掘、无边加权、无 few-shot）
- 可用数据源：
  - 审计日志：`data_dir/audit.log`（JSONL，`audit/logger.py` 写入，含 sql 字段）——历史 SQL 来源
  - 边模型已有 `weight` 字段（storage.py edges 表）——但从未启用
  - 对话/查询历史：`data_dir/chat.db`——问题原文来源

## 3. 接口定义（实现必须对齐）

```python
# backend/app/knowledge/behavior/log_mining.py
def mine_join_edges(audit_rows: list[dict]) -> list[GraphEdge]:
    """从审计日志 SQL 提取实际 join 过的表对/列对。

    - 入参：audit_rows = [{sql: str, connection: str, executed_at: str}, ...]
    - 用 sqlglot 解析每条 SQL 的 JOIN 子句 → 提取 (table, col) 对
    - 产出：relation=query_log, confidence=0.9, provenance=query_log, reason="查询日志"
    - 只增不减：与现有边去重（同列对已存在则不重复产）
    """

# backend/app/knowledge/behavior/weighting.py
def bump_weights(edges: list[GraphEdge], used: list[GraphEdge], factor: float = 1.1) -> int:
    """成功查询使用的 join 边 → weight *= factor（封顶 10.0），返回更新条数。

    - used：本次查询实际经过的边（由消费链路记录）
    - 幂等：重复调用不重复计数（由调用方保证 used 去重）
    """

# backend/app/knowledge/behavior/fewshot.py
def add_fewshot(conn_id: str, question: str, sql: str, join_path: list[str]) -> None:
    """成功问答对入库（fewshot 表或 JSON 文件）"""
def recall_fewshot(conn_id: str, question: str, k: int = 3) -> list[dict]:
    """相似问题召回 → [{question, sql, join_path}]，作为 in-context 样例。
    - v1：关键词/词面相似（复用 vectorstore 的 HashingEmbedder 或简单 token 重叠）
    - 有 API 嵌入时升级为向量召回
    """
```

**fewshot 表（storage.py 增加）**：

```sql
CREATE TABLE IF NOT EXISTS fewshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conn_id TEXT NOT NULL,
  question TEXT NOT NULL,
  sql TEXT NOT NULL,
  join_path TEXT,              -- JSON: ["orders.user_id = users.id"]
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fewshot_conn ON fewshot(conn_id);
```

## 4. 触发时机（接入点）

| 能力 | 触发 | 接入位置 |
|---|---|---|
| 日志挖掘 | 定时（复用 SyncLoop 节奏）或构建后 | `knowledge/jobs.py::SyncLoop` 或独立 task |
| 边加权 | 每次成功查询后 | `core/query.py` 执行成功路径（T8 注入过滤器同一位置附近） |
| few-shot 入库 | 每次成功查询后（问题+SQL+路径） | 同上 |
| few-shot 召回 | 每次 AI 查询组装上下文时 | `ai/context.py`（T9 的 prompt 组装处） |

## 5. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | 审计 SQL 解析失败（sqlglot 报错） | 跳过该条，记 debug 日志，不中断批量 |
| 2 | JOIN 列对无法提取（子查询/动态表） | 跳过（宁缺勿错） |
| 3 | 挖掘的边与现有边同列对 | 去重：已存在则跳过（confidence 高的保留） |
| 4 | 挖掘的边是幻觉（表不存在） | 用 schema 校验：表/列不存在则丢弃 |
| 5 | weight 封顶 | max(10.0)，防无限增长 |
| 6 | few-shot 表膨胀 | 按 conn 清理最旧（保留上限 500 条/连接，可配置） |
| 7 | few-shot 召回含敏感表 | 过 B3 代号化（与 context 一致） |

## 6. 日志要求（必须加）

```python
logger = logging.getLogger("kb.behavior")

logger.info("[behavior] 日志挖掘：解析 %d 条 SQL，提取 join 边 %d 条（去重后 %d）",
            len(audit_rows), extracted, added)
logger.debug("[behavior] 跳过 SQL：%(reason)s（id=%(id)s）", ...)
logger.info("[behavior] 边加权：更新 %d 条（本次查询）", updated)
logger.info("[behavior] few-shot：新增 1 条（conn=%(conn)s），当前 %d 条", total)
logger.info("[behavior] few-shot：召回 %d 条（conn=%(conn)s）", len(hits))
```

## 7. 测试设计（tests/knowledge/test_behavior.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_mine_join_edges_basic` | 含 `JOIN users ON orders.user_id = users.id` 的 SQL | 提取出 (orders,user_id)-(users,id) 边 |
| `test_mine_skips_unparseable` | 非法 SQL | 跳过不中断，返回剩余 |
| `test_mine_drops_phantom_tables` | join 不存在的表 | 边被丢弃（schema 校验） |
| `test_bump_weights` | 边 weight=1.0，bump×1.1 | weight=1.1；重复调用幂等 |
| `test_weight_cap` | 多次 bump 至超限 | 封顶 10.0 |
| `test_fewshot_add_recall` | 入库 2 条相似问题 | recall 按相似度召回 |
| `test_fewshot_limit` | 超上限入库 | 最旧被清理 |

## 8. 执行步骤

- [x] 1. `storage.py` 加 fewshot 表
- [x] 2. `log_mining.py`：sqlglot 解析 JOIN → 提取表对 → schema 校验 → 去重
- [x] 3. `weighting.py`：bump_weights（幂等 + 封顶）
- [x] 4. `fewshot.py`：add/recall（v1 词面相似）
- [x] 5. 接入点：query 成功路径（加权 + few-shot 入库）、SyncLoop（日志挖掘）、context.py（召回）
- [x] 6. 日志就位
- [x] 7. 测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 9. 验收标准（我 review 时逐条检查）

1. 三个模块接口与 §3 一致
2. 日志挖掘：真实跑一段查询后，图里出现 query_log 边（我用 demo 库验证）
3. 边加权：成功查询后 weight 变化，路径序列化排序生效
4. few-shot：能召回相似问答（词面相似可用即可）
5. 敏感表 few-shot 过代号化
6. 全量 pytest 通过

## 10. 完成定义

- [x] 日志挖掘（sqlglot + schema 校验 + 去重）
- [x] 边加权（幂等 + 封顶）
- [x] few-shot（add/recall + 上限清理）
- [x] 三个接入点接通
- [x] 7 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §2（L3 行为层）/ §6（查询日志来源）/ §7 Phase D / §10⑦
- 现状代码：`audit/logger.py`（audit.log 格式）、`storage.py`（weight 字段已存在）、`ai/context.py`（召回接入点）、`core/query.py`（加权接入点）


## 偏差记录（2026-08-30 修复轮 R7/R13）

1. **三接入点全部接线**：query 成功路径（ai/tools/sql.py + api/query.py 手动查询）→ `record_query_success`（join 边加权 + few-shot 入库，静默降级）；SyncLoop.tick → 审计行挖掘 → query_log 边；context → few-shot 召回 + B3 代号化。
2. **加权键去 kind 化**：used 边来自 query_log 挖掘，存量边 kind 各异——按列对匹配跨来源加权（weighting._dict_key）。
3. **挖掘 schema 校验已实现**（§4#4）：`mine_join_edges(audit_rows, schema=None)`，表/列不存在即丢弃；`test_mine_drops_phantom_tables` 覆盖。
4. **偏差：审计源为 SQLite audit_log 表**（`audit.list`，verdict=allow 行），非 JSONL audit.log；few-shot v1 词面相似召回（文档允许）；日志挖掘唯一触发点为 SyncLoop（增量重建不挖，避免构建链路偶合）。
