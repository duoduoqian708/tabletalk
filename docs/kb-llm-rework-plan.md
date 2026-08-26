# 知识库构建 × LLM 记账统一改造 · 实施清单

> 状态：**✅ 全部完成**（段1~9 闭环）· 生成：2026-08-25 · 维护：在 docs 内随进度更新
> 关联设计文档：`docs/ai-tools-skills-design.md`（铁律 3：每次模型调用有清单有审计）

---

## 0. 背景与目标

1. **记账缺口（硬伤）**：知识库构建的 N+3 次 LLM 调用（逐表注释 / 领域标签 / 图谱两轮）目前**零记录** —— 不写成本（`LlmCallLog`/`CostTracker`）、不写出网清单审计（`egress`）。这与铁律 3 冲突，成本页和出网报表看不到构建消耗/出网内容。
2. **统一拦截器**：需要「作用在 LLM 调用函数上」的拦截器，**任何场景调用必记、一次请求/响应只记一组、流式不遗漏**。
3. **5 项构建逻辑调整**（用户口述）：敏感名单交互化 / 采样严格授权 / 逐表两套模板 / 领域标签批量划分 / 进度条重构。
4. **推理能力**：阶段2/3 开启推理（接入时探测是否支持 + 最大深度，落库到模型配置）。

---

## 1. 已锁定的决策（含拍板）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 敏感名单存储形态 | **精确名列表**：`{表名, 可选列[]}`；选表不选列=整表排除；按真实名过滤，不转 glob |
| 2 | 采样授权 | **严格零采样**：不授权 → 不抽不落盘不发；仅 DDL+字段名注释 |
| 3 | 逐表注释模板 | **两套独立模板**：有采样版 / 无采样版，绝不共用一套 |
| 4 | 领域标签契约 | LLM 返回**领域列表** `[{name, 描述, 成员表[], 依据}]`，目标 ~√N 个；允许一表挂多领域；表描述来自阶段一，领域调用**不再写 desc_drafts** |
| 5 | 记账拦截器范围 | **全部记（含 mock）**：chat 与 chat_stream 都接，写 `LlmCallLog` + `CostTracker` + egress 审计 |
| 6 | 推理深度 | 阶段2（标签）/阶段3（图谱）开启推理；接入时探测支持+最大档位，存 `ModelConfig.capabilities` |
| 7 | 自检 | 阶段2 + 阶段3 各做一次**审校式自检**（拿上一遍产物回来裁决/修正，不重跑生成）；`kb_build_self_check` **默认开**（运行时可关，构建弹窗可逐次覆盖） |

---

## 2. 执行段

### 段1 · LLM 统一记账拦截器 —— ✅ 完工

**目标**：`gateway.chat` / `chat_stream` 为唯一记账点（无条件必记），旧 5 处硬编码迁入 ctx 并删除；流式 `finally` 兜底；失败也记。

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 1.1 | `chat`/`chat_stream` 加 `ctx` 参数；`_record_llm`（llm_log+cost_tracker+egress）无条件调用；`ctx=None` 用默认（skill=llm/connection=unknown）；**失败也记（token=0，可追溯）** | `backend/app/ai/gateway.py` | ✅ |
| 1.2 | `chat_stream` 整段 `try/finally` 记账（中断/异常不漏） | 同上 | ✅ |
| 1.3 | KB 构建 4 处 `provider.chat` 传 ctx（kb-annotation/kb-tags/kb-graph，candidate_tables 如实） | `backend/app/knowledge/annotator.py` | ✅ |
| 1.4 | 主聊天 loop：删 egress 硬编码 + 删 LlmCallLog/CostTracker + 传 ctx | `backend/app/ai/loop.py` | ✅ |
| 1.5 | preflight：删 egress（关键字路径无 LLM 不再产 egress）+ 删 LlmCallLog + 传 ctx(status=egress-intent) | `backend/app/ai/preflight.py` | ✅ |
| 1.6 | report：删 `_log_llm_call` + 3 helper 改接 ctx + 删 report_stream egress | `backend/app/ai/report.py` | ✅ |
| 1.7 | ai_review：删 egress + LlmCallLog + 传 ctx | `backend/app/ai/tools/ai_review.py` | ✅ |
| 1.8 | compress：删 egress + 传 ctx（若走 provider.chat） | `backend/app/ai/compress.py` | ✅ |
| 1.9 | 冒烟验证：chat + 流式各记一组、无重复、流中断不丢 | 独立脚本 | ✅ |
| 1.10 | **pytest 全量**：拦截器统一后 418 passed / 8 skipped 全绿（含 egress 计数契约） | `backend/tests/` | ✅ |
| 1.11 | `/ai/test`、其它直接 provider 调用点统一确认走拦截器 | 全局 grep | ✅ |

---

### 段2 · 推理探测 + 落库 —— ✅ 完工

**目标**：模型接入时探测「是否支持推理 + 最大档位」，存进模型配置，阶段2/3 用最大深度。

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 2.1 | `_detect_reasoning` 扩展：布尔通过后发 `reasoning_effort=high` 实测 → max_effort ∈ low\|medium\|high；拒绝则降档探测 | `backend/app/api/ai.py` | ✅ |
| 2.2 | `ModelConfig` 加 `capabilities: {reasoning, reasoning_effort}` 字段（含序列化，旧数据缺省空） | `backend/app/core/settings.py` | ✅ |
| 2.3 | `/ai/test` 成功后写回对应 `ai_models` 条目 → `SettingsStore.save()` | `backend/app/api/ai.py` + settings | ✅ |
| 2.4 | 阶段2/3 构造 provider cfg：`capabilities.reasoning → effort ?? "thinking"`（有档开最大、无档仅思考、mock 不受影响） | annotator `_kb_reason_provider_cfg` | ✅ |

> 实测：deepseek-v4-flash `reasoning=true` 但 `reasoning_effort=null`（不接受档位）→ 阶段2/3 自动用 `thinking`。

---

### 段3 · 采样严格授权门控 —— ✅ 完工

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 3.1 | `build_index._run` 采样改为仅 `include_samples=True` 时执行 | `backend/app/api/knowledge.py` | ✅ |
| 3.2 | 同步入口门控（手动 `/sync` + 定时 `SyncLoop.tick`）按运行时 `kb_ai_annotation_samples` 判定；`annotate_knowledge` 独立 API 先验已按 `include_samples` 门控 | `api/knowledge.py` + `jobs.py` + `annotator.py` | ✅ |
| 3.3 | 确认 store 在 samples 为空时各消费点安全（`_build_graph` 纯 FK 无关、`truncate_samples({})`→`{}`、mock 路径空守卫） | `store.py` 检查 | ✅ |
| 3.4 | **回归测试**：`tests/api/test_kb_zero_sample.py` 4 例钉死「未授权零抽样 + 授权逐表抽样 + 同步同构」 | `tests/api/` | ✅ |

---

### 段4 · 逐表注释两套模板 —— ✅ 完工

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 4.1 | `annotate_table` 拆成：有采样版（样本段+values 指令+含 values JSON 契约）/ 无采样版（纯结构不提 values）+ 无采样硬闸（丢弃 LLM 硬凑的 values/example） | `annotator.py` | ✅ |
| 4.2 | mock 路径对齐两版（`_mock_table_comments_from_ddl` 按 samples 有无自动增减 values/example） | 同上 | ✅ |
| 4.3 | 测试：模板契约（不含"样本/values"）+ 分流行为 + 硬闸 | `tests/knowledge/test_annotator.py` | ✅ |

---

### 段5 · 图谱自检并入第二轮 —— ✅ 完工

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 5.1 | `annotate_graph` 第二轮候选池 = 程序候选 ∪ 第一轮 `confidence≠high` 的 LLM 边 | `annotator.py` | ✅ |
| 5.2 | verify prompt 明确「只裁决给定候选（confirmed/rejected）+ 修正字段，不新增」 | 同上 | ✅ |
| 5.3 | `kb_build_self_check` 运行时兜底开关（**默认开**，可运行时关；构建弹窗可逐次覆盖）；最终去重 | settings + build 弹窗 | ✅ |

---

### 段6 · 领域划分 + 自检 —— ✅ 完工

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 6.1 | 表画像构造：表名｜表描述（库注释优先，缺则阶段一表草案）｜核心字段名(描述)，去噪音 `is_noise_column`，**不显字段类型** | annotator `_build_table_portraits` | ✅ |
| 6.2 | 数量目标随库规模：`target=max(3, round(sqrt(N)))`，prompt 注入 `lo~hi` 区间 | annotator | ✅ |
| 6.3 | 一轮「划分」：N 条画像打包一次调用 → 领域列表 `{name, 描述, 成员表[], 依据}`；一表可多领域 | `annotate_domain` 重写 | ✅ |
| 6.4 | 一轮「审校自检」：初版划分+画像送回，裁决（单表成域/语义重叠/孤立表/命名），仅在改进时输出修正版否则 `{"unchanged":true}` | `_domain_selfcheck_prompt` | ✅ |
| 6.5 | 落库：域→标签草案 + `assign_table_tags` 多打标；**不再写 desc_drafts**（表级描述前移阶段一 `annotate_table` 每表产出） | annotator + store | ✅ |
| 6.6 | 解析校验：域数上界、无孤表、表名真实；越界程序兜底（`_normalize_domains`） | annotator | ✅ |
| 6.7 | 测试：画像/解析/兜底/一轮+自检/覆盖开关/无 desc_drafts（`tests/knowledge/test_domain_partition.py` + graph 覆盖） | tests | ✅ |

---

### 段7 · 进度条重构 —— ✅ 完工

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 7.1 | `PHASES` 扩带 `steps` 元数据；report 维度 `(stage, percent, detail, phase)` → `+step/step_index/step_total` | `jobs.py` | ✅ |
| 7.2 | 子步语义：阶段二=划分→自检、阶段三=全局→裁决、阶段一=逐表 i/N+表名（annotator 内部上报） | annotator | ✅ |
| 7.3 | 全局 overall 条按阶段权重窗口映射（`PHASE_WINDOW`，单调不降）+ 阶段条子步计数 | `jobs.py _overall` | ✅ |
| 7.4 | 失败定位到子步（`error_at`{phase,step}）；自检 unchanged 时自检子步正常收尾；阶段一标注授权与否 | jobs + store + annotator | ✅ |
| 7.5 | 前端 `BuildPhase/BuildProgressState` 加可选字段（step/step_label/steps/error_at，兼容旧帧）+ 阶段条 chips | `store/knowledge.ts` + `KbBuildGate.tsx` | ✅ |
| 7.6 | 测试：窗口映射单测 / steps 元数据 / SSE 帧流（单调 + 子步标注） | `tests/api/test_kb_progress.py` | ✅ |

---

### 段8 · 敏感名单交互化 —— ✅ 完工

| 子步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 8.1 | `ConnectionConfig.sensitive` 支持 `{table, columns[]}` 精确名结构（与旧 glob 字符串**混排兼容**）；`columns` 缺省/空=整表排除 | `core/connections.py` + `api/connections.py` | ✅ |
| 8.2 | `filter_sensitive` 精确名过滤：dict 表名精确、列子集表内剔除、整表联动 FK 悬空清理；glob 语义不变 | `core/sensitive.py` | ✅ |
| 8.3 | `ConnectionModal` 文本框 → 「＋新增」下拉选表 + 勾列 +「整张表」开关（编辑模式拉 schema 提供选项；新建回退手动表名/逗号列）；旧 glob 条目以 dashed chip 展示可移除 | `ConnectionModal.tsx` + `SettingsDrawer` 卡片 | ✅ |
| 8.4 | 测试：精确整表/精确列表内作用域/无 columns 键/混排/API 往返（`tests/core/test_sensitive.py` + `test_api.py`） | tests | ✅ |

---

### 段9 · 全量验证 —— ✅ 完工

| 项 | 结果 |
|---|---|
| pytest 全量 | **450 passed / 8 skipped**（9 段新增 ~70 用例） |
| 前端 typecheck + build | 通过 → dist 已更新，8777 同源托管 |
| 端到端记账不变量（隔离实例 + 确定性 MockProvider 走真实 LLMGateway） | 构建三阶段 LLM 调用全进 `llm_log`/`cost_tracker`/`egress`（kb-annotation/tags/graph）；**一次请求=一组**（逐表注释条数==表数）；mock 网关也记；egress 清单先于出网；未授权构建零 values/example 出网/落库 |
| 端到端固化测试 | `tests/knowledge/test_kb_llm_integration.py`（2 例，防回归） |
| 真实模型构建 | 由用户在 8777 浏览器验证（真实 deepseek + doubao embedding；成本页/出网报表可视化 + 注释质量） |

---

## 3. 关键开放点 / 风险

- **preflight 语义变化**：关键字快判路径不调 LLM，不再产 egress 行（「无出网即无清单」）。`test_preflight_egress_exactly_one`/`test_egress_manifest` 若断言受影响，需同步调整测试。
- **report egress 粒度**：原每份报告 1 条 egress → 改为每次 LLM 调用 1 条（clarify/plan/narration 各自记账），更细但计数变化。
- **`/ai/test` 探测调用**也会被拦截器记账（skill=llm），属预期（全部记录）。
- **流式中断**：`chat_stream` finally 记录，usage 为中断前累积的最后一个 chunk。
- ~~**`kb_build_self_check` 默认值未拍板**~~（已定：默认开；运行时关 + 构建弹窗覆盖）。
- **2D 图小节点命中带**：圆环带宽固定 `LINK_BAND=10`，最小节点 r=18 时中心移动区仅 8px。已建议内环改相对 `max(6, r×0.6)` —— **用户未确认**，改动时需拍板。
- **阶段2/3 完整提示词全文**：结构已定（见段5/6），**逐字文案「后面慢慢磨」**，不在本清单固化。
- **`last_meta` 遗产**：loop/preflight/report 迁移后不再读它，`gateway` 仍写（向后兼容）；可后续清理。

---

## 4. 本会话已完成（早期交付，独立于本计划，记录完整账本）

> 以下均已实现并通过 typecheck + build + 8777 验证（构建产物已在 dist）。

| 完成项 | 要点 | 文件 |
|---|---|---|
| 页面背景网格去除 | body 层 48px 网格删除（深/浅两套），保留品牌光晕+噪点；`.trg2d` 画布网格按用户倾向保留 | `styles/tokens.css` |
| 2D 关系图重设计 | 圆形节点（配色/尺寸对齐 3D `SPHERE_PALETTE`+log 缩放）；编辑态箭头线(from→to)、展示态定向流动；圆弧拖线连表+属性面板、中心拖动移动节点；右上改「展示/编辑」两级（撤掉 3D GraphEditor）；3D 同源命中 | `components/TableRelationGraph2D.tsx`、`hooks/useTrg2d.ts`、`KnowledgeReview.tsx`、`styles/app.css`、`components/KbReviewModal.tsx` |
| 知识库历史记录入口 | 移除侧边栏「审批流」(团队模式 DML 死页)；知识库页右上「历史记录」→ 抽屉读审计 `origin=kb_build`；`confirm_all` 补审计留痕 | `components/KbHistoryDrawer.tsx`、`KnowledgeReview.tsx`、`AppLayout.tsx`、`backend/app/api/knowledge.py` |
| 顶部大模型展示移除 | 头部 `gw` 模块删除；底部状态栏保留 | `AppLayout.tsx` |
| 定时任务控制台重设计 | 统计条 + 任务卡片（cron 友好化/启停/运行/删除/展开 SQL）+「实时/预览Mock」切换 + 新建弹窗；修复 Mock 重置 | `TasksConsole.tsx` |
| AI 对话 md 渲染 | `react-markdown`+`remark-gfm`，3+ 空行折叠，`pre-wrap→normal`，Markdown 样式全套 | `AiRail.tsx`、`deck.css`、`package.json` |
| 知识库/成本两页 50/50 | `.kb-cols` 42/58→50/50；成本 `3fr 2fr`→`1fr 1fr` | `review.css`、`CostDashboard.tsx` |

> 现存未提交工作区还包含 c3 会话早期产物（`KbReviewModal` 标签计数修复、`app.css` krm 全屏三栏等），与本计划无关，验收后一并整理提交。