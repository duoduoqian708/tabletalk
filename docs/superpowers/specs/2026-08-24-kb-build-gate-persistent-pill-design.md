# 知识库构建门禁「常驻提醒」+ 审阅弹窗受控化 + 后端懒构建清理 — 设计文档

- 日期：2026-08-24（v2，含 pending_review 故事线）
- 状态：故事线讨论中（用户确认部分：方案 A + 胶囊直达弹窗），暂缓实施
- 范围：前端 `KbBuildGate.tsx` / `KbReviewModal.tsx` + 样式 + i18n；后端删除三处懒构建死代码

## 1. 背景与问题

知识库构建是数据源接入的强制步骤（状态机 `none → building → pending_review → ready`，
未 ready 的数据源手动查询与 AI 对话均被拦截，见 `api/query.py:99`、`api/ai.py:379`）。
当前触发入口梳理结论：

**显式构建入口（全部收敛于 `useKnowledge.buildTask` → `POST /knowledge/{id}/build` + SSE）：**

1. KbBuildGate 浮卡（全局右下门禁）：连接非 ready 时弹出，用户点击"构建"才开始
2. 知识审查页·未构建空态大按钮
3. 知识审查页·顶栏"重建"按钮（二次确认）
4. 增量同步入口：审查页"检查更新"按钮（`POST /knowledge/{id}/sync`）+ 后端 `SyncLoop` 周期任务

**问题：**

- P1 用户点 ✕/"稍后" 关闭浮卡后，引导彻底消失（仅切走再切回才重现），
  失去"未构建不可用"的持续提醒。
- P2 后端存在三处静默懒构建（`loop.py:227-233`、`report.py:287-293`、
  `context.py:97-108`），与 `ai_chat` 入口的 kb_not_built 拦截矛盾，正常流程为死代码；
  仅"ready 但工件丢失"边缘情况会静默重建。其中 `report.py` 一处漏了 `filter_sensitive`
  （潜在敏感表/列进知识库的防御缺口）。
- P3 构建完成（pending_review）瞬间**三个界面同时响应**：
  ①KbBuildGate 浮卡 pending_review 卡（含就地一键确认快捷键）
  ②KbReviewModal 三列大弹窗（kb_status 驱动自动弹出、无关闭按钮）
  ③KnowledgeReview 页面审阅队列 tab。职责重叠、主次不清。

**用户期望的正确逻辑（评审确认）：**

1. 接入流程保持"测试 → 保存 → 引导式提示"，构建需用户点击启动（不做全自动构建）
2. 浮卡关闭后退为右下角**常驻胶囊**，持续提示"不构建不可用"/"待确认"
3. pending_review 阶段：浮卡先单独出现；**只有用户点击"去审查"才展开三列大弹窗**；
   弹窗内处理完并提交后才算流程完成、关闭弹窗；否则常驻提醒一直存在
4. 状态由后端持久化记录，刷新页面后提示链路可恢复（现状已满足：`kb_status`
   存于 `connections.json`）

## 2. 方案

在已选方案 A（浮卡常驻胶囊态 + 后端死代码清理）基础上，并入 pending_review
故事线：**KbReviewModal 从状态驱动改为用户点击驱动**。

否决项：方案 B（门禁状态上收 zustand store——当前仅单组件消费，YAGNI）；
方案 C（只清后端不动交互——不满足常驻提醒诉求）；大弹窗保持自动弹出
（抢屏且与浮卡冲突，用户明确否决）。

## 3. 前端设计

### 3.1 总体流程（pending_review 故事线）

```
构建完成(pending_review)
  → KbBuildGate 浮卡单独出现（大弹窗不再自动弹）
  → ├─ 用户点「去审查」→ 三列大弹窗(KbReviewModal)展开
  │     → 处理完 + 提交(一键确认启用) → ready → 弹窗关、全部浮层消失、流程结束
  │     → 中途点 ✕ 关闭弹窗 → 回到浮卡+常驻胶囊（流程未完成，继续提醒）
  └─ 用户点 ✕/稍后 → 右下角常驻胶囊："待确认 · 点击处理"
        → 点击胶囊 → 直达三列大弹窗（用户已选定，少一层跳转）
        → 未处理就一直常驻
```

### 3.2 KbBuildGate.tsx

`dismissed` 语义变更：从「彻底隐藏」改为「缩为常驻胶囊」。

| kb_status | 未 dismiss | 已 dismiss（✕ / 稍后） |
|---|---|---|
| none | 引导卡（现状不变） | 常驻胶囊："知识库未构建 · 暂不可用"，点击展开引导卡 |
| building | 进度卡 / 可最小化进度胶囊（现状不变） | 同左（构建中不可关闭） |
| pending_review | 引导卡，按钮改为「去审查」（开三列弹窗）+「一键确认启用」保留 | 常驻胶囊："知识库待确认 · 点击处理"，点击**直达三列弹窗** |
| ready | 全部消失（不变） | — |

实现要点：

- 显示条件简化：`show = currentId && conn && (isBuilding || needsBuild)`；
  形态由 `dismissed === currentId && forceConnId !== currentId` 决定完整卡 or 胶囊
- 胶囊复用 `.kb-gate-min` DOM 结构 + `.warn` 修饰类（警告色边框/文字/脉冲点）；
  胶囊本身无关闭按钮（它是"晚点"的最终形态，直到 ready 才消失）
- 胶囊点击行为按状态分流：none → 展开引导卡；pending_review → 直接打开三列弹窗
- `forceConnId`（AI 使用时收到 `kb_not_built` 强制弹卡）始终显示完整卡
- 「一键确认启用」快捷按钮保留在浮卡上（信任 AI 结果的快速通道）；
  原「查看并修正」（跳知识库页面）改为「去审查」（打开三列弹窗）
- **构建中刷新恢复**：`useKnowledge` 新增 `reattachBuild(connId)` 动作
  （只挂 `/build/events` 吃剩余进度，不重复 POST /build）；KbBuildGate 在
  `/status` 查询发现 `building === true` 且 store 非 busy 时自动调用。后端任务
  不受刷新影响；若构建恰已结束，SSE 首帧即 done、自然退出

### 3.3 KbReviewModal.tsx（受控化改造）

- **移除自动弹出**：删除 `kb_status === 'pending_review'` 即渲染的逻辑，
  改为受控开关。开关放 `useKbGate` store 扩展（如 `reviewOpen/openReview/closeReview`），
  由 KbBuildGate（去审查按钮/胶囊点击）触发
- **新增关闭按钮 ✕**（头部）：关闭即回浮卡/常驻胶囊态，不推进流程；
  提交成功（confirm-all 成功 → ready）才自动关闭且浮层全消
- 三列内容（标签库 / 表卡片 / 关系列表）与提交逻辑维持现状不动
- 刷新恢复依赖后端 kb_status：刷新后浮卡/胶囊经 `/status` 查询恢复原位，
  大弹窗不自动恢复（需用户再点）——符合"提示不丢、弹窗不强弹"

### 3.4 i18n（zh-CN.ts / en-US.ts）

- 新增：`kb.pillNotBuilt`（"知识库未构建 · 暂不可用，点击构建"）、
  `kb.pillPendingReview`（"知识库待确认 · 点击处理"）、
  `kb.goReview`（"去审查"）
- 清理：`viewFix`、检查更新/生成标签/生成枚举相关 key（`checkUpdate`/`syncing`/
  `genTags`/`genEnums` 等）若无他处引用则移除；`rebuild` 文案改为「全部重构」

### 3.5 样式（styles/app.css）

新增 `.kb-gate-min.warn`：警告色 `border-color` / 文字色，脉冲点同色系变体；
其余尺寸/布局复用现有规则。`krm-*` 三列弹窗样式已有，不动。

### 3.6 审查页按钮收敛：「全部重构」唯一自动入口

**原则：自动批量生成入口收敛为 1 个；三列内容的手动调整口子全部保留。**

KnowledgeReview 页顶栏动作区变更：

| 按钮 | 处置 |
|---|---|
| 检查更新（手动增量同步） | **删除**（SyncLoop 默认 30min 后台自动增量，无需手动入口） |
| 生成标签 | **删除**（并入全部重构） |
| 生成枚举 | **删除**（并入全部重构，见下） |
| 重建 | 更名 **「全部重构」**，保留二次确认弹窗 |
| 未构建空态「构建」大按钮 | 保留（首次构建引导，属 none 态场景） |

**配套后端改动（功能缺口补偿）**：现 `build()` 流水线只有注释/标签/关系三个
AI 阶段，枚举生成（`annotator.annotate_enums`）是独立端点不在构建内——若只删
按钮会导致枚举字典再无生成途径。故将枚举提取并入 `build()` 作为第四 AI 阶段
（进度条 phases 增加"枚举字典"；`incremental_build` 同步纳入变化表枚举重提）。

**手动调整口子保留清单（不动）**：三列弹窗内注释 ✓/✕ 与文本修正、标签库
CRUD+绑定、枚举值含义编辑与逐列确认/拒绝、关系手动连线/删边/tombstone、
审查页审阅队列逐项处理。

**页面自动刷新**：从审查页发起「全部重构」→ 构建（浮卡进度）→ 去审查 →
三列弹窗提交（confirm-all → ready）→ 关闭弹窗后**审查页自动重载 overview**
（store `load(connId)` 数据级刷新，非浏览器 reload），确保页面呈现 ready 后
最终态。

### 3.7 枚举字典消费接入（并入列注释文档）

枚举保持结构化存储不变（`store._enums`，draft/confirmed 可编辑），但
**列注释文档（`auto-col-{table}-{column}`，即向量库内容）的 body 渲染时追加
confirmed 取值对照**：

```
orders.status 列，类型 varchar（主键）。列注释：订单状态。取值：P=待付款、S=已发货、R=已退货
```

触点三处：

1. **建库/增量重建**：`_from_schema` 生成列文档 body 时查 confirmed 枚举拼入
2. **枚举变更钩子**：人工确认/编辑/拒绝某列枚举后，同步更新该列文档并只重嵌
   该一个文档
3. **检索与上下文零改动**：`retrieve`/`to_context` 天然携带；向量语义使
   "查已退货订单"能直接召回 status 列

原则：draft 枚举不进文档（对齐 confirmed-only）；高基数列（>50 值不抽为枚举）
的向量检索方案记为未来故事。知识库文档不参与结构指纹，无假增量风险。

### 3.8 构建确认弹窗统一 + 数据授权勾选 + 审计留痕

**所有构建入口（初始化 + 全部重构）统一走「发起确认弹窗」，确认后才真正起任务。**

弹窗要素：

1. 标题/说明：即将对当前连接执行知识库构建（含 AI 注释/标签/关系/枚举）
2. **勾选框（默认不勾，免责声明性质）**："允许 LLM 读取少量实例数据以辅助理解
   表结构"：
   - 勾选 → 逐表识别时按**主键倒序抽查整行样本**发给 LLM（行数沿用
     `kb_sample_rows` 设置，默认调整至 15）
   - 不勾（默认）→ **零实例数据出网**：AI 仅看结构（DDL/注释/标签）；本地采样
     照常执行，但仅用于图谱值重叠边等本地计算，不出网
3. 确认按钮文案区分场景：「开始构建」/「开始全部重构」

**入口收敛**：

| 入口 | 处置 |
|---|---|
| KbBuildGate 浮卡选项框 | 已有，改为标准形态（勾选框语义收紧） |
| 审查页空态「构建」 | 现为直发，接同一标准弹窗 |
| 审查页顶栏「全部重构」 | 二次确认框与标准弹窗合并（原确认框退役） |

**审计留痕**：确认动作写审计（复用 `audit.logger.log()`）：`origin="kb_build"`、
`status="confirmed"`、`extra={trigger:"init"|"rebuild", include_samples:bool}`；
关闭/取消弹窗不留痕。

**配套后端改动**：

- `sample_values` 重构（core/schema.py:102）：从"每列 `SELECT DISTINCT col
  LIMIT K`"改为 `SELECT * ORDER BY <pk> DESC LIMIT n` 整行抽样（无主键表退化
  为不排序），本地按列拆分供重叠图/枚举判定使用
- **出网列裁剪器**：样本发 LLM 前过滤噪声列（created_by/create_time/updater/
  is_deleted 等命名模式，扩展 `ddl_context._is_noise_column`）与长文本类型列
  （TEXT/BLOB/CLOB 等）；裁剪只作用于出网内容，本地计算不受影响
- **枚举阶段门控**：未勾选时跳过枚举 AI 提取（现 annotator.py 无条件发送去重
  取值给 LLM，与新授权语义冲突）；进度条 phases 相应缺省该阶段
- `include_samples=false` 时注释阶段不带样本（现状保留）

## 4. 后端设计

1. **`app/ai/loop.py:227-233`**：删除 `chat_stream` 内懒构建块
2. **`app/ai/report.py:287-293`**：删除 `report_stream` 内懒构建块
   （该处漏 `filter_sensitive`，删除即消除缺口）
3. **`app/ai/context.py:97-110`**：删除懒构建分支，
   改为无条件 `ensure_loaded()` + `reembed_if_needed()`
4. **枚举生成并入构建**（§3.6 配套）：`store.build()` 增加第四 AI 阶段
   （annotate_enums，phase key 如 `enums`），`jobs.PHASES` 同步增加进度条；
   `incremental_build` 对变化表重提枚举
5. **枚举消费接入**（§3.7）：列文档 body 渲染拼 confirmed 取值对照；
   枚举确认/编辑/拒绝后更新对应文档并单文档重嵌
6. **构建授权与审计**（§3.8）：`sample_values` 重构为主键倒序整行抽样；
   出网列裁剪器；枚举阶段受 `include_samples` 门控（未授权跳过）；
   `POST /knowledge/{id}/build` 确认时写审计事件 `origin="kb_build"`
7. 相关 import 无其他引用则一并清理
8. **无其他新增后端改动**：pending_review 流程所需的状态持久化（`set_kb_status` →
   `connections.json`）与 confirm-all API 均已存在；`/sync`、`/annotate-tags`、
   `/annotate-enums` API 本次保留不删（仅前端入口移除）

### 边界行为变化

"ready 但工件文件丢失"场景：原为静默重建，改为 `ensure_loaded()` 抛错 → 上游
通用错误提示。比静默重建更诚实（用户可手动重建恢复）；错误文案优化不在本次范围。

## 5. 非目标（Non-goals)

- 不做"保存后自动开始构建"（保持引导式，用户点击启动）
- 不重构 KnowledgeReview 页面（日常管理页角色维持现状；其与弹窗的分工
  属后续故事线）
- 不扩大 kb_not_built 拦截面（schema 浏览、tasks、suggestions 维持现状）
- 不优化工件丢失场景的错误文案

## 6. 测试与验收

**回归验证：**

- 后端：`cd backend && .venv/bin/python -m pytest -q`（重点
  `tests/api/test_onboarding.py`、`tests/knowledge/`、`tests/ai/`）
- 前端：`cd frontend && npm run typecheck && npm run build`

**手动验收路径：**

1. 新建连接保存 → 浮卡弹出（none 态）→ 点 ✕ → 常驻胶囊出现 → 点胶囊回引导卡
2. 构建 → 进度正常 → 完成（pending_review）→ **只出浮卡，大弹窗不自动弹**
3. 点 ✕ → 常驻"待确认"胶囊 → 点胶囊 → **直达三列弹窗**
4. 弹窗内 ✕ 关闭 → 回浮卡/胶囊，流程未完成
5. 再进弹窗 → 处理完提交 → ready → 弹窗关、浮层全消
6. 浮卡「一键确认启用」快速通道仍可用
7. 刷新页面 → 未 ready 连接的浮卡/胶囊按后端 kb_status 恢复；
   **构建中刷新** → 浮卡恢复为实时进度条（reattachBuild 接管剩余进度）
8. 未构建连接发 AI 对话 → kb_not_built 且强制弹完整卡（forceOpen 不受影响）
9. 审查页顶栏只剩「全部重构」；发起后走浮卡进度 → 去审查 → 弹窗提交 →
   页面自动呈现 ready 最终态（无需手动刷新）；枚举字典在重构后正常生成
10. 手动调整口子回归：弹窗内改注释、编辑标签、修枚举含义、手动连线均可用
11. 任一入口发起构建均先弹标准确认弹窗（勾选框默认不勾）；确认后审计页出现
    `kb_build/confirmed` 记录，extra 含 include_samples
12. 不勾选构建 → AI 上下文/审计出网清单中无任何实例数据；勾选构建 → 样本按
    主键倒序抽取且不含噪声列/长文本列（可用 egress manifest / mock 网关核对）
13. 构建并确认枚举后，AI 对话上下文【知识库】段可见"取值：P=待付款…"对照；
    向量检索"已退货"可召回 status 列文档

**风险点：**

- `dismissed` 语义改动影响浮卡显隐核心逻辑，需覆盖切连接/切回场景不串状态
- KbReviewModal 从自驱动改受控，注意 hooks 时序（原 notPending 早退在 hooks 之后）
- 删除懒构建属死代码清理，理论零行为变化，以全量 pytest 兜底
- 枚举并入构建会改变 build 产物（新增 draft 枚举），相关 store 测试需同步更新
