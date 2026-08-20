# 07 · 知识库架构（向量库 + 图谱）

> 知识库是产品的复利资产：结构注释、领域标签、FK/值重叠图谱、向量索引的"AI 起草 -> 人工确认"循环。
> 本文记录**已实现的架构事实**（2026-08 对照 `backend/app/knowledge/` 源码核实），以及演进守则。
> 原则（01 §6）：不堆重型 RAG、不上外部向量数据库--例外触发条件见 §8。

## 1. 分层总览

```
app/knowledge/
├─ store.py        KnowledgeBase 编排：构建/增量同步/混合检索/路由/标注确认（1291 行，核心）
├─ storage.py      KbStorage 协议 + JsonStorage / SqliteStorage（持久化）
├─ embedding.py    Embedder 协议 + HashingEmbedder(离线) / ApiEmbedder(OpenAI 兼容)
├─ vectors.py      BatchIndex：numpy 批量余弦（内存索引）
├─ vectorstore.py  VectorStore 业务接口 + NumpyVectorStore 默认实现
├─ annotator.py    AI 草案生成（注释/标签/枚举）
├─ jobs.py         构建任务（异步进度）
└─ docs.py         KnowledgeDoc 模型
```

数据流向（检索时）：`Embedder(query)` -> `VectorStore.scores_all` 与关键词分、图谱分融合 -> `KnowledgeDoc` 列表 -> `to_context()` 拼上下文（**结构+标注，无行数据**）。

## 2. 数据模型

- **KnowledgeDoc**：id / kind / title / body / table / column / status / source / tags。
  - `status` 状态机：`auto`（构建生成）与 `user`（手写）均经 confirm 后为 `confirmed`；检索加权 confirmed 优先（§6）。
  - draft 不影响任何决策行为：注释草案不进加权、标签 draft 不路由（§7 不变式）。
- **标签**：`tags`（name/description/status）+ `table_tags`（表 -> 标签集合），每库一套。
- **枚举注释**：enum 值 -> 含义（draft -> confirm / 手写 save）。
- **墓碑（tombstone）**：用户删除的值重叠边按 `(from, from_col, to, to_col)` 记录，重建时不再生成；`excluded_tables` 排除整表。

## 3. 存储层（KbStorage 协议）

- **默认 SqliteStorage**：每连接一个 `data_dir/knowledge-{conn_id}.db`（WAL 模式）。表：`docs / edges / tags / table_tags / embeddings / table_embeddings / meta`；meta 存 schema 快照、samples、指纹、墓碑等。
- **sqlite-vec 探测**：可用时建 `doc_vec / table_vec` vec0 虚表（ANN 备用）。**注意坑**：vec0 列固定 `float[256]`，`_pad256` 会把超过 256 维的向量**截断**（API 嵌入如 bge-m3 是 1024 维）--运行时检索不走这条路（走内存全维索引），vec0 仅供未来 SQL 级查询/服务化使用；真用 ANN 时需按实际维度重建虚表。
- **JsonStorage**：遗留格式（`knowledge-{conn_id}.json`），SQLite 首次加载自动迁移；`TABLETALK_KB_STORAGE=json` 可强制。
- **选择器**：`make_storage(data_dir, conn_id, backend)`，业务层只见协议。

## 4. 向量层

- **Embedder 协议**（async）：
  - `HashingEmbedder`（默认，离线）：字符 uni/bi-gram CRC32 哈希到 **256 维**，L2 归一化。只表达"表面重叠"（中文"退货"命中"退货率"），跨语言语义桥接做不到。
  - `ApiEmbedder`：任意 OpenAI 兼容 `/embeddings` 端点（Ollama/vLLM/bge-m3 网关/云端），运行时设置 `TABLETALK_EMBEDDING_PROVIDER=api` + URL/MODEL/KEY。本地端点不出内网。
- **指纹机制**：嵌入配置变化 -> `emb_fingerprint` 不匹配 -> `reembed_if_needed()` 全量重嵌（否则维度错位）。
- **BatchIndex**（`vectors.py`）：numpy 矩阵乘批量余弦，`top_k` 用 argpartition O(N)。实测基准（代码注释）：5 万条 × 2048 维 ≈ 20-50ms/查询（纯 Python 逐条 6.6s，不可接受）；numpy 缺失自动回退纯 Python。
- **VectorStore 业务接口**（`vectorstore.py`）：`set_chunks / scores_all / search`，chunk 带 `collection`（doc/table/enum/intent/alias…）+ `metadata` 过滤。**换 LanceDB/pgvector/Qdrant = 新增一个实现类，业务代码零改动**--这是"不上外部向量库"决策的换装点。

## 5. 图谱层

`_build_graph()` 产两类边（`edges` 表）：

1. **FK 边**（kind=fk, weight=1.0）：schema 外键，结构事实。
2. **值重叠边**（kind=overlap）：对每张表抽样列值（构建时的 samples），两列样本值有交集 -> 疑似 join 路径（无 FK 也能连）。`weight = |交集| / min(|A|,|B|)`。**启发式去噪**：同名 `id ↔ id` 跳过（自增主键天然同范围，重叠无意义）；用户删过的边（墓碑）跳过。

图操作：`expand_tables(seeds, hops)` 沿 FK 边 k 跳扩展（默认 2 跳）；`storage.hop_sql()` 提供等价的递归 CTE SQL（供审计/服务化/企业版复用）。

## 6. 混合检索（retrieve 的分数公式，改动须同步本文）

```
score(doc) = kw_score + 2.0 × cos(query_vec, doc_vec) + 1.5 × [status=confirmed] + 0.35 × best_neighbor_score
```

- `kw_score`：分词 token 命中 title/body/tags 各 +1；整句包含 +1；指定表参数时表名精确命中 +3 / 出现 +2。
- 图谱传播：相邻表（FK+重叠边）的最高分 × 0.35 加给本表文档（"邻居被检索命中 -> 本表相关"）。
- 全部得分为 0 时回退全量文档序（保证不空手而归）。
- `to_context()`：top-k 拼 `【知识库】` 文本，草案带"（AI 草案，待确认）"标记。

## 7. 标签路由（NL 查询圈表范围的核心）

```
用户问题 -> intent.classify_tags（LLM 判定 / mock 关键词回退）
         -> route_tables(tag_names, hops=2)
         -> 打了这些标签的表 + FK 2 跳扩展 = 候选表集合
         -> 候选表之间的边 = 候选子图（随上下文发给模型）
```

**不变式：路由只认 confirmed 标签**--AI 刚起草的标签不影响路由，直到人工确认（`route_tables` 显式过滤 `confirmed_tags`）。价值：避免模型在全库 schema 里瞎扫，准确圈定表范围。

## 8. 演进守则

- **不引入外部向量数据库**（01 反目标）。换装条件：文档量 > 10 万 chunk 或单查询 > 100ms 时，先试 sqlite-vec（已探测、接口已留），再考虑独立 ANN（FAISS/LanceDB）；一律实现 `VectorStore` 接口，业务零改动。
- **B3 敏感度标签**挂进现有 tags 体系（新增一类标签，走同一 draft->confirm 流程）；代号化在 `ai/context.py` 组装时做，KB 内部仍存真名。
- **C6 问题库独立于 KB**：存 `app/core/questions.py`（`data_dir`），不复用 KnowledgeDoc--问题库是"问答对"，KB 是"结构语义"，混在一起会让确认流和检索都变复杂。
- **增量同步**已具备（schema 指纹 + `diff_schema` + `incremental_build` + `needs_sync`）：schema 变更后只重嵌受影响表，勿退回全量重建。
- 检索公式（§6）与路由不变式（§7）的任何改动：先改本文，再改代码，e2e 用 demo 库 14 表验证路由正确性（表集合与预期 FK 子图一致）。

## 9. 相关技术债

| # | 债 | 处理时机 |
|---|-----|----------|
| 1 | `app/debuglog.py` 调试模块（"TODO: 测试后删除"）在 main.py / store.py / tools/sql.py / report.py / loop.py / api/query.py 共 7 处引用残留 | Phase 0 清理：删除模块与全部引用，跑全量 pytest 确认 |
| 2 | vec0 虚表固定 256 维会截断 API 嵌入（§3） | 启用 SQL 级 ANN 前按实际维度重建；当前运行时路径不受影响 |
| 3 | CLAUDE.md 仍写"persist 到 knowledge-{conn_id}.json"（已演进为 SQLite 默认 + JSON 迁移） | Phase 0 顺手更新 CLAUDE.md，保持"代码现状"文档准确 |
