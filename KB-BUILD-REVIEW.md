# 知识库构建走查 · 问题追踪清单

> 起始：2026-09-08 · 覆盖范围：`backend/app/knowledge/`（build/sync/审查语义/storage）+
> `backend/app/api/knowledge.py` + 前端审查交互（KbBuildGate / KnowledgeReview / ReviewLens /
> KbHistoryDrawer / store/knowledge.ts）
> 状态标注：🔴 P0 数据丢失/状态断裂 · 🟡 P1 主功能失效 · 🟢 P2 一致性/健壮性 · ⚪ P3 小问题/死代码
> `open` = 待处理 · `fixed` = 已修复
> **处理策略（用户指示 2026-09-08）：修复时不叠加屎山——该删的死代码/死分支一并删除，
> 接口和状态机收敛干净，不做补丁摞补丁。**

## 修复进度（2026-09-08 第一批）

**已修（代码已落地 + 218 测试绿 + typecheck/build 通过 + 重启持久化实测）**：
P0-A/B/C/D/E（数据丢失与状态卡死全组）· P1-1/2/3/5（红边闭环：pin 接线+持久化、确认移除、
单边裁决）· P1-9/10（vstore 重建、嵌入失败保留旧向量 + embedder 按需创建）· P1-11/12/13
（自动同步审计+60s 状态轮询、changed 键、轮询重试+forceOpen 生效）· P1-14（confirmed
过滤器防 AI 覆盖）· P1-15（sync job 进度+取消检查点）· P2-7（query_log 草案跨重建存活）·
P2-10（JSON 后端版本方法）· P2-15 后半（batch-review 归档 + finalize 落盘）· P2-24
（历史抽屉服务端过滤 + 正则）· 顺手清死代码（all_samples/重复 touched/重复状态请求）。

**仍 open（下一批）**：P1-6（confirm_all 采用 tags_new）· P1-7（台账外即时裁决门控）·
P1-8（表级用旧吞字段用新）· P1-16（重复构建后端防护）· 其余 P2/P3。

## 走查方式

三路并行代码走查（构建/同步生命周期、审查确认/放弃/diff 语义、前端交互）+ 双库实时状态探针
（演示库 / mdm-ai）。行号以 2026-09-08 工作区（commit dabd0fc）为准，修复时允许漂移。

---

## 一、P0 · 数据丢失 / 状态断裂

### 🔴 P0-A — fixed
- **位置**: `build.py:626/606/660/669-682/931`，对照 `api/knowledge.py:234-248`
- **问题**: `build()` 全程不 `ensure_loaded`。后端重启后（dev 热重载/崩溃）内存为空时点「重建」：
  diff 模式静默退化 full、`_sync_table_shells` 在 setdefault 出的空 dict 上建全新壳、
  `_auto[conn_id]` 先被赋值导致 `annotation_cache()` 里的 ensure_loaded 空转——磁盘数据永不被救回，
  收尾 `_save_conn` 把**空快照落盘**。
- **症状**: 重启后一次重建 → 已确认边/注释/标签/绑定/布局/排除表/向量全部覆盖丢失，审核页把所有表显示为"新增"。**演示库 edges 全空的最可能根因**。
- **修法**: build 入口（及 fallback 路径）先 `ensure_loaded(conn_id)`。

### 🔴 P0-B — fixed
- **位置**: `build.py:643`（draft 队列 pop）、`annotator.py:596`（阶段一收尾落盘）、`build.py:569-573`（discard 同样清队列）
- **问题**: draft 边队列在 AI 阶段开始前被销毁、阶段一收尾即落盘；构建失败/取消/放弃后不可恢复。确定性 FK 草案只由全量重建产出——放弃或失败后待审 FK 边永久消失。
- **症状**: **演示库 FK 草案消失的第二根因**；已确认边不受影响（确认+待审一起看才全空）。
- **修法**: 队列销毁推迟到新提案就绪（merge 是替换语义，无需先清）；discard 对确定性来源草案与 AI 提案区分处理。

### 🔴 P0-C — fixed
- **位置**: `build.py:869` + `graph/store.py:39-59`；`core/schema.py:57-90`
- **问题**: `filter_confirmed_by_schema` 无"空 schema"守卫——结构探测因权限/方言返回空表集（不抛异常）时，每条已确认边判失效、全部表知识被清、正常落盘。
- **症状**: 一次「成功」的构建后整库静默清空。
- **修法**: 空表集 → 拒绝构建（fail-closed），不进构建流程。

### 🔴 P0-D — fixed
- **位置**: `api/knowledge.py:200-208`（/build 的 JobBusyError 回退置 none）；`jobs.py:303-316`（apply_sync_result_status 只在 ready 流转）
- **问题**: `POST /build` 先置 building，`start()` 遇 JobBusyError（sync/confirm/discard 占坑）→ 无条件回置 **none** 而非恢复原状态；之后永不恢复。30 分钟自动同步持 `_ops` 坑期间前端无感、按钮全可点。
- **症状**: 自动同步中点「重新构建」→ 409 + 门禁变「未构建」+ **AI 断供**（ai.py `!= ready` 即拒）。ReviewLens applyAll 进行中同理（顶栏重建按钮不看 confirm op）。
- **修法**: 记录原状态回退（或干脆先 start 后置状态）。

### 🔴 P0-E — fixed
- **位置**: `storage.py:456-460`（版本门控返回空 snap）、`facade.py:435-436`（is_built 只看文件）、`state.py:110-112`（启动迁移 none→ready）
- **问题**: v1 工件版本作废 → 内存空 KB + 连接标 ready 的不一致：overview 显示 built+ready+零内容；SyncLoop 30 分钟后指纹失配走内联全量回退；`_load_conn` 空快照早退导致 `_user` 永不置位、每次 ensure_loaded 重读文件。启动迁移还把「discard 后无 confirmed 内容」的连接也翻成 ready。
- **症状**: 门禁不弹、无重建引导、AI 拿空知识上下文。
- **修法**: 版本作废时降级 kb_status + 前端弹重建引导；is_built 校验版本。

---

## 二、P1 · 审查闭环（三色 diff 半成品）

### 🟡 P1-1 红边「保留 pin」前端零接线 — fixed
- `api/knowledge.ts:282-287` 定义了 `pinEdge` 全前端零调用；`api/types.ts` GraphEdge 无 `pinned` 字段；ReviewLens.tsx:534-537 红边卡 onConfirm/onReject = `() => undefined`。后端 `graph/store.py:251-263` 能力齐备。
- **症状**: 红边既不能保留也不能处理，纯展示。
- 附：**pin 标记重启丢失**（P2-1 关联）——SQLite 后端 save 经 GraphEdge 重建，无 pinned 字段不透传，重启后全部失效。

### 🟡 P1-2 红边「确认后移除」永不发生 — fixed
- 文案承诺「随本轮确认生效移除」（zh-CN.ts:772），后端 `_maybe_finalize`（api/knowledge.py:631-641）只 `clear_diff_base`，全文无按 diff 删边的路径。
- **症状**: 确认后红边原样留图、下轮重新标红——文案说谎。

### 🟡 P1-3 单边裁决实为按表（双向）批量 — fixed
- 前后端都只按 `from_table`：`graph/store.py:178-186` 过滤 `from==X or to==X`；ReviewLens.tsx:173-174 / KnowledgeReview.tsx:887-889 / useTrg2d.ts:66-78 同粒度。
- **症状**: 点一条边的 ✓，该表任一端点的全部 draft 边批量生效。设计「逐边裁决」没接上。
- **修法**: 后端加单边 confirm/reject 端点（按列对+方向匹配），前端逐边传参。

### 🟡 P1-4 黄边确认两头都不对 — fixed（确认时按列对取新，不再丢弃/造平行边）
- `graph/store.py:93-98`（黄=同列对基数不同）与 `confirm_graph_edges:195-199`（判重键方向敏感）矛盾：同方向黄边确认被静默丢弃（基数不更新）；反方向黄边确认 append 造反向平行边。

### 🟡 P1-5 diff 基线不持久化，重启红边全灭 — fixed
- `_diff_base/_diff_active` 不进 KbSnapshot（storage.py:43-70；round 有 storage.py:673）——重启后绿/黄在、红边全转灰。增量 sync 不 set/clear diff 基线，旧基线与新队列错位（假红/假灰）；构建进行期 `_llm_graph_edges` 已清而 diff_active 还挂着 → 轮询图谱出现「全红风暴」闪烁。
- **修法**: diff 基线随 round 一起持久化；增量 sync 重置基线；构建期抑制红边判定。

### 🟡 P1-6 confirm_all 不采用本轮标签划分 — open
- 全量构建的新标签只挂 round.tags_new 不落库（build.py:803-806）；confirm_all 只逐个 confirm_tag 翻 status（facade.py:291-294），随后 clear_round 丢弃 tags_new。
- **症状**: 全量重建后点「确认全部」，本轮标签新划分（名称/描述/成员绑定）整体静默丢失；首次构建则一个标签都没有。与审核页两栏 apply-round「全取新」语义不一致；单标签「采用新版」粒度也不存在。

### 🟡 P1-7 台账外即时裁决漏洞（两套确认语义并存） — open
- pending_review 期间列级 ✓✕ 已隐藏（拆除清单#5），但**表注释行级按钮（KnowledgeReview.tsx:364-369）、对比弹窗按钮（522/531）、左栏 draft 标签 ✓✕（922-923）、图库 draft 边按钮（863-896）均未门控**——可绕过镜头台账直接生效。
- **症状**: 审核中点 ✓ 立即生效，镜头该项随即消失，裁决统计与印章栏不再反映真实。
- **修法**: pending_review 期间统一收口到台账（或这些即时路径改走暂存）。

### 🟡 P1-8 表级「用旧」静默吞字段级「用新」 — open
- ReviewLens.tsx:256-263 只有 'old' 有暂存记录（'new'=缺省）；applyAll（305-317）先 `rejectComment(table)`——后端无 column 即清全表全部提案（含列）。
- **症状**: 表选「用旧」时，用户标了「用新」的字段提案一并被清——UI 呈现可混合裁决，数据模型表达不了。

---

## 三、P1 · 向量与检索

### 🟡 P1-9 confirm_all 后 vstore 不重建 — fixed
- `facade.confirm_all:298` 调 `_embed_tables`（build.py:488-524）只更新 `_table_vec`，从不 `_rebuild_vstore`；对照单表确认路径 `_reembed_tables`（build.py:527-540）有重建。`reembed_if_needed` 指纹未变不补偿。
- **症状**: 确认后向量检索/路由停在旧嵌入，新画像、draft→confirmed 加分不生效，直到下次构建。

### 🟡 P1-10 嵌入失败把旧向量覆盖为 [0.0] — fixed
- `build.py:512-515` `except: vecs[name]=[0.0]` 随后覆盖旧值，与注释「嵌入失败保留旧向量」相反。叠加 confirm-all 不刷 embedder（`_emb=None` → AttributeError → 全部 [0.0]，facade.py:279-302 对照 build.py:534）。
- **症状**: 嵌入 API 一次抖动/重启后直接确认 → 检索质量静默劣化（仅 warning 日志）。
- **修法**: 失败保留旧向量 + confirm_all 前重建 embedder。

---

## 四、P1 · 自动同步可见性

### 🟡 P1-11 30 分钟自动同步「三无」 — fixed
- `jobs.py:432-478` SyncLoop inline 持 `_ops` 跑：无 job、无进度帧、无审计——历史抽屉看不到、前端无提示。
- **症状链**: 自动同步产出提案转 pending_review → 用户无感 → AI 用「知识库未构建」误导文案拒绝（ai.py:516，pending_review≠ready）→ forceOpen 因状态过期不弹门禁（见 P1-13）→ 用户只看到「AI 坏了」。

### 🟡 P1-12 sync 空工件回退全量的结果缺 `changed` 键 — fixed
- `build.py:443-445` 直接返回 build 结果（无 changed）；`apply_sync_result_status`（jobs.py:96-115）首行判 changed 为假即 return。
- **症状**: 后台全量重建完成后 kb_status 停 ready、**pending_review 永不出现**、审计留痕被跳过——审核入口隐身、草案长期滞留。

### 🟡 P1-13 前端构建感知丢失 — fixed
- **轮询零重试**（knowledge.ts:116-134）：一次瞬时 fetch 失败即判任务死亡 → 门禁消失而后端仍在构建，完成零通知（手机锁屏/切网高频触发；overview 有四次重试、轮询没有）。
- **forceOpen 空操作**（KbBuildGate.tsx:134-135）：`show` 不含 forceConnId、status 只在 `[currentId]` 变化时拉取——收到 kb_not_built 时本地状态过期则门禁不弹。
- **修法**: 轮询加重试；forceOpen 触发时强制刷新 status。

### 🟡 P1-14 AI 裁决可覆盖人工 confirmed 过滤器 — fixed
- `annotator.py:1963-1976` AI 路径直接 `store.add(...)`（FilterStore.add 无条件覆盖，filters.py:103-109）；结构检测路径（build.py:377-381）有 confirmed 保护，但 AI 裁决先跑（gather 内），保护形同虚设。
- **症状**: 重建/增量后用户确认过的租户/软删过滤器被 AI draft 覆盖或改判 exempt，查询注入静默失效——违反「已确认资产保留」。
- 附：`soft_delete` 高置信自动 confirmed（annotator.py:1971-1974）与「一切经确认生效」原则相悖（文档自认例外，修复时一并定夺）。

---

## 五、P1 · 并发与互斥

### 🟡 P1-15 sync job 无取消检查点 — fixed
- `start()` 只标记 `old.cancelled=True`（jobs.py:173-188）；`incremental_build` 无 on_progress/检查点——被替换的 sync job 一路跑到底，与新 build 并发读写 facade 内存、交错落盘（`_save_conn` 全量快照 last-writer-wins）。
- **症状**: 重复点击/边构建边同步 → 混合状态快照、edges/tables 回跳。

### 🟡 P1-16 重复构建只靠前端防护 — open
- `POST /build` 对「已有 build job 在跑」静默替换不 409（api/knowledge.py:175-212）；双开窗口/网络重放即并发构建。SSE `build_events` 捕获旧 job 引用（:296），替换后第二 job 帧不推送。

---

## 六、P2 · 一致性 / 健壮性（open）

| # | 问题 | 位置 |
|---|---|---|
| ✅ P2-1 | pinned 边重启丢失（SQLite save 经 GraphEdge 重建不透传；JSON 后端不受影响） | graph/model.py:68-93、storage.py:625-628 |
| P2-2 | 只确认过图边的库 discard 后回 none（has_confirmed_content 不看边） | api/knowledge.py:514、semantic/store.py:499-507 |
| P2-3 | 崩溃后 building→none 不区分有无已确认历史（应回 ready） | state.py:108-114 |
| P2-4 | D3 中止发生在落盘后（jobs.py:287-300 在 build.py:931 之后判失败） | jobs.py:287-300 |
| P2-5 | tag_mode=fresh 清空标签库在失败不可回滚点 | build.py:776-779 |
| P2-6 | 全量构建不清孤儿标签（只增量路径清） | build.py:650-653 |
| ✅ P2-7 | query_log 草案被重建无痕清掉；入队不触发 pending_review | build.py:643、behavior/service.py:50-77 |
| P2-8 | excluded 排除表只过滤视图：检索/路由/join 校验/上下文全不认 | graph/store.py:265-313、semantic/store.py:574-598 |
| P2-9 | TableKnowledge.excluded 死字段（与 graph_store._excluded 永不同步） | store.py:90/129/148 |
| ✅ P2-10 | JSON 存储后端 confirm-all 直接 500（缺整套版本方法） | storage.py:814-820、build.py:296 |
| P2-11 | confirm_all 多次同步全量落盘（每标签一次 _save_conn）+ 阻塞事件循环 | facade.py:285-294 |
| P2-12 | confirm_all 无原子性；_save_conn_blocking 吞异常仅 warning | facade.py:281-300、158-159 |
| P2-13 | _archive_current_fields 读全局 get_state() 而非传入 facade | build.py:285-290 |
| P2-14 | 首轮 confirm_all「只重嵌受影响表」退化为全库重嵌 | semantic/store.py:305-323 |
| ✅ P2-15 | 表级字段归档写入后读不出（field_history 只查 column）；finalize 不落盘不归档 | storage.py:754-775、api/knowledge.py:626-649 |
| P2-16 | 嵌入用量记账恒 0（确认阶段真实消耗不记账）；增量路径绕过限流与重试（一次 429 即 degraded） | build.py:933、annotator.py:1345/1611 |
| P2-17 | sync 空工件内联全量：无进度无 kb_status 变化，tick 串行阻塞其他连接 | jobs.py:466-489 |
| P2-18 | 构建中协作取消在长 LLM 调用期间不生效（busy 帧 check_cancel=False，最长 ~10 分钟无响应） | annotator.py:915-919 |
| P2-19 | 前端构建 SSE 无超时无 AbortController（同对话流旧病）；sync 进度 30% 后冻结 | knowledge.ts:137-172、api/knowledge.py:411-431 |
| P2-20 | degraded_phases 后端发了前端不读（单相失败无提示） | jobs.py:313-318、knowledge.ts:26-37 |
| P2-21 | busy 全局无连接维度（A 构建时 B 的知识库页/图谱全禁） | knowledge.ts:193-253 |
| P2-22 | 构建期间审查页显示幻影数据（后端已清提案、前端显示上轮残留且无告知） | KnowledgeReview.tsx:778 |
| P2-23 | 多写端点缺 begin_op 互斥（/reject、/graph/confirm 等） | api/knowledge.py:960-975、786-829 |
| ✅ P2-24 | HistoryDrawer 客户端过滤 200 条截尾 + batch-review 留痕正则失配 | KbHistoryDrawer.tsx:60-95 |
| P2-25 | apply 步骤②隐性放大（每次 rejectComment 全量 load + 全 store 订阅重渲染） | knowledge.ts:260-263、ReviewLens.tsx:185 |
| P2-26 | ReviewLens 挂载并发拉全部 pending 表 baseline，无上限无批接口 | ReviewLens.tsx:225-235 |
| P2-27 | 后台重嵌任务丢失无补偿（reembed_if_needed 只在指纹/维度变化时重嵌） | api/knowledge.py:610-617 |
| P2-28 | 旧 edges 表迁移静默丢边（cols 缺失即 continue，.bak 无提示） | storage.py:404 |
| P2-29 | KbHistoryDrawer 拉取不用服务端 connection 参数 | KbHistoryDrawer.tsx:92-95 |

## 七、P3 · 小问题 / 死代码（open，修复时顺手清）

- 死代码：`api/knowledge.ts confirmAll` 无调用；KnowledgeReview 批量按钮永不可见；`build.py all_samples`/重复 touched；`View 'graph'` 死分支；`BuildProgress` 公共类型缺 phases/kind 与实载荷不符。
- docstring/注释谎言：batch_review 谎称自动收尾（`_maybe_finalize` 从未被它调用，连接可能卡 pending_review）；`/reject` 注释还是旧语义；pillMode 注释与实现不符；`filters`/`constants` 阶段未注册 PHASES（stage 标签英文裸串）。
- 交互小刺：cancelBuild 无异常处理；失败卡出口绕路；KbBuildGate 双请求；doSync 在 pending_review 未禁用吃 409；同步期间重建按钮文案错；`key.split('.')` 脆弱；EdgeCard key 可碰撞；单字段确认对不存在表/列静默返回 0；进度端点浅拷贝撕裂帧。
- 轻微：facade.clear 不清 _round（取消构建后幽灵 round）；两套 AI 配置首检并存。

---

## 八、现场观察（非 bug）

演示库处于「构建了但从未确认」状态（71 条文档草案 + 13 个 draft 标签、0 confirmed、
version 0、从未同步）；mdm 库健康（全确认、version 7、近期同步）。演示库上标签路由、
confirmed-only 边全部哑火，叠加 P0-B（放弃清草案）后演示体验差是复合结果。

## 九、已验证不是问题的点（修复时别误伤）

- `merge_draft_edges` 与已确认边同列对同基数的 draft 去重——符合设计。
- `sync()`/`needs_sync`/`_touched_for_sync` 均在指纹比较前 ensure_loaded——增量路径无 P0-A 问题。
- `begin_op` 检查+占坑同步原子——SyncLoop 与手动 sync/confirm/discard 互斥本身成立。
- gather 四阶段异常隔离正确（各协程自吞 Exception，CancelledError 穿透）。
- 红边判定本体与设计一致（diff_active ∧ 非 user ∧ 未 pin ∧ 端点存 ∧ 不在队列/base）。
- apply 链四端点与后端一一对应，409 幂等跳过 + 120s 遮罩超时 + 失败刷新 overview 均在。

## 修复顺序建议（供拍板）

1. **P0-A/B/C**（ensure_loaded 入口 + 队列销毁推迟 + 空 schema 守卫）——止住数据丢失
2. **P0-D/E**（409 回退原状态 + 版本作废降级引导）——止住状态卡死
3. **红边闭环**（P1-1/2/3 + P2-1 pin 持久化 + P1-5 diff 基线持久化）——三色 diff 真正落地
4. **自动同步可见性**（P1-11/12/13）——审核入口不再隐身
5. **向量两件**（P1-9/10）——检索质量
6. 其余 P2/P3 按「修复时顺手清」原则随批处理
