# T3 · 边模型升级（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T2
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

把边从「单列对」升级为「列对列表」，支持三种新能力：

1. **复合键**：join 需要多列 AND 连接（`A.order_id = B.id AND A.line_no = B.line_no`）
2. **守卫边**：多态关联（`X.type=1` 时 `code` 连 table1）
3. **自环**：`employee.manager_id → employee.id`
4. **置信度/来源**：每条边携带 confidence + provenance

## 2. 现状（读码后核实）

当前 `storage.py::_SCHEMA` 的 edges 表：

```sql
CREATE TABLE IF NOT EXISTS edges (
  from_table TEXT, from_col TEXT, to_table TEXT, to_col TEXT,
  kind TEXT, weight REAL, shared INTEGER,
  cardinality TEXT, reason TEXT
);
```

读写点：
- 读：`SqliteStorage._read`（`from_table`→`from`、`to_table`→`to` 重命名，`e.setdefault("cardinality","n:1")`）
- 写：`SqliteStorage.save`（`e.get("from")` 等字段回写）
- 产边：`KnowledgeBase._build_graph`（只产 kind=fk）

**单列模型缺陷**：`from_col/to_col` 表达不了复合键；无 guard/confidence/provenance 字段；`shared` 字段闲置。

## 3. 数据模型（实现必须对齐）

```python
@dataclass
class GraphEdge:
    source_table: str
    target_table: str
    cols: list[tuple[str, str]]        # [("user_id","id"),("line_no","line_no")]
    cardinality: str = "n:1"           # "1:1" | "1:N" | "N:M"
    relation: str = "fk"               # fk|naming|value_overlap|query_log|user|same_dimension
    confidence: float = 1.0
    provenance: str = "declared_fk"    # declared_fk|naming_inference|value_overlap|query_log|human
    guard: str | None = None           # "X.type = 1"
    weight: float = 1.0
    reason: str = ""
    metadata: dict = field(default_factory=dict)

    def join_condition(self, quote: Callable[[str], str] = lambda x: x) -> str:
        """序列化 join 条件：
        - 单列：  "orders.user_id = users.id"
        - 复合：  "A.order_id = B.id AND A.line_no = B.line_no"
        - 守卫：  追加 " AND X.type = 1"
        """
```

**join_condition 输出规则**（验收按此对照）：

| 情况 | 输入 | 输出 |
|---|---|---|
| 单列 | cols=[("user_id","id")] | `orders.user_id = users.id` |
| 复合 | cols=[("order_id","id"),("line_no","line_no")] | `orders.order_id = users.id AND orders.line_no = users.line_no` |
| 守卫 | guard="X.type = 1" | 上述输出 + ` AND X.type = 1` |
| 自环 | source==target | 正常输出（无特殊处理，防环在 BFS） |

## 4. 存储 schema（新）

```sql
CREATE TABLE IF NOT EXISTS edges (
  source_table TEXT NOT NULL,
  target_table TEXT NOT NULL,
  cols TEXT NOT NULL,                -- JSON: [["user_id","id"],["line_no","line_no"]]
  cardinality TEXT DEFAULT 'n:1',
  relation TEXT DEFAULT 'fk',
  confidence REAL DEFAULT 1.0,
  provenance TEXT DEFAULT 'declared_fk',
  guard TEXT,
  weight REAL DEFAULT 1.0,
  reason TEXT DEFAULT '',
  metadata TEXT DEFAULT '{}',
  PRIMARY KEY (source_table, target_table, cols, guard)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_table);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_table);
```

**PRIMARY KEY 设计说明**：含 `cols`（JSON 串）+ `guard`，保证：
- 同一表对不同列对 = 不同边（多重图共存）
- 同一 (表对, 列对) 不同 guard = 不同边（守卫边并存）
- `guard` 为 NULL 时，SQLite 中 NULL 不等于 NULL——需要额外处理（见边界规则 5）

## 5. 迁移规则（旧库 → 新库）

| 旧字段 | 新字段 |
|---|---|
| `from_table`/`from_col`/`to_table`/`to_col` | `source_table`/`target_table` + `cols=[[from_col,to_col]]` |
| `kind='fk'` | `relation='fk'`，`provenance='declared_fk'`，`confidence=1.0` |
| `kind='user'` | `relation='user'`，`provenance='human'`，`confidence=1.0` |
| `kind='llm'`（draft） | **不进 edges**，保留在 `llm_graph_edges` |
| `cardinality`/`reason`/`weight` | 直接平移 |

迁移实现建议（`SqliteStorage._init` 内）：
1. `PRAGMA table_info(edges)` 检测：有 `from_table` 无 `source_table` → 旧 schema
2. 读旧表全部行 → 构造 `GraphEdge` → 重建新表（DROP + CREATE + INSERT）
3. 迁移失败时保留旧库备份（`knowledge-{conn}.db.bak`）

## 6. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | `cols=[]` | **禁止**，构造时抛 `ValueError("edge requires at least one column pair")` |
| 2 | 自环（source==target） | 允许存储；防死循环在 BFS 层处理（T6） |
| 3 | guard 为 NULL | 非守卫边；`PRIMARY KEY` 中 NULL 不相等——用 `IFNULL(guard,'')` 归一化存储 |
| 4 | 复合键基数 | 复合键的 `cardinality` 按整体声明，不逐列 |
| 5 | 重复边（同表对同列对同 guard） | PRIMARY KEY 冲突 → `INSERT OR REPLACE`（幂等） |
| 6 | 旧 `shared` 字段 | 弃用，数据并入 `metadata` 或丢弃 |

## 7. 存储接口（GraphStore 读写）

```python
class GraphStore:
    def get_edges(self, conn_id: str) -> list[GraphEdge]:
        """读取全部边（含复合键/守卫边），按 source_table 排序"""

    def put_edges(self, conn_id: str, edges: list[GraphEdge]) -> None:
        """全量替换（构建用）：DELETE FROM edges + 批量 INSERT"""

    def add_edge(self, conn_id: str, edge: GraphEdge) -> None:
        """INSERT OR REPLACE 单条边"""

    def remove_edge(self, conn_id: str, edge: GraphEdge) -> bool:
        """按 (source_table, target_table, cols, guard) 删除，返回是否删到"""

    def is_tombstoned(self, conn_id: str, edge: GraphEdge) -> bool:
        """墓碑匹配（按表对 + 列对），沿用现有 _tombstone_key 逻辑改造"""
```

## 8. 测试设计（tests/knowledge/test_edge_model.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_single_col_migration` | 手写旧 schema 库（含 from_table 行） | `load()` 后 `cols == [["user_id","id"]]`，`provenance == "declared_fk"` |
| `test_composite_condition` | `GraphEdge(cols=[("order_id","id"),("line_no","line_no")])` | `join_condition() == "orders.order_id = users.id AND orders.line_no = users.line_no"` |
| `test_guarded_condition` | guard="X.type = 1" | `join_condition()` 尾部含 `" AND X.type = 1"` |
| `test_self_loop_roundtrip` | source==target 的边 put→get | 读回一致 |
| `test_parallel_edges_coexist` | 同表对不同列对两条边 | 两条都读回（未被去重吞掉） |
| `test_guarded_edges_coexist` | 同 (表对,列对) 不同 guard 两条边 | 两条都读回 |
| `test_empty_cols_rejected` | `GraphEdge(cols=[])` | 抛 `ValueError` |

## 9. 验收标准（我 review 时逐条检查）

1. `GraphEdge` 字段/`join_condition` 签名与本文件 §3 一致
2. 迁移：旧 `knowledge-{conn}.db` 读入后边不丢、cols 正确、kind 映射正确
3. `join_condition` 对单列/复合/守卫三种情况输出与 §3 规则表一致
4. 7 个测试用例**真实断言**了上述行为（我会抽查是否有空断言）
5. 全量 `pytest` 通过

## 10. 完成定义

- [x] `GraphEdge` 数据类 + `join_condition` 实现
- [x] `storage.py` edges 新 schema + 旧库迁移
- [x] `GraphStore` 五个读写接口
- [x] 7 个测试全绿 + 全量回归绿

---

## 参考

- 设计文档 `docs/knowledge-and-engine-design.md` §3.2（边属性）/ §3.4（守卫边）
- 阶段计划 `docs/knowledge-refactor-plan.md` Phase 1.1
- 现状代码 `backend/app/knowledge/storage.py`（`_SCHEMA`/`_read`/`save`）、`backend/app/knowledge/store.py`（`_build_graph`/`_tombstone_key`）


## 偏差记录（2026-08-30 修复轮 R1/R11）

1. **cols JSON 往返 bug 已修**：`_normalize_edge`（storage.py，JSON/SQLite 双存储共用）统一解析 cols 字符串；复合列对 save→load 无损（回归测试覆盖）。
2. **edges 表已按 §3 建**：复合主键 `(source_table, target_table, cols, guard)`、guard 归一化 `''` 存储、`INSERT OR REPLACE` 幂等；旧库迁移（`PRAGMA table_info` 检测 from_table → 读旧行 → GraphEdge → DROP+CREATE+INSERT）已实现，备份用 `VACUUM INTO knowledge-{conn}.db.bak`（SQLite 一致性快照，§5 建议的复制式备份的实现变体）。
3. **weight NULL 归一化 1.0**（表定义 DEFAULT 1.0）。
4. 仍待后续：方言层复合 FK 自动检测（需方言适配器携带约束 id，非本文件范围）。
