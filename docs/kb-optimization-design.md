# 知识库构建优化设计（2026-09 走查）

> 日期：2026-09-07 · 状态：已实施（五阶段全部落地，197 后端测试 + typecheck/build 通过）· 定位：本轮代码走查产出的优化方案与决策记录
> 依据：`docs/knowledge-and-engine-design.md`（原架构蓝图）+ 对 `backend/app/knowledge/`（build/annotator/jobs/semantic/storage/api）与前端知识库页（KnowledgeReview/ReviewPage/KbBuildGate 等）的走查
> 执行顺序约束：五阶段串行，每阶段跑完对应回归再进下一阶段。

---

## 0. 文档定位

知识库的"提案 → 人工确认 → 生效"核心契约与状态机（`none → building → pending_review → ready`）保持不变。本设计只解决走查发现的三类问题：

1. **审核疲劳**：全量重构全量重提案、提案措辞漂移、收尾路径双轨；
2. **流程断裂**：构建完成无引导、增量同步无进度、审核信号倒挂（完成时零信号 / 待审时四个信号）；
3. **质量与健壮性**：增量产物知识种类不全、标签跨轮漂移、逐表失败不可见、审核对比层重启即丢。

---

## 1. 走查诊断（现状问题清单）

### 1.1 审核疲劳

- **全量重构 = 全量重新提案**：`build()` 里 `clear_round_proposals(全部)` + `annotate_tables(全部表)`。哪怕 schema 未变，全部表重跑 AI、全部产生新提案；LLM 输出不稳定 → 对比层充满措辞噪音，"改了什么"被淹没。
- **`confirm_all` 后全表重嵌**：`_embed_tables(conn_id, None)`，500 表即 500 次嵌入调用，实际内容变化通常只有几张表。
- **增量同步采样全库**（`SyncLoop.tick` / `sync_kb` 对全部表 `sample_values`），但只有 touched 表需要样本。

### 1.2 流程断裂与双轨并存

- **审核是平行世界**：`KnowledgeReview.tsx`（浏览态，含行内 ✓/✗、kb-cmp 对比弹窗、顶栏 confirm-all）与 `ReviewPage.tsx`（ReviewOverlay，radio + batch-review + finalize）复刻两套三列布局；对比 UI 共三套样式。
- **收尾双轨**：顶栏「确认启用」走 `/confirm-all`；Overlay「应用」走 `/batch-review` + `/review/finalize`。同一 pending 状态两个终点。
- **裁决语义混用**：radio 与行内操作即时生效（拒绝不可撤销），「应用」又按"不操作=全采纳"批量收尾——"我的决定何时算数"无稳定心智。
- **增量同步裸奔**：同步阻塞 POST、无 busy、可重复点击、无进度无取消，与 init/rebuild 的 SSE 三阶段进度形成落差。
- **信号倒挂**：构建完成浮卡悄悄消失（无"去审核"CTA）；pending_review 期间同屏四个提醒（进页弹窗每次都弹 + banner + 顶栏按钮 + 审核 tab）。
- **审核对比层是内存态**：`snapshot_round_baseline` 不落盘（KbSnapshot 无 round 字段），构建后重启 → 审核页"旧版"侧全空。

### 1.3 输出内容质量

- **领域划分跨轮漂移**：全量划分不参考既有 confirmed 域 → 每次重构域名/粒度重掷骰子 → 标签 churn。增量吸收路径（`_annotate_domain_incremental`）反而有锚点，设计不对称。
- **增量产物知识种类不全**：`annotate_filters` / `annotate_constants` / `ingest_values_candidates` 仅全量构建运行；靠增量长大的库缺三块知识。
- **逐表失败静默**：单表 LLM 失败 → 零提案，审核页无法区分"AI 无话可说"与"调用失败"，无单表重试入口（`degraded_phases` 只有整阶段粒度）。
- **上下文随表数平方增长**：逐表注释 prompt 的 `db_tables` 每表附带全库表清单；领域划分/关系识别单轮打包全库画像，大库质量与 token 双降。
- 图谱已砍第二轮验证（低置信边直接进 draft），质量闸门后移到人工——维持现状，不在本期放大。

### 1.4 工程细节

`kb_sync_minutes` 不在 settings update 白名单（运行时改不了）；构建路径 `_kb_schema` 走 30s 缓存（DDL 刚改完可能建在旧结构上）；`KbBuildGate` 存在不可达的 pending_review 死分支；中栏「搜索」按钮无 onClick；边审核按钮 UI 是"每条边"但生效粒度是 `from_table`；Overlay「放弃本轮」无二次确认；历史抽屉靠解析审计 SQL 注释字符串还原动作。

---

## 2. 设计决策记录（本会话拍板）

| 决策点 | 结论 |
|---|---|
| 优化范围 | P0-1（差异提案+缓存）、P0-2/3（入口收敛+sync 任务化）、P1（质量包）、P2（健壮性）全做 |
| 全量重构语义 | 做成开关，用户在重构弹窗显式选择（三档，见 §3.3） |
| 标签锚点 | 三档方案：锚点作为参考而非服从，AI 可提议改名/合并并在审核层批准映射 |
| 前端调整幅度 | A+B：审核镜头化 + 裁决暂存制；C 档（详情面板拆 tab、URL 路由）不做 |
| 嵌入模型硬门槛 | 不动：构建仍强制对话+嵌入双模型配置 |
| sync 后状态流转 | **修复现网缺口**：当前 sync 产出提案后 `kb_status` 永远停留 ready（全库无 `set_kb_status("pending_review")` 调用于 sync 路径），前端审核入口全部以 `pending_review` 为开关 → 提案隐身。决策：sync 完成有提案 → 转 pending_review |
| diff 档标签阶段 | 全量划分带锚点（非轻量增量吸收）：审核镜头标签对比区在 diff 档可用，标签体系持续可演化 |
| 增量概念候选 | `ingest_values_candidates` 读取链改为 `confirmed values → proposed_values`：新表当轮即产概念候选（产出仍为 draft，人工闸不变） |
| 老旧代码 | 实施原则：该删就删，不堆叠修补（ReviewOverlay 整体删除、KbBuildGate 死分支清理、confirm-all 前端入口退役等） |

---

## 3. 核心机制设计

### 3.1 注释缓存（内容寻址）

LLM 注释按输入内容哈希缓存，同输入零成本、零漂移——它同时让"全量重新注释"变便宜、让 diff 模式的未变表提案措辞不漂。

- **键**：`input_hash = sha1(table_ddl + 规范化samples + PROMPT_VERSION + model + 有/无采样)`。
  - `PROMPT_VERSION` 为 `annotator.py` 新增常量，prompt 模板变更时手动 bump（防旧缓存污染新输出）。
- **存储**：`KbSnapshot` 加字段 `annotation_cache: dict[str, dict]`（表名 → `{input_hash, items, created_at}`）；SQLite 后端加表 `annotation_cache(table_name TEXT PRIMARY KEY, input_hash TEXT, items_json TEXT, created_at TEXT)`；JSON 后端随快照落盘。加字段属增量变更，`KB_SNAPSHOT_VERSION` 保持 2。
- **读写点**：`annotate_table()` 入口查缓存，命中直接返回 items（跳过 LLM）；未命中走 LLM，成功后写缓存。cache get/set 由 facade 注入，annotator 保持无状态。缓存只在 LLM 成功后写入——失败表无缓存条目，重试自然重新调 LLM。
- **可观测**：`annotate_tables` 日志区分 `cached/llm` 计数。

### 3.2 差异提案（annotate_mode="diff"）

全量重构默认只对变化表重新提案；未变表已确认知识原样保留、不产提案。

- `build()` 加参数 `annotate_mode: "diff" | "full"`（默认 diff；init 首建无旧 schema，touched=全部表，两模式天然等价）。
- **diff 模式**：构建开头用 `diff_schema(old, new)` + `match_renames` 算 touched 集合（复用 incremental 的 rename 继承逻辑）；`clear_round_proposals(conn_id, touched)` 只清变化表；`annotate_tables` 只跑 touched 表的 ddl_map；round diff 用真实 diff（含 renamed）。
- **full 模式**：维持现状（清全部提案 + 全表重注释），但同样走缓存。
- tags/graph/filters/constants 阶段两模式都全量跑（单轮便宜调用，产出走版本制对比）。

### 3.3 重构三档与标签锚点（tag_mode）

**取舍论证**：标签是路由资产（`route_tables` 只认 confirmed 标签）且承载人工沉淀（审核勾选、手动绑定、手建标签）；从零划分让域名措辞随机漂移，人工资产失去锚点，审核对比区充满"换名字的假变更"。但锚点也有代价——第一版划分若烂，"尽量沿用"会让错误自我延续；结构大改时旧域可能根本覆盖不了。**修正方案是锚点照给、决策权放回审核层**：AI 负责"参考而非服从 + 提出映射"，人负责批准映射；彻底不满时保留从零出口。

- `build()` 加参数 `tag_mode: "keep" | "anchor" | "fresh"`：
  - **keep**（智能差异档默认）：领域划分 prompt 注入既有 confirmed 域（名称+描述+成员表），优先沿用；
  - **anchor**：同上，且 prompt 明示"现有划分不合理时可提出合并/拆分/改名并说明理由"，输出条目可带 `renamed_from` / `merged_from`；审核镜头标签对比区展示映射（"订单管理（原：订单数据）"），一键采纳；
  - **fresh**：`clear_tags` 后从零划分（`_normalize_domains` 程序兜底逻辑不变）。
- **重构弹窗三档**（`KbBuildConfirmDialog`，trigger=rebuild 时显示；init 不显示）：
  1. 智能差异（推荐）——只更新变化表，标签沿用；
  2. 全量重新注释 · 参考现有标签——全部表重提案（走缓存），结构大改时用；
  3. 全量重新注释 · 标签从零——对现状彻底不满时用。
  前两档对应 `annotate_mode=diff/full` + `tag_mode=keep/anchor`；第三档 `full + fresh`。

### 3.4 sync 任务化

增量同步从裸 POST 改为后台 job，与构建共用进度通道。

- `BuildJobManager.start()` 加 `kind: "build" | "sync"`；sync 用简化阶段集（结构探测 → 抽样 → AI 注释 → 图谱标签 → 落盘），复用 `/build/progress` + `/build/events`（前端零新端点）。
- 抽样移入 job 内部且**只抽 touched 表**（先 diff 再采样）。
- `POST /sync`：保留同步快速路径（`needs_sync` 为假直接返回"结构无变化"）；有变化则启动 job 返回句柄。
- `SyncLoop` 周期同步同走 job：失败保持 ready 静默（不破坏可用库）；成功且产出提案 → `kb_status=pending_review`（用户下次进知识库看到审核入口）。
- `run_build_job` 按 kind 分流状态机：build 成功 → pending_review；sync 成功 → pending_review（有提案）/ ready（无提案）；sync 失败/取消 → ready。
- **现网缺口修复**：当前 sync 产出提案后不置 pending_review，前端审核入口（进页弹窗/审查横条/审核 tab 均以 `kb_status==='pending_review'` 为开关）全部隐身——本节实施即修复。

### 3.5 审核镜头化（前端 B 档核心）

**设计原则：审核不是独立页面，是知识库主页在 pending_review 下的一个镜头。**

- **删除 `ReviewPage.tsx`（ReviewOverlay 整个组件）**；`KnowledgeReview.tsx` 在 `kb_status=pending_review` 时进入审核镜头：
  - 表行出现 new/del/mod 徽章（数据源 = round.diff）；
  - 详情面板提案**内联**：当前值 vs 提案并排，逐项接受/拒绝；
  - 左栏顶部出现标签集合对比区（新集合 checkbox ∪ 旧集合 checkbox，展示 renamed_from 映射）；
  - 图库 tab 的 draft 边审核卡片并入同一镜头。
- **裁决全暂存制**：所有接受/拒绝只改本地 decisions（再点一次=撤销裁决）；底部常驻裁决栏："已裁决 X / 待处理 Y / 其余默认采纳新版 — [应用] [放弃本轮]"。
  - 「应用」= `/batch-review`（未裁决表全 confirm）+ `/review/finalize`——**唯一收尾路径**；
  - 「放弃本轮」带二次确认（补齐顶栏版的行为）；`/confirm-all` API 保留（测试/兼容），前端入口退役。
- 后端 API 均为现成能力，本阶段后端不动。

### 3.6 前端一致性修补（A 档，随镜头化顺势覆盖）

- 构建完成 → `KbBuildGate` 浮卡变"构建完成，去审核"CTA（替代悄悄消失）；pending_review 死分支转正为该 CTA 的实现载体；
- 「未构建」判定统一用 `kb_status`（不再混用 `overview.built`）；
- sync 接进度浮卡（复用构建通道，小一号）+ busy + 可取消；
- 进页弹窗每轮只弹一次（conn + synced_at 签名记忆）；banner 保留为唯一常驻提醒，顶栏批量按钮随镜头化移除；
- 边审核文案表达真实粒度："接受该表全部 N 条边"；
- 修掉无 onClick 的搜索按钮；历史抽屉改读审计结构化字段（`trigger`/`added_tables` 等），不再解析 SQL 注释字符串。

### 3.7 内容质量增强（P1）

- **增量补齐 AI 阶段**：`incremental_build` 中 rebuild_tables 非空时补跑 `annotate_filters`（schema 子集）、`annotate_constants`、`ingest_values_candidates`。其中概念候选读取链改为 `confirmed values → proposed_values`（新表当轮即产候选，产出仍为 draft）。
- **逐表失败可见 + 单表重试**：`annotate_tables` 收集 `failed_tables` → build stats → `set_round(failed_tables=...)` → overview.round 透出 → 审核镜头标注 + 重试按钮；新端点 `POST /{conn_id}/annotate-table` 单表重注释（缓存未命中自然重新调 LLM；兼作"刷新单表注释"手动入口）。
- **上下文裁剪**：逐表 prompt 的 `db_tables` 裁剪为 FK 邻表 + 同前缀表（上限 ~30 条）。
- **确定性候选注入图谱 prompt**（2026-09 追加）：阶段三全局/增量扫描前，把 `build_draft_edges`（FK + 命名推断）格式化为"代码已发现的候选"块注入 prompt，引导 LLM 注意力放到候选之外（语义关联/多态/误判修正），不再重复发现机器已知的事实。权威解析与确定性通道不变——候选只是参考，不替代任何通道。
- **进度体验三修**（2026-09 追加）：① 阶段一空跑打满（无表变更 → 100% + "无表变更 · 跳过逐表注释"）；② `_chat_with_beat` 两段式时间爬坡（`_climb_pct`：前 20s 爬半窗，剩余按 climb_seconds≈读超时×0.8 匀速，长推理期间条持续走、不提前封顶）；③ 阶段二/三**精准流式**：消费 `gateway.chat_stream`，容忍式部分 JSON 计数实时显示"已划分出 N 个领域 / 已识别 N 条关系"（权威解析仍为全文），读超时语义从"总时长上限"变"卡顿上限"——大库长推理不再撞 480s。
- **大库分治暂缓**（>150 表按 FK 连通分量 + 前缀预分组、组内划分后合并）：先落上下文裁剪，分治等真实大库验证后再上。

### 3.8 健壮性收尾（P2）

- **round baseline 落盘**（提前到阶段 1 与缓存同文件实施）：`KbSnapshot` 加 `round` 字段（SQLite 存 meta `round_json`），`_save_conn_blocking`/`_load_conn` 对称读写 → 重启后审核对比层不丢。
- `kb_sync_minutes` 进 `settings.py::update()` 白名单。
- 构建路径 `_kb_schema(..., refresh=True)`。

---

## 4. 实施阶段与验证

| 阶段 | 内容 | 主要落点 | 验证 |
|---|---|---|---|
| 1 | 注释缓存 + annotate_mode + tag_mode 三档 + round 落盘 | storage.py / annotator.py / build.py / api/knowledge.py / KbBuildConfirmDialog / api.ts+store | 新测试：缓存命中不调 LLM、diff 未变表零提案、rename 继承、round 快照往返；后端全量 pytest；前端 typecheck+build；重启 sidecar |
| 2 | sync 任务化 + 收尾 helper 统一 + confirm_all 按需嵌入 | jobs.py / build.py / api/knowledge.py / facade.py | 新测试：sync job 完成/无变化/失败保 ready；全量 pytest；重启 sidecar |
| 3 | 前端 A+B：镜头化 + 暂存制 + CTA/进度/二次确认/死代码 | KnowledgeReview.tsx / 删 ReviewPage.tsx / KbBuildGate.tsx / store/knowledge.ts / KbHistoryDrawer.tsx | typecheck+build；手工走查：init 构建→镜头→暂存裁决→应用→ready；重构三档；sync 进度 |
| 4 | 增量补齐 AI 阶段 + failed_tables + 单表重试端点 + db_tables 裁剪 | build.py / annotator.py / api/knowledge.py / 审核镜头徽章 | 对应单测；全量 pytest；typecheck+build；重启 sidecar |
| 5 | kb_sync_minutes 白名单 + 构建 refresh=True + AGENTS.md 更新 | settings.py / api/knowledge.py / backend/AGENTS.md | 全量 pytest；重启 sidecar 验证 health |

依赖关系：阶段 1 是 3/4 的基础（缓存支撑 diff 模式与单表重试）；阶段 2 与 3 需先后（前端 sync 进度卡依赖后端 job 通道）；阶段 4 依赖阶段 1 的缓存与阶段 3 的镜头（failed_tables 展示位）。

---

## 5. 明确不做（本期边界）

- **C 档**：详情面板拆 tab（字段/DDL/关联/高级）、URL 路由深链——留待下期；
- **大库分治**：领域划分/关系识别的预分组合并——先做 db_tables 裁剪，分治等真实大库数据；
- **嵌入模型降级可选**：构建门禁维持"对话+嵌入双模型"不变；
- **图谱二轮验证**：维持"低置信边进 draft 人工审"现状；
- 后端图/语义模块的结构重构（facade 穿透内部状态等）——不在本期范围。
