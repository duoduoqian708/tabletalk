# T8 · 表级过滤器 + 会话变量池（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T1（独立于 T3–T6，可并行）
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

实现 `filters.py` 两块能力：

1. **表级过滤器**：软删除/租户/数据权限——定义在语义层（L2）、执行在横切（查询层自动注入）
2. **会话变量池**：运行时占位符（`:current_tenant`/`:current_user`/`:current_date`...）——LLM 引用名字，执行层替换真实值

对齐设计 §8（会话变量）+ §9（表级过滤器）。

## 2. 关键概念（务必区分，别混）

| | 表级过滤器 | 守卫边（T3/T4） |
|---|---|---|
| 挂在 | 表上 | 边上 |
| 谓词依赖对方表吗 | ❌ 不依赖 | ✅ 依赖 |
| 何时生效 | 表一出现就要带（含单表查询） | 仅遍历该边时带 |
| 例子 | `is_deleted = 0`、`tenant_id = :current_tenant` | `X.type = 1`（区分连哪张表） |

**会话变量 ≠ 静态常量**（T7）：`tenant_id`/`user_id` 是运行时变量，用占位符；税率/阈值是静态常量，进概念字典。

## 3. 数据模型与接口（实现必须对齐）

```python
# backend/app/knowledge/filters.py

@dataclass
class TableFilter:
    table: str
    predicate: str            # "is_deleted = 0" 或 "tenant_id = :current_tenant"
    scope: str = "table"      # connection_default | table | exempt
    status: str = "draft"     # draft | confirmed

class FilterStore:
    def __init__(self, data_dir: Path): ...

    def detect_candidates(self, schema: dict) -> list[TableFilter]:
        """结构检测（确定性）：发现 is_deleted/deleted_at/tenant_id/org_id 列 → draft 候选。
        判定规则：
        - 列名 is_deleted/deleted/valid/deleted_at/removed → 软删除候选 "x = 0"（deleted_at 用 IS NULL）
        - 列名 tenant_id/tenant/org_id/org → 租户候选 "x = :current_tenant"（值由会话变量替换）
        """

    def get_filters(self, conn_id: str, tables: set[str]) -> dict[str, list[str]]:
        """返回 {表: [谓词串]}：confirmed 的表级 filter + 连接级默认（排除 exempt 表）"""

    def confirm(self, conn_id: str, table: str) -> bool: ...
    def reject(self, conn_id: str, table: str) -> bool: ...

# 会话变量
SESSION_VARS: dict[str, dict] = {
    ":current_tenant": {"type": "int",  "desc": "当前租户ID"},
    ":current_user":   {"type": "int",  "desc": "当前用户ID"},
    ":current_date":   {"type": "date", "desc": "今天（本地时区）"},
    ":current_org":    {"type": "int",  "desc": "当前组织ID"},
}

def session_vars_prompt() -> str:
    """注入 context 的变量清单文本（LLM 知道有哪些可用）"""

def resolve_session_var(name: str, ctx: dict) -> str:
    """执行时替换：
    - ctx 提供 {current_tenant: 7, current_user: 3, ...}（来自连接配置/会话）
    - 时间变量取系统时钟（datetime.now() 格式化）
    - 未知变量 → 抛 KeyError（调用方捕获并告警）
    """
```

**filters 表（storage.py 增加）**：

```sql
CREATE TABLE IF NOT EXISTS table_filters (
  table_name TEXT PRIMARY KEY,
  predicate TEXT NOT NULL,
  scope TEXT DEFAULT 'table',      -- connection_default | table | exempt
  status TEXT DEFAULT 'draft'
);
```

**连接级默认**（存 settings 或 filters 表 scope=connection_default 行）：

```python
# 连接配置扩展（connections.json 或 settings）：
#   multi_tenant: bool, tenant_col: str
# 单租户部署：全局默认 {current_tenant: <值>} 来自设置
```

## 4. 执行注入（横切）

查询层（`core/query.py` 或安全闸门路径）在 SQL 执行前注入：

```python
def inject_filters(sql: str, filters: dict[str, list[str]], ctx: dict) -> str:
    """把表级过滤器注入 SQL：
    - 对 SQL 中出现的每张表，追加其谓词（WHERE 条件 AND）
    - 谓词中的 :current_tenant 等由 resolve_session_var 替换
    - 注入后过安全闸门（原有流程不变）
    """
```

> 注入实现注意：用 sqlglot 解析 SQL → 按表追加过滤条件 → 重组。**不能简单字符串拼接**（子查询/别名会漏）。v1 可先支持简单 SELECT（无子查询嵌套），复杂 SQL 降级为日志告警 + 不注入（宁缺勿错）。

## 5. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | 表无 filter | 不注入 |
| 2 | exempt 表（系统表） | 不注入租户过滤（连接级默认豁免） |
| 3 | 会话变量无法解析（未配置当前租户） | 告警 + 不注入该谓词（避免注入错误值） |
| 4 | SQL 有子查询/复杂结构 | v1 不注入（日志告警），宁缺勿错 |
| 5 | 表出现多次（别名） | sqlglot 解析后按底层表注入一次 |
| 6 | filter 谓词含 `:current_date` | 执行时替换为系统时钟日期 |

## 6. 日志要求（必须加）

```python
logger = logging.getLogger("kb.filters")

logger.info("[filters] conn=%(conn)s detect 候选 %d 个：%(items)s", ...)
logger.info("[filters] conn=%(conn)s confirm/reject table=%(table)s", ...)
logger.warning("[filters] conn=%(conn)s 无法解析会话变量 %(var)s，跳过注入", ...)
logger.warning("[filters] conn=%(conn)s SQL 含子查询，跳过过滤器注入（需人工检查）", ...)
logger.info("[filters] conn=%(conn)s 注入过滤器：%(tables)s", ...)
```

## 7. 测试设计（tests/knowledge/test_filters.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_detect_soft_delete` | 表含 is_deleted 列 | 产候选 "is_deleted = 0"（draft） |
| `test_detect_tenant` | 表含 tenant_id 列 | 产候选 "tenant_id = :current_tenant" |
| `test_get_filters_merged` | 表级 + 连接级默认 + exempt 表 | 合并正确，exempt 排除 |
| `test_resolve_session_var` | ctx={current_tenant: 7} | `resolve_session_var(":current_tenant", ctx) == "7"` |
| `test_resolve_current_date` | 空 ctx | 返回今天日期串 |
| `test_resolve_unknown_raises` | 未知变量 | 抛 KeyError |
| `test_inject_simple_select` | `SELECT * FROM orders` + filter | 产出 `SELECT * FROM orders WHERE is_deleted = 0` |
| `test_inject_subquery_skipped` | 含子查询 | 不注入 + 日志告警 |
| `test_inject_var_replaced` | 谓词含 :current_tenant | 注入后为真实值 |

## 8. 执行步骤

- [x] 1. `storage.py` 加 table_filters 表
- [x] 2. `filters.py`：`TableFilter`/`FilterStore`/`SESSION_VARS`/`resolve_session_var`/`session_vars_prompt`
- [x] 3. 连接级默认（multi_tenant + tenant_col + 默认租户值来源）
- [x] 4. `inject_filters`（sqlglot 解析，v1 简单 SELECT）
- [x] 5. 日志就位
- [x] 6. 测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 9. 验收标准（我 review 时逐条检查）

1. 接口与 §3 一致
2. 过滤器定义在语义层（表属性），**不进图**（我 grep 确认 filters 不写 edges）
3. 会话变量用占位符，执行层替换，LLM 不接触真实值
4. `inject_filters` 用 sqlglot（非字符串拼接），子查询降级正确
5. 日志覆盖检测/注入/变量解析失败
6. 全量 pytest 通过

## 10. 完成定义

- [x] table_filters 表 + FilterStore
- [x] SESSION_VARS + resolve_session_var
- [x] inject_filters（sqlglot）
- [x] 日志就位
- [x] 9 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §8（会话变量）/ §9（表级过滤器）/ §8.4（安全意义）
- 现状代码：`core/query.py`（执行路径，注入点）、`app/config.py`/`connections.py`（连接配置扩展点）
- 下游消费者：T9 消费流程（变量清单进 prompt + 过滤器注入）


## 偏差记录（2026-08-30 修复轮 R6/R11）

1. **inject_filters 已接入查询执行路径**（4 个执行点）：api/query.py（主路径）、ai/tools/sql.py、ai/report.py、ai/tasks/runner.py——闸门评估/执行/审计一律用加工后 SQL；confirm-token TOCTOU 校验保留原始 SQL（token 存原文哈希）。
2. **会话变量替换为 AST 级**（优于文档示例的正则）：sqlglot 解析后仅替换 Placeholder 节点且键在 SESSION_VARS 中，字符串字面量（'a:current_tenant'）绝不误替换；未配置变量 → warning + 保留占位符。
3. **连接级默认最小实现**：`ConnectionConfig.session_vars` dict（create/update/API 三层已贯通）；前端连接表单未加字段（后端兼容，缺省 {}）。
4. `table_filters` 表已按 §3 建（R11）；`FilterStore.add` 已补。
