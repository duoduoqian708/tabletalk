# 实施计划：知识库增量更新（结构同步）

依据：`2026-08-19-onboarding-kb-page-design.md` §5（已定稿）。

## 目标

- schema 指纹 + diff：周期对比，只增量更新变化部分（文档/图/向量），不重置确认闸
- 墓碑机制：用户删除的值重叠边不复活（存储+遵守，UI 下一轮）
- 周期任务（`kb_sync_minutes`，默认 30）+ 手动「检查更新」端点
- 前端：知识库页「检查更新」按钮 + 上次同步时间 + diff 摘要

## 改动清单

### 后端（backend/app/）

| 文件 | 改动 |
|---|---|
| `knowledge/docs.py` | KnowledgeDoc 加 `archived: bool=False`（删除表标记，检索/图谱过滤） |
| `knowledge/storage.py` | KbSnapshot 加 `schema_fingerprint`、`edge_tombstones`、`synced_at`；SQLite meta 表读写；旧 artifact 兼容 |
| `knowledge/store.py` | `_schema_fingerprint`（sha1 规范化 schema）；`diff_schema(old,new)`；`incremental_build`（加表/改列/删表/FK/overlap 局部重算）；`sync(conn_id, schema, samples)` 高层（指纹→diff→增量→更新指纹+synced_at→落盘）；`_build_graph` 遵守墓碑；overview/retrieve/graph 过滤 archived |
| `knowledge/jobs.py` | SyncLoop：周期任务（kb_sync_minutes），遍历 ready 连接，指纹对比，增量；互斥（构建中跳过） |
| `core/settings.py` | RuntimeSettings 加 `kb_sync_minutes: int = 30`（0=关） |
| `api/knowledge.py` | `POST /{id}/sync`（手动增量，返回 diff）；status/overview 返回 `synced_at` |
| `main.py` | lifespan 启动/停止 sync 循环 |

### 前端（frontend/src/renderer/src/）

| 文件 | 改动 |
|---|---|
| `api/knowledge.ts` | `sync()`；overview 类型加 `synced_at` |
| `components/KnowledgeReview.tsx` | 顶栏「检查更新」按钮 + 上次同步时间 + 同步后 diff 摘要提示 |

### 测试（backend/tests/）

- 新 `tests/knowledge/test_incremental.py`：指纹稳定、diff 各类型、增量构建（加表/改注释/删表/FK 变化）、墓碑、archived 过滤、sync 幂等（无变化零副作用）、确认闸不重置

## 顺序

1. docs.py + storage.py（数据模型）→ 2. store.py（指纹/diff/增量/墓碑）→ 3. jobs.py + settings + api + main（触发）→ 4. 测试 → 5. 前端 → 6. 全量验证 + 推送
