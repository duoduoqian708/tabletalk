# 实施计划：数据源接入流程 + 知识库重构（DATUM V2 阶段二）

> 依据：《2026-08-19-onboarding-kb-page-design.md》（设计定稿）。本计划只列改动点与顺序。

## 目标

1. 接入流程：测试前置（test-draft 不落盘）→ 通过才落盘 → 构建强制（kb_status 状态机）→ 确认闸 → 未构建卡死（kb_not_built）
2. 知识库构建任务化：后台 job + 进度轮询 + 取消 + 确认
3. VectorStore 分层：Pydantic 契约（VectorChunk/SearchQuery/SearchHit）+ NumpyBackend 默认 + SqliteVec 持久化 + collection/metadata 过滤
4. 前端：接入闸门（草稿 localStorage + 保存禁用）+ 构建弹窗进度 + 知识库合并页（左内容右图 + 状态徽标 + 确认闸）

## 改动清单

### 后端（backend/app/）

| 文件 | 改动 |
|---|---|
| `knowledge/vectorstore.py`（新） | Pydantic 契约 + VectorStore ABC + NumpyBackend（归一化+过滤+top_k） |
| `knowledge/vectors.py` | BatchIndex 保留为 NumpyBackend 内部/构建工具，接口对齐 |
| `knowledge/store.py` | `_vec/_table_vec/_batch/_batch_table` 归一为 per-conn VectorStore；`build` 加进度回调；新增 `confirm_all`/`status`；`retrieve/vector_route_tables` 走 VectorStore.search |
| `knowledge/storage.py` | KbSnapshot 兼容（vec/table_vec 保留）；vec0 加 collection/metadata 列（向后兼容迁移） |
| `core/connections.py` | ConnectionConfig 加 `kb_status`（none/building/pending_review/ready）、`kb_updated_at` |
| `api/connections.py` | 新增 `POST /connections/test-draft`（不落盘；SQLite=文件可读+列表，其余=真实连库） |
| `api/knowledge.py` | `build` → 后台 job 返回 job_id；新增 `build/progress`、`build/cancel`、`confirm-all`、`status` |
| `api/query.py` | 入口检查 `kb_status != ready` → 409 `kb_not_built`（schema/图谱等只读查看除外） |
| `ai/context.py` | 懒构建逻辑替换为：未构建时走任务化入口（或直接 409，由 api/ai 层拦截） |

### 前端（frontend/src/renderer/src/）

| 文件 | 改动 |
|---|---|
| `components/ConnectionModal.tsx` | 保存闸门（测试通过且未改动才可点）；草稿 localStorage（cleared-conn-drafts-v1）；test-draft 流程 |
| `store/connections.ts` | kb_status 透传；草稿读回 |
| `api/knowledge.ts` | 新增 buildJob/progress/cancel/confirmAll/status 调用 |
| `components/KnowledgeReview.tsx` | 大改：左内容区（文档/标签子页+搜索）+ 右图区（GraphCanvas 复用 + FK/overlap 过滤 + 节点浮层编辑） |
| `components/AppLayout.tsx` | kb_not_built 拦截 → 构建弹窗（进度条）；状态徽标入口 |
| `api/types.ts` | kb_status 类型 |

### 测试（backend/tests/）

- 新增：test-draft（含 SQLite 文件判定）、kb_status 状态机（build→progress→confirm→ready）、卡死拦截、VectorStore 过滤/归一化、job 取消
- 更新：knowledge API 相关既有测试（build 返回形状变化）

## 顺序

1. VectorStore 分层（不破坏既有 API，先绿）→ 2. kb_status + 任务化 + 端点 → 3. test-draft + 卡死 → 4. 前端接入流程 → 5. 知识库合并页 → 6. 全量验证 + 推送

## 边界（本轮不做）

- LanceBackend（触发信号未到）；增量更新（设计留位）；加边/删边 UI（下一轮）；企业版存储
