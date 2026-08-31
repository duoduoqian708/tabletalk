# T2 · 统一图谱：合并两套图（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T1
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

消除双图：当前系统有**两套互不同步的图**，合并为一套，`graph_read`/`graph_write` 工具指向知识库唯一的图。

## 2. 现状（读码后核实）

| 图 | 位置 | 内容 | 谁在用 |
|---|---|---|---|
| 知识库图 | `knowledge/store.py`（T1 后迁入 `knowledge/graph/store.py`） | FK + LLM draft + 用户手绘边，存 `knowledge-{conn}.db` | 构建流程、`context.py` 路由 |
| 独立图 | `ai/graph/store.py::GraphStore`（138 行） | **只有 FK 边**（`auto_build_from_schema`），存 `graph-{conn}.db` | **`graph_read`/`graph_write` 工具** |

问题：`graph_read` 工具永远读不到知识库那套图里的 LLM/用户边；两套图各建各的、各存各的。

`ai/graph/store.py` 现状接口（将被替换）：

```python
class GraphStore:
    def __init__(self, data_dir): ...
    def add_edge(self, conn_id, source, target, relation="related", source_col=None, target_col=None)
    def remove_edge(self, conn_id, source, target, relation=None)
    def get_neighbors(self, conn_id, table_name, hops=2)   # N 跳 BFS（按表去重）
    def list_edges(self, conn_id, table_name=None)
    def auto_build_from_schema(self, conn_id, schema)      # 只画 FK 边
```

工具调用点：`ai/tools/graph_read.py`（`_graph_read` 用 `GraphStore(get_env().data_dir)` + `get_neighbors`/`list_edges`）、`ai/tools/graph_write.py`（用 `add_edge`/`remove_edge`）。

## 3. 方案

1. **知识库图成为唯一图**（T1 后位于 `knowledge/graph/store.py`）
2. `graph_read.py`/`graph_write.py` 改调 `state.knowledge` 的图接口（保留原工具参数语义）
3. `ai/graph/store.py` 删除；`graph-{conn}.db` 数据**不迁移**（FK 边可由 schema 重建）
4. `GraphStore.auto_build_from_schema` 职责并入 T4 的建边管线，此处不实现

## 4. 接口对齐（工具参数 → 知识库图接口）

| graph_read/graph_write 参数 | 映射到知识库图 |
|---|---|
| `table_name` + `hops`（graph_read） | `state.knowledge.expand_tables(conn_id, {table_name}, hops)` + 取相关边（T6 后换 `path_strings`） |
| 无参数（graph_read，全图） | `state.knowledge.graph(conn_id)` |
| `add_edge(from,to,relation,from_col,to_col)`（graph_write） | `state.knowledge.add_graph_edge(conn_id, from, to, kind=relation, from_col, to_col)`（注意 T3 后签名升级） |
| `remove_edge(from,to,relation)` | `state.knowledge.remove_graph_edge(conn_id, from, to, kind=relation)` |

> **T3 依赖说明**：T2 只做"工具改指向"，工具内部参数映射用现有接口先对上；T3 边模型升级后再调整入参结构（cols/guard），届时我给出升级后的映射。

## 5. 测试设计（tests/knowledge/test_graph_unify.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_graph_read_uses_kb_graph` | 构建 demo 库知识库（有 FK 边），调 graph_read（无参数） | 返回的边与 `state.knowledge.graph()` 一致 |
| `test_graph_write_visible_in_kb` | graph_write 加一条 user 边 | `state.knowledge.graph()` 里能看到；graph_read 也能读到 |
| `test_no_separate_graph_db` | 调用后检查 data_dir | `graph-{conn}.db` 不再被创建 |

## 6. 执行步骤

- [x] 1. 改 `graph_read.py`/`graph_write.py`：去掉 `from app.ai.graph.store import GraphStore`，改调 `state.knowledge`（保持工具返回值结构兼容现有调用方）
- [x] 2. 删除 `backend/app/ai/graph/` 目录
- [x] 3. 检查 `ai/tools/__init__.py`、`ai/agent/dispatcher.py` 等无残留 import
- [x] 4. 跑新测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 7. 验收标准（我 review 时逐条检查）

1. `app/ai/graph/` 目录已删，全仓库无 `GraphStore`（`ai/graph`）残留 import
2. `graph_read`/`graph_write` 与知识库图同一存储——graph_write 加的边 graph_read 可见
3. 新测试通过 + 全量回归通过
4. `graph-{conn}.db` 不再产生

## 8. 完成定义

- [x] graph_read/graph_write 改指向 `state.knowledge`
- [x] `ai/graph/` 删除、无残留 import
- [x] 3 个新测试全绿 + 全量回归绿

---

## 参考

- 现状代码：`ai/tools/graph_read.py`、`ai/tools/graph_write.py`、`ai/graph/store.py`
- 目标接口：T1 后 `knowledge/graph/store.py`（GraphStore）


## 偏差记录（2026-08-30 修复轮）

无偏差：审查（通过）确认 ai/graph 已删、零残留 import、graph_read/write 指向知识库唯一图。
