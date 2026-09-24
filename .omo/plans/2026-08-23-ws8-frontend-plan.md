# WS8 前端执行计划 · 2026-08-23

> 对应 `docs/implementation/ai-planning-execution.md` WS8 (T8.1/T8.2/T8.3) + `product-handbook/03-interaction` / `04-design`
> 目标：把 WS1/WS4/WS5/WS7 已就绪的后端协议接上真实前端交互，移除假定时器，完成最后 8% 交付。

## 1. 背景与现状差距

| 任务 | 现状（8-23 核对） | 要做 |
|---|---|---|
| T8.1 真4步 | `AiRail.tsx:559-909` 用 `STEP_MS=1500` + `setTimeout` 假播放 4 步；`ThinkPanel:56-125` 默认 `open=false` 折叠；intent/retrieval 细节未绑 `context_meta`；拒答轮仍渲染 | 改为**真事件驱动**、默认展开、拒答轮隐藏 |
| T8.2 确认卡 | 后端已就绪：SSE 卡带 `confirm_token/expires_in/needs_confirm+preview_rows/rollback`，`POST /query` 同传 `token+session_id` 执行，`POST /ai/dml/cancel` 取消 | 渲染 DML 三态（执行/取消/过期）+ 问题库命中卡，文案先入 `locales/zh-CN.ts+en-US.ts` |
| T8.3 开关UI | `SettingsDrawer:658-662` 托管 `SkillPlaza`，`SkillPlaza:127-134` toggle 未对地板置灰；WS7 T7.4 地板/审计/交集已就绪 | 地板 `query/refusal` 置灰+说明，其余可勾选+toast 回执 |

依赖：WS4 `pending_dml`（T4.1-4.4）、WS5 问题库、WS7 技能开关已全绿；WS8 无新后端，纯前端。

## 2. 文件清单（只动这些）

- `frontend/src/renderer/src/components/AiRail.tsx` — T8.1/T8.2 主战场
- `frontend/src/renderer/src/components/SettingsDrawer.tsx` — T8.3 容器（逻辑在 SkillPlaza）
- `frontend/src/renderer/src/components/SkillPlaza.tsx` — T8.3 置灰/提示/toast
- `frontend/src/renderer/src/locales/zh-CN.ts` + `en-US.ts` + `frontend/src/renderer/src/locales/index.ts` — T8.1/T8.2 文案
- `frontend/src/renderer/src/api/query.ts` + `frontend/src/renderer/src/api/ai.ts` — T8.2 确认/取消调用薄封装
- `frontend/src/renderer/src/store/chat.ts` — 可选：仅在需要时为确认卡补 Turn 字段类型（尽量不动）

**不动**：`backend/*`（协议已冻结）、`tokens.css` 措辞外颜色、路由/存储架构。

## 3. 任务拆解与验收

### T8.1 真4步（随 WS1）— 优先级 P0

**要改**
- `AiRail.tsx:44-46` `STEP_DEFS` 保留，删 `STEP_MS`、`playSteps()`、`stepTimers`、`setStepStatus/attachCards/finishGate/maybeAutoRun` 的定时器分支
- 新驱动：已有 `stage` 事件（intent/retrieval）→ 直接把 `context_meta` 写进 `steps[0/1].detail`；`think`→挂当前 running 步；`sql_card`→挂 `steps[2]` 并落 `cardsRef`；`manifest/gate` 完成后把 `steps[3]` 置 done
- `ThinkPanel` 默认 `open=true`；`openStep` 默认含 `intent`？至少外层默认展开，内层 `pending/done` 可折叠但初始展开一次以通过视觉门控
- 拒答轮：`activeConv` 或 `ev.type==="refusal"` / 技能为 `refusal` 时，不渲染 `ThinkPanel+Manifest`（现有 `manifest` 渲染处加 guard）

**验收**
- 单测：`ThinkPanel` 默认展开（`open` 初始 true）；`playSteps`/`STEP_MS` 已删（grep 为 0）
- 手动：触发一次 `query` 对话，4 步由真实 `stage` 顺序点亮，intent 行显示已确认标签，retrieval 行显示 `candidate_tables`+`context_meta.capped` 提示；拒答轮无 4 步
- 门控：`npm run typecheck && npm run build` 通过

**风险**：定时器删后流式竞态；用 `useEffect` 清理并以 `streamDoneRef/gateDoneRef` 为屏障，保留 `clearStepTimers` 空实现兼容。

### T8.2 确认卡（随 WS4/WS5）— 优先级 P0

**后端协议已就绪（勿再动）**
- 卡字段：`confirm_token/expires_in/needs_confirm/preview_rows/blast/rollback` + `question_library:true/question_id/sql/sub`
- 执行：`POST /api/v1/query {connection_id, sql, origin:"ai", confirm:true, confirm_token, session_id}`
- 取消：`POST /api/v1/ai/dml/cancel {session_id, confirm_token}`

**要改**
1. **i18n 先行**：`zh-CN.ts:ws.*` 新增 `confirmExecWithRows="确认更新 {n} 行"/confirmExpired="已过期，请重新生成"/confirmCancelled="已取消"/confirmRun/confirmCancel/confirmExpiredTip/qlHit="问题库命中 · 零模型调用"/qlExec="执行"/qlDesc`
    `en-US.ts` 同步；`SkillPlaza` 风格只用 `t()` 键，不硬编码中文
2. **`AiCard` 类型扩展**（`api/ai.ts:39-61`）：补 `confirm_token?:string; expires_in?:number; needs_confirm?:boolean; question_library?:boolean; question_id?:string`
3. **`SqlCard` 分支**：
   - 命中卡（`question_library`）：渲染 `sub="问题库命中"` + `执行` 按钮走 `POST /query`（`origin:"ai"` + `sql` 透传，正常过闸门与审计 `source=question_library`）+ `needs_confirm` 可选
   - DML 需确认卡（`verdict==="review" && needs_confirm && !executed`）：三态
     - 执行：`确认更新 ${preview_rows} 行` + `expires_in` 倒计时 → 调 `POST /query` 带 token+session_id → 成功置 `card.executed=true/ affected`
     - 取消：调 `/ai/dml/cancel` → 追加 `kind=system` 提示到下一轮（WS4 T4.4 已写回历史，前端只需发请求并本地置 `cancelled` 态）
     - 过期：`expires_in<=0` 或 403 `expired` → 灰态提示
   - 普通 allow/block 卡保持现有 `run/ddlBlock` 逻辑

**验收**
- 单测：i18n 键存在（双语）；`AiCard` 含新增字段类型检查通过
- 手动：走 `run_dml REVIEW` 流程可见 `confirm_token` 卡，三态可点；问题库命中卡可见且点执行后出真实结果；不点不执行
- 门控：typecheck/build 通过；现有 `SqlCard` 非 DML 回归不变

**风险**：卡片复用导致 `executed` 态串台；用 `card.confirm_token` 为 key，成功后锁按钮。

### T8.3 技能开关 UI（随 WS7）— 优先级 P1

**要改**
- `SkillPlaza.tsx:120-138` toggle：判断 `FLOOR={"query","refusal"}` → `disabled={busy||FLOOR.has(s.id)}` 置灰，`title` 用 `t('skill.floorHint')`，`className` 新增 `floor` 变体（`app.css/tokens.css` 用 `var(--text-muted)` 已有变量，不新增颜色）
- 变更成功后 `toastMsg(t('skill.toggleOk', {name:s.name, enabled}))` 或 `t('settings.saveSuccess')` 复用，失败 `toastMsg` 已有
- `SettingsDrawer.tsx` skills 分节标题下增一行业务说明：`t('skill.floorNote')`（"query/refusal 为地板技能，始终开启"）

**验收**
- 单测/视觉：地板行 checkbox `disabled` 且 `title` 含说明；非地板可切
- 手动：关 `report` 再开，toast 出现；切地板无反应（或轻提示）；`PUT /skills` 审计可用 `GET /audit?source=settings` 验证
- 门控：typecheck/build

**风险**：置灰仅前端，安全靠后端 `FLOOR` 双层校验已就绪（T7.4）。

## 4. 执行顺序与依赖

```
T8.1（真4步，地基） ──┐
                      ├─→ 联调 → typecheck/build → 截图回归（可选）
T8.2（确认卡，后端已就绪，可并行 T8.1） ──┘
T8.3（开关UI，独立，WS7 后可立即做） ───────→ 同上
```

建议单分支单 PR 逐步合：`T8.1 → T8.2 → T8.3`，每步均跑 `npm run typecheck && npm run build`；涉及交互的补 e2e（`npm run screenshot` 仅星图）。

## 5. 测试与门控

- **必跑**：`cd backend && .venv/bin/python -m pytest -q`（已 381 passed, 不动后端则保持） + `cd frontend && npm run typecheck && npm run build`
- **新增**：`SkillPlaza` 置灰断言、`ThinkPanel` 默认展开断言、i18n 键覆盖（双语）
- **不做**：平台工具批次2/3、KB 冷启动方案（开放问题 Q1）、`/ai/selection` 恢复

## 6. 颜色/文案硬规则

- 颜色只引 `tokens.css` 变量，禁硬编码；`SkillPlaza` 地板置灰复用 `--text-muted/--border` 已有
- 文案一律先入 `locales/{zh-CN,en-US}.ts` 再在组件用 `t()`，中英双份

## 7. 交付物

- 代码：上述 5-6 文件改动，单次 `git diff --stat` ≤ 10 文件
- 文档：本计划 + `ai-planning-execution.md` WS8 三卡勾 ✅ 并注 commit
- 验证证据：`typecheck`/`build` 日志，确认卡三态录屏/截图（如有）

## 8. 回滚

任一 T8 回滚不影响后端：仅前端展示回退，已有假定时器分支可一键恢复；确认卡失败回 `needs_confirm`灰态。
