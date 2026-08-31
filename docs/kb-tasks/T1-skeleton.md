# T1 · 工程骨架：模块结构 + 门面拆分（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：无
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

把 `backend/app/knowledge/store.py`（**2192 行、60+ 方法的 god-class**）拆成职责单一的模块 + 门面，**行为不变、对外 API 零改动、全量测试全绿**。

这是用户唯一要求的"工程骨架"落点——骨架立稳后，T2–T10 都是往正确的位置填功能。

## 2. 现状（读码后核实）

`KnowledgeBase`（store.py）混了 6 类职责：持久化/版本、构建编排、图谱、语义（表知识/标签/文档）、检索、状态确认。方法清单见 §5 映射表。

依赖 `state.knowledge` 的外部调用方（**全部不能动**）：
- `app/api/knowledge.py`（build/overview/graph/tags/annotate/confirm/reject/route...）
- `app/ai/context.py`（retrieve/route_tables/vector_route_tables/to_context/expand_tables）
- `app/ai/tools/kb_read.py`、`kb_write.py`、`graph_read.py`、`graph_write.py`
- `app/state.py`（`KnowledgeBase(data_dir, runtime)` 构造）

## 3. 目标模块结构

```
backend/app/knowledge/
  facade.py            # KnowledgeBase 门面：保留全部公开方法签名，委托各模块
  storage.py           # 持久化层（不动，T3 再升级边模型）
  graph/               # L1 结构层
    __init__.py
    store.py           # GraphStore：图状态 + 边读写 + BFS（搬移）
  semantic/            # L2 语义层
    __init__.py
    store.py           # SemanticStore：表知识/标签/文档（搬移）
  retrieval.py         # 检索服务：retrieve/to_context/route_tables（搬移）
  build.py             # 构建编排：build/incremental/sync/embed（搬移）
  behavior/            # L3 行为层（T10 填充，先建空 __init__.py）
    __init__.py
  jobs.py              # BuildJobManager（不动）
  annotator.py         # AI 注释（不动，T4 再迁命名推断）
  ddl_context.py / docs.py / embedding.py / vectorstore.py / vectors.py  # 不动
```

**状态归属**（每个模块持有自己的状态切片，不再集中在 KnowledgeBase 的 20 个 dict）：

| 状态 dict | 归属 |
|---|---|
| `_graph` `_edge_tombstones` `_excluded` `_llm_graph_edges` `_llm_edge_tombstones` | `graph/store.py` |
| `_tables` `_tags` `_table_tags` `_samples` `_schema` `_supplemented` | `semantic/store.py` |
| `_table_vec` `_vstore` `_artifact_fingerprint` | `retrieval.py`（检索/向量） |
| `_schema_fingerprint_map` `_synced_at` `_stash` `_storages` | 门面（持久化/版本协调） |

## 4. 门面设计

```python
class KnowledgeBase:
    """门面：公开方法签名与现有完全一致，内部委托各模块。"""
    def __init__(self, data_dir, runtime=None):
        self._storage_backend = ...
        self.graph = GraphStore(data_dir)          # L1
        self.semantic = SemanticStore(data_dir)    # L2
        self.retrieval = RetrievalService(data_dir)
        self.build = BuildService(data_dir, runtime)
        # 跨模块协调的方法保留在门面（confirm_all / discard_drafts / overview / build 编排...）

    # ---- 委托示例（每类方法一个委托块）----
    def graph(self, conn_id): return self.graph_store.graph(conn_id)
    def expand_tables(self, conn_id, seeds, hops=2): return self.graph_store.expand_tables(conn_id, seeds, hops)
    def retrieve(self, conn_id, query, table=None, k=10): return self.retrieval.retrieve(...)
    def confirm(self, conn_id, table=None, column=None): return self.semantic.confirm(...)
```

**规则**：
- **公开方法名一个都不能少**——`api/knowledge.py`/`context.py`/`tools/*` 调用的每个方法都要在门面上存在
- 跨模块方法（confirm_all 要归档语义 + 图边）由门面编排
- 门面不存业务状态（只存持久化相关 + 协调用）

## 5. 搬移映射表（方法 → 目标模块）

**→ `graph/store.py`（GraphStore）**：

| 方法 | 说明 |
|---|---|
| `_build_graph` / `_tombstone_key` / `_is_tombstoned` / `_llm_edge_key` / `_upsert_llm_edges` | 建图/墓碑 |
| `add_graph_edge` / `remove_graph_edge` / `confirm_graph_edges` / `reject_graph_edges` / `llm_graph_edges` | 边 CRUD |
| `graph` / `excluded_tables` / `set_table_excluded` / `graph_layout` / `set_layout` / `hop_sql` / `_neighbors` / `_neighbors_for` | 图查询 |
| `_fk_adj` / `expand_tables` / `_sync_removed_tables` | BFS 扩展 |

**→ `semantic/store.py`（SemanticStore）**：

| 方法 | 说明 |
|---|---|
| `_from_schema` / `_sync_table_shells` / `_synthesize_table_text` / `_table_payload` | 表知识 |
| `annotate` / `edit_table_knowledge` / `list_docs` / `delete_user_doc` / `table_card` / `table_cards` / `samples` | 文档/表卡 |
| `clear_tags` / `upsert_tags` / `create_tag` / `update_tag` / `assign_table_tags` / `confirm_tag` / `reject_tag` / `tags` / `confirmed_tags` | 标签 |
| `annotate_drafts` / `take_supplemented` / `confirm` / `reject` / `reject_comment` / `pending_counts` / `has_confirmed_content` / `field_history` / `apply_field_history` | 确认/历史 |

**→ `retrieval.py`（RetrievalService）**：

| 方法 | 说明 |
|---|---|
| `retrieve` / `to_context` / `vector_route_tables` / `route_tables` / `_docs` / `overview`(读部分) | 检索/路由 |
| `_vector_store` / `_rebuild_vstore` | 向量索引 |

**→ `build.py`（BuildService）**：

| 方法 | 说明 |
|---|---|
| `build` / `incremental_build` / `sync` / `needs_sync` | 构建编排 |
| `_schema_fingerprint` / `diff_schema` / `diff_is_empty` / `_from_schema_subset` | 指纹/diff |
| `_embedder` / `_log_embedding_usage` / `_emb_fingerprint` / `reembed_if_needed` / `_embed_tables` / `_reembed_tables` | 嵌入 |
| `_stash_snapshot` / `_persist_stash` / `_archive_stash` / `_restore_stash` / `_clear_stash` / `_stash_persisted` / `current_version` | 版本/stash |

**→ 门面保留（跨模块协调）**：`_storage` / `_load_conn` / `_save_conn` / `_storages` / `is_built` / `ensure_loaded` / `synced_at` / `clear` / `confirm_all` / `discard_drafts` / `overview`(汇总)

## 6. 执行步骤（每步都验证）

- [x] **Step 1**：建目录 `graph/`、`semantic/`、`behavior/`，各含 `__init__.py`；建 `retrieval.py`、`build.py`、`facade.py`（空壳）
- [x] **Step 2**：`graph/store.py` —— 把 §5 图中 14 个方法**原样搬移**（复制粘贴，不改逻辑），状态 dict 一并迁入；类构造签名 `GraphStore(data_dir)`
- [x] **Step 3**：`semantic/store.py` —— 搬移 §5 语义组方法，类签名 `SemanticStore(data_dir)`
- [x] **Step 4**：`retrieval.py` / `build.py` —— 搬移对应方法（build 组方法需要的 `_stash` 等仍在门面时，先传引用或留门面协调）
- [x] **Step 5**：`facade.py` —— `KnowledgeBase` 委托：**先写一个自动化检查**：遍历 `api/knowledge.py`、`context.py`、`tools/*` 中对 `state.knowledge.<method>` 的调用点，收集方法名集合，断言门面都有
- [x] **Step 6**：删 `store.py` 中被搬移的方法（保留门面需要的），`state.py` 的 `KnowledgeBase(data_dir, runtime)` 构造兼容
- [x] **Step 7**：全量测试

```
cd backend && .venv/bin/python -m pytest -q
```

预期：全绿（243 个）

## 7. 验收标准（我 review 时逐条检查）

1. `backend/app/knowledge/graph/`、`semantic/`、`behavior/`、`retrieval.py`、`build.py`、`facade.py` 存在
2. `KnowledgeBase` 公开方法覆盖 §5 Step 5 收集的全部调用点（我用 grep 抽查）
3. `state.knowledge` 初始化正常（`app/state.py` 不改或仅构造参数微调）
4. 全量 pytest 通过
5. `store.py` 行数显著下降（不再有 god-class；目标 < 400 行）
6. **行为不变**：`graph()`、`retrieve()`、`build()` 等抽查方法输出与重构前一致

## 8. 完成定义

- [x] 三个子模块目录 + `retrieval.py` + `build.py` 建立
- [x] §5 映射表全部方法搬移完成（行为不变）
- [x] `facade.py` 门面委托 + 公开方法齐全
- [x] 全量测试绿 + 我抽查的调用点方法都在

---

## 参考

- 现状代码：`backend/app/knowledge/store.py`（方法清单）、`app/state.py`（构造）
- 外部调用方：`api/knowledge.py`、`ai/context.py`、`ai/tools/kb_read.py`、`kb_write.py`、`graph_read.py`、`graph_write.py`


## 偏差记录（2026-08-30 修复轮 R8）

1. **§5 映射表调整**：`discard_drafts` 原列「门面保留」，执行时按 R8 验收（facade ≤500 行）下沉至 `BuildService.discard_drafts(facade, conn_id)`，门面留薄委托。
2. **门面下沉完成**：build/incremental_build/sync/reembed_if_needed/_embed_tables/_reembed_tables → `build.py::BuildService`；route_tables/vector_route_tables/overview/rebuild_vstore → `retrieval.py::RetrievalService`；record_query_success/apply_query_log_edges → `behavior/service.py::BehaviorService`（T1 时行为层为空，T10 填充）。
3. **私有别名 hack 已消除**：16 个 `_xxx` 子模块状态引用改为 class-level property（零调用方改动，测试/annotator 全部兼容）。
4. 验收现状：facade.py 498 行（≤500）、store.py 119 行（<400）、全量测试 650+ 绿。

## 偏差记录（2026-08-30 收尾轮 R15）

5. **`_docs` 归属与 §5 映射表不一致**：文档 §5 列 `_docs` → retrieval.py，实际落在 `semantic/store.py:447`（`SemanticStore._docs`，与 list_docs/表卡同域）。行为正确（检索经表级知识卡，文档不进向量），仅归属记录补正。
6. **子模块构造签名**：文档示例 `GraphStore(data_dir)`/`SemanticStore(data_dir)`，实际无参构造（facade.py:41-42 直接实例化），持久化由门面统一协调——偏差记录 #3 的 property 模式同源，此处补记。
