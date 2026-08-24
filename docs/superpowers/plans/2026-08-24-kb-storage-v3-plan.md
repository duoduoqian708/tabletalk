# KB 存储与工作台重构 v3 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec `docs/superpowers/specs/2026-08-24-kb-storage-table-chunk-v3-design.md` 落地：按表组织的知识库存储 v2、一表一 chunk 向量合一、边模型 v2（字段级端点+基数）、三列工作台 + 可复用 2D 关系图组件。

**Architecture:** 后端先立新模型与流水线并拆旧枚举体系（T1），再合一向量与检索层（T2）；前端先绑新数据模型改三列（T3），再做 2D 图组件（T4）与双宿主组装（T5），最后回归收尾（T6）。每任务独立提交，子代理实现+双审。

**Tech Stack:** FastAPI + sqlite 快照 + 手写 numpy 向量存储；React18+zustand TS，手写 SVG 图组件。

**约定：** pytest 在 backend/ 下 `.venv/bin/python -m pytest -q`；前端 typecheck/build/i18n:check 在 frontend/ 下。分支 feature/kb-build-flow 直接续做。设计依据一律指向 v3 spec，实施者动手前必须通读 spec 与本计划对应任务。

---

### Task 1: 存储模型 v2 + 生成流水线简化（后端）

**Files:**
- Rewrite: `backend/app/knowledge/store.py`（KnowledgeBase 核心）
- Modify: `backend/app/knowledge/storage.py`（KbSnapshot v2）、`backend/app/knowledge/jobs.py`、`backend/app/knowledge/annotator.py`、`backend/app/api/knowledge.py`
- Test: 改造 `tests/knowledge/` 相关用例

- [ ] **Step 0 侦察**：通读 store.py 全文、storage.py KbSnapshot/SqliteStorage/JsonStorage、annotator.annotate_table/_mock_table_comments_from_ddl、api/knowledge.py 枚举端点；列出全部将被删除符号清单
- [ ] **Step 1 定义新模型**：store.py 内 `ColumnInfo`/`TableKnowledge` dataclass（spec §2 字段）；连接态 `_tables: dict[conn, dict[name, TableKnowledge]]`
- [ ] **Step 2 快照 v2**：storage.py snapshot 增加 `version=2` 与 tables 序列化；读到 version<2 → 返回空快照 + logger.warning("旧工件作废，请重新构建")
- [ ] **Step 3 流水线改造**：
  - annotate_table 返回 items 增加可选 `values`（LLM JSON 字段）与 `example`（从样本取首非空截断60）；prompt 按 spec §3 更新（低基数离散列附取值对照）
  - build 阶段一落库：items 写入 TableKnowledge.columns（draft 态）；删第四阶段调用块
  - incremental_build 同步适配（变化表重注释）
  - jobs.PHASES 回三条；BuildJobManager.start 的 include_samples 过滤逻辑随之移除
- [ ] **Step 4 拆旧枚举体系**：删 store 的 `_enums` 全部方法与 pending_counts/confirm_all/overview 中枚举分量；删 annotator 的 annotate_enums_core/annotate_enums/_parse_enum_items/_mock_enums/_distinct_enum_values；删 api 四个枚举端点与请求模型
- [ ] **Step 5 测试改造**：test_build_enum_phase → 断言"授权构建后 ColumnInfo.values 非空 / 未授权为空"；test_enum_docs 删除；test_store/test_incremental 中枚举引用清理；全量 PASS
- [ ] **Step 6 Commit**: `feat(kb)!: 存储模型v2——按表组织，枚举并入逐表注释`

### Task 2: 一表一 chunk 向量合一 + 边 v2 + 检索适配（后端）

**Files:**
- Modify: store.py（向量化/检索/route）、`backend/app/knowledge/vectorstore.py`、`backend/app/ai/tools/kb_read.py`
- Test: tests/knowledge 新增 chunk 合成与检索用例

- [ ] **Step 1 合成函数**：`_synthesize_table_text(tk: TableKnowledge) -> str`（spec §4 格式：confirmed 优先、字段行含 pk/fk 标记/可选值/示例）；`_table_payload(tk) -> dict`（ddl/tags/layout/draft_count/updated_at）
- [ ] **Step 2 向量合一**：`_embed_docs`/`_embed_table_docs` 收敛为 `_embed_tables(conn)`——每表一条 VectorChunk(id=`tbl-{name}`, text, payload)；删除 doc_vec 碎片路径与 `_table_vec` 第二体系；`_rebuild_vstore` 单体系重建
- [ ] **Step 3 检索适配**：`retrieve(conn,q,k)` 返回命中表知识卡（含 payload 过滤）；`to_context` 输出【知识库】段为完整表描述卡；`route_tables`/`vector_route_tables` 用同一 chunk 集；kb_read 工具返回结构适配
- [ ] **Step 4 边 v2**：edges 存储增加 cardinality/reason 字段；FK 边自动推导方向+基数（pk 判 1:1）；annotator._generate_candidate_pairs/annotate_graph prompt 强制四元组+cardinality+多侧校验；墓碑键=字段对
- [ ] **Step 5 测试**：合成文本格式断言、payload 断言、FK 方向推导用例、LLM 解析四元组用例；全量 PASS
- [ ] **Step 6 Commit**: `feat(kb): 一表一chunk向量合一+GraphEdgeV2字段级基数边`

### Task 3: 前端 types + 三列工作台改版

**Files:**
- Modify: `frontend/src/renderer/src/api/types.ts`、api/knowledge.ts、store/knowledge.ts、components/KnowledgeReview.tsx、KbReviewModal.tsx、locales/*
- [ ] **Step 1 types 重写**：KbOverview→新结构（tables: TableKnowledgeView[]，含 columns/values/example/status/ddl/draft_count）
- [ ] **Step 2 中列内容块**：审查页/KbReviewModal 表卡按新模型渲染（字段行含可选值/示例/PK/FK 徽标/状态；逐列 ✓✕ 语义=整列确认/撤下）
- [ ] **Step 3 API/store 对齐**：overview/status/confirm/reject/save 的参数与返回适配；删枚举残留调用
- [ ] **Step 4 i18n 清理**（enum 卡等孤儿 key）+ typecheck/build/i18n:check
- [ ] **Step 5 Commit**: `feat(ui): 工作台绑定存储v2按表内容块`

### Task 4: TableRelationGraph2D 组件（前端）

**Files:**
- Create: `frontend/src/renderer/src/components/TableRelationGraph2D.tsx` + styles
- [ ] **Step 1 布局**：领域聚类初始布局（同标签表聚拢，网格分区）+ 拖拽（坐标写回 payload.layout 经 store action 持久化）
- [ ] **Step 2 边渲染**：SVG 直线/轻曲线；中点标签 `from.col ─n:1─ to.col`；n:1 实线 / 1:1 双短线徽标；draft 虚线
- [ ] **Step 3 连线交互**：拖线 A→B 弹面板（两端字段下拉+基数默认 n:1）创建 user 边；点选边可编辑/删除（走现有 edges API）
- [ ] **Step 4 typecheck/build + Commit**: `feat(ui): TableRelationGraph2D 领域聚类可编辑关系图`

### Task 5: 双宿主组装

**Files:**
- Modify: KbReviewModal.tsx（右栏列表→TableRelationGraph2D）、AppLayout/ModulePages（知识库编辑页新增 2D 形态切换，与 Graph3D 共存）
- [ ] **Step 1 审阅弹窗右栏接入**；confirm-all 流程回归
- [ ] **Step 2 编辑页 2D 切换**；布局持久化联动验证
- [ ] **Step 3 typecheck/build + Commit**: `feat(ui): 2D关系图双宿主组装（审阅右栏+编辑页）`

### Task 6: 回归收尾

- [ ] 后端全量 pytest；前端 typecheck/build/i18n:check
- [ ] sidecar 重启 + health；手动验收 spec §8 六条（重点：授权/未授权 chunk 内容差异、2D 图增删拖拽、审计留痕）
- [ ] 最终整体复审（子代理）→ Ready to merge
