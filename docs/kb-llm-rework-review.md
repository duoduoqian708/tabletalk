# kb-llm-rework 实施审查报告

> 审查对象：`docs/kb-llm-rework-plan.md`（知识库构建 × LLM 记账统一改造）
> 审查方式：A 静态代码核查（33 子步逐一）→ B 回归测试 → C 运行时端到端冒烟（隔离实例 + 本地假 OpenAI 端点，零外部成本）
> 日期：2026-08-25 · 结论三态：✅ 通过 / ⚠️ 偏差 / ❌ 缺失

---

## 0. 总览

| 段 | 内容 | 结论 | 问题 |
|---|---|---|---|
| 1 | LLM 统一记账拦截器 | ✅（1 个低危偏差） | P1 |
| 2 | 推理探测 + 落库 | ✅ | — |
| 3 | 采样严格授权门控 | ✅ | — |
| 4 | 逐表注释两套模板 | ✅ | — |
| 5 | 图谱自检并入第二轮 | ✅ | — |
| 6 | 领域划分 + 自检 | ✅ | — |
| 7 | 进度条重构 | ✅ | — |
| 8 | 敏感名单交互化 | ✅ | X2 备注 |
| 9 | 全量验证 | ⚠️ 大部分过 | P2 |

**问题计数**：计划内 2（P1 低、P2 中）· 计划外 5（X3 中，其余低/信息）

**B 层结果**：pytest **450 passed, 8 skipped**（Docker 门控集成跳过）· 前端 typecheck 干净 · build 成功
**C 层结果**：真跑 KB 构建（15 表）→ `llm_call_log`/`cost_log` 出现 kb-annotation×15、kb-tags×2、kb-graph×1；egress 审计 18 条按模型/模式聚合正确；chat 单请求恰好一组记账；流中断 finally 兜底记账有效；敏感名单整表+列过滤在 KB 构建产物中生效。

---

## 1. 分段详查记录（证据摘要）

### 段1 记账拦截器 ✅
- `gateway.py:110/156`：chat/chat_stream 均有 `ctx` 参数；`_record_llm` 无条件调用；ctx=None 默认 skill="llm"
- chat 失败路径 L151-153：except 中记 `ok=False, note=str(exc)` ✅「失败也记」
- chat_stream 整段 try/finally（L162-226），mock 与 cloud 分支都覆盖
- 全局 grep：`LlmCallLog/CostTracker/verdict="egress"` 写点仅在 gateway（loop/preflight/report/ai_review/compress 五处已删干净）；`store.py:337 _log_embedding_usage` 是 embedding 直写（embedding 不走 gateway，合理补充路径，skill=embedding）
- annotator 6 处 provider.chat 全带 ctx 且 skill 正确（kb-annotation/kb-tags/kb-graph）、candidate_tables 如实（逐表=[table_name]、划分=全表、verify=related_tables）
- preflight ctx(status=egress-intent)、compress(egress-compress)、ai_review(egress-review) ✅；关键字快判路径无 egress 写点
- report 三处调用共用 ctx(skill=report)；narration 调用点 L395 `{**ctx, "include_data": True}` 覆盖正确
- `/ai/test` 两处 allow_fallback=False（api/ai.py:64,76）

### 段2 推理探测落库 ✅
- `_detect_reasoning_effort`（api/ai.py:212）：high→medium→low 降档探测，全拒返回 None
- `ModelConfig.capabilities`（settings.py:37）默认 None 兼容旧数据
- `/ai/test` 成功后 `_persist_capabilities` 写回 ai_models（L84-86, 237）；model_id 不在列表则跳过（临时配置不落库）
- `_kb_reason_provider_cfg`（annotator.py:27）：effort ∈ low|medium|high 用档位，否则 "thinking"；mock 不受影响

### 段3 采样授权 ✅
- 构建入口（api/knowledge.py:149-158）：仅 `include_samples and kb_sample_rows>0` 抽样
- 手动 `/sync`（L261-268）与 SyncLoop.tick（jobs.py:260-268）同构门控
- 空 samples 安全：truncate_samples({})→{}、graph 纯 FK、mock 空守卫（C 层零采样构建实测通过）
- `test_kb_zero_sample.py` 4 例齐全

### 段4 双模板 ✅
- `_annotation_prompt_sampled/_unsampled` 按 table_samples 分流（annotator.py:287-293）；无采样版纯结构不提 values
- 无采样硬闸：程序级 pop values/example（L308-313），不只靠 prompt
- mock 对齐：`_mock_table_comments_from_ddl` 按 samples 增减 values/example
- 测试：test_two_templates_prompt_contract 等 3 例钉死

### 段5 图谱自检 ✅
- 第二轮池 = 未被全局覆盖的程序候选 ∪ 首轮 confidence≠high（L975-982，self_check 开时）
- verify prompt「不得新增列表之外的关系」（L1022）
- self_check 默认取运行时 kb_build_self_check=True（L953-954）；关时池只含程序候选；合并时自检开仅保留全局高置信（L1052-1054）；最终四元组+反向去重
- API body.self_check → store.build → annotate_domain 链路透传

### 段6 领域划分 ✅
- 画像：库注释 > 阶段一草案（_stage1_table_desc），噪音列过滤，不显字段类型（L456-487）
- target=max(3, round(√N))，lo~hi 注入 prompt（L658-659）
- 一轮划分 + 一轮审校（unchanged:true 协议，L509/L629/L719-726）
- 落库 upsert_tags + assign_table_tags 多打标（L731-745）；**desc_drafts 在 annotator 中零写点**（grep 证实）
- _normalize_domains 兜底：域数上界/孤表挂靠/兜底单域
- C 层实测：15 表 → 13 域、86 docs + 13 tags 草案，pending_review 等人工确认

### 段7 进度条 ✅
- PHASES steps 元数据（jobs.py:26-38）+ report 扩展 step/step_index/step_total/steps
- 子步上报：阶段一逐表 i/N+表名（annotator.py:343-349）、tags partition→selfcheck、graph global→verify
- `_overall` PHASE_WINDOW 映射（annotate 16-45/tags 45-60/graph 60-78，窗口衔接单调）
- error_at {phase, step}（jobs.py:205）
- 前端 BuildPhase 可选字段兼容旧帧 + KbBuildGate chips 渲染
- 注：SSE build/events 活动帧未实际捕获（mock 秒完错过），结构由代码与 test_kb_progress 单测覆盖

### 段8 敏感名单 ✅
- sensitive 支持 str(glob)|dict({table, columns[]}) 混排（sensitive.py:17-39）
- dict 无 columns/空 = 整表排除；FK 悬空双向清理（L68-75）
- ConnectionModal：下拉选表 + 列勾选 checkbox + 「整表」开关；旧 glob chip 带 glob 标签可删；旧逗号字符串草稿兼容
- C 层实测：保存 `{customers:[email]},{suppliers}` → rebuild 后 95 条 docs 零提及 suppliers/email

### 段9 全量验证 ⚠️
- pytest 450 passed ✅ · typecheck/build ✅ · egress/cost 报表出现 kb-* 记录 ✅（真实 gateway 驱动下）
- ⚠️ P2：mock 场景构建零记账（见下）

---

## 2. 问题清单

### 计划内（P）

**P1 · chat_stream 中断记账 ok 恒为 True**（⚠️ 低）
- 位置：`backend/app/ai/gateway.py:226`
- 现象：finally 里 `_record_llm(..., ok=True)` 硬编码。流异常/客户端中断时也记成功；而 chat 的 except 路径记 `ok=False`。行为不一致，失败排查时 verdict 失真（token=0 可间接识别）。
- 建议：finally 捕获是否发生异常（如 `sys.exc_info()` 或标志位），中断时记 ok=False/note=interrupted。
- 复现：消费 1 chunk 后 aclose 生成器 → llm_call_log 新行 response_json 为空但无 error 标记。

**P2 · mock 场景 KB 构建零记账，段9 验收存在 mock 缺口**（⚠️ 中）
- 位置：`backend/app/knowledge/annotator.py`（各阶段 mock 分支直接本地生成，不经 gateway，如 L256-258/L677-682/L905-910）
- 现象：mock 下跑构建，llm_call_log/cost_log/egress 全空——「成本页出现 kb-* 记录」这条验收只在配置真实模型时可满足；且自动化测试未钉死 kb-* skill 的 LLM 记账行（test_kb_build_audit 只测构建动作审计）。若未来有人改动 annotator 的 provider.chat 调用丢失 ctx，现有测试无法发现。
- 建议：加一个用 fake transport/provider 驱动的回归测试，断言构建后 llm_call_log 出现 kb-annotation/kb-tags/kb-graph 各 ≥1 行（本次审查已用本地假 OpenAI 端点人工验证闭环成立）。

### 计划外（X）

**X3 · 数据目录迁移守卫恒真，自定义 TABLETALK_DATA_DIR 必被灌入 legacy 数据**（中）
- 位置：`backend/app/config.py:125-127`
- 现象：`default_new = os.environ.get("TABLETALK_DATA_DIR")` 与 `self.data_dir` 读同一个环境变量，`str(self.data_dir)==str(default_new)` 恒为真。docstring 承诺「用户显式指定 TABLETALK_DATA_DIR 时不迁移」，实际任何全新自定义目录首启都会 copytree `~/.cleared`（含连接配置与 API key）进来。本次冒烟首次启动即复现（临时目录凭空出现 2 个连接 + cloud key）。多实例/隔离测试场景会意外拿到生产凭证。
- 建议：守卫改为与硬编码默认值比较：`if str(self.data_dir) == str(_expand("~/.tabletalk")):`，或引入独立开关 env。
- 复现：`TABLETALK_DATA_DIR=/tmp/x uvicorn ...`（/tmp/x 不存在且 ~/.cleared 存在）→ 启动后 /tmp/x 出现迁移数据。
- 备注：迁移是复制语义，源目录完好，无数据损失。

**X1 · ModelConfig 反序列化无未知字段容错**（低）
- 位置：`backend/app/core/settings.py:412`
- 现象：`ModelConfig(**m)` 对 settings.json 里的多余键直接 TypeError；同函数内 Policy 构造有字段过滤（L420），风格不一致。手工编辑配置多写一个键即启动崩溃。
- 建议：仿照 Policy 加 `if k in ModelConfig.__dataclass_fields__` 过滤。

**X2 · sensitive 过滤大小写归一不一致**（低）
- 位置：`backend/app/core/sensitive.py:19-23`
- 现象：glob 分支 lower() 归一匹配；dict 精确分支 `entry.get("table") != tname` 区分大小写。UI 下拉选表（名字来自 schema 本身）无碍；手输表名大小写不符时静默不过滤。
- 建议：dict 分支同样 lower() 比较。

**X4 · 前端主包体积**（信息）
- `dist/assets/index-*.js` 961KB 超 chunk 警告（既有问题，three.js/codemirror 大头），可考虑 manualChunks 分包。

**X5 · 测试资源告警**（信息）
- `tests/api/test_models.py::test_detect_reasoning_streaming_finds_reasoning` 有 `coroutine 'aclose' was never awaited` RuntimeWarning，流式响应未清理。

### 核查说明（非问题）
- `/connections/{id}/schema` 不过滤 sensitive 是设计意图：过滤点在 KB 构建（api/knowledge.py `_kb_schema`）与 AI 上下文（ai/context.py）；schema API 需全量结构供前端勾选敏感列。审查初期曾误判为漏洞，已澄清。
- graph 仅 1 次 LLM 调用属正常：全局扫描全部高置信且程序候选被覆盖时第二轮候选池为空（annotator.py:984-989 提前返回）。
- chat 冒烟 token=0：MockProvider 不产 usage，「无 usage 也记」符合拦截器设计。

---

## 3. 冒烟环境与方法备注

- 隔离实例：`TABLETALK_DATA_DIR=<tmp>/kb-smoke` + 端口 8791，未触碰 `.tabletalk-data/` 与运行中的 8777
- 记账闭环验证用本地假 OpenAI 端点（127.0.0.1:8799 返回确定性 JSON 产物），零外部 API 成本；冒烟产生的数据留在系统临时目录，未清理
- 测试 diff 抽查：13 个被改测试文件删除行均为 mock provider 加 ctx 参数的签名适配与注释更新，egress 契约测试改为经真实 gateway 拦截器驱动（更严格而非放水）

## 4. 遗留提醒（来自原计划文档，仍然有效）

- 工作区大量未提交改动（含 c3 会话早期产物），验收后需整理提交
- 2D 图小节点命中带 LINK_BAND 改动仍待拍板（原计划开放点）

---

## 5. 复核结论（作者回应，2026-08-25）

逐条核对源码后：**审查结论整体合理，证据扎实（行号精确）；计划内 2 项 + 计划外 X1/X2/X3 均属实，P2 需二分**。处置如下：

| 编号 | 复核 | 处置 | 状态 |
|---|---|---|---|
| **P1** | ✅ 属实：`gateway.py` chat_stream `finally` 硬编码 `ok=True`，与 chat 的 except 记 `ok=False` 不一致 | 已修：finally 按退出状态记 ok——正常完成 ok=True；流中异常 ok=False+note；客户端提前 `aclose`（GeneratorExit）ok=False+note="interrupted" | ✅ 修复 + 2 回归测试 |
| **P2** | ⚠️ 部分属实，需二分：① KB **mock 本地分支**（`_mock_*`）不经 gateway 是**正确行为**——它不产生任何 LLM 调用事件，零记账属设计（"含 mock 全记"指 `MockProvider` 那条路径）；② 审查建议的回归测试**已在段9 交付**：`tests/knowledge/test_kb_llm_integration.py` 强制走真实 LLMGateway + 确定性响应，断言 `llm_call_log` 出现 kb-annotation/tags/graph 各 ≥1，防未来丢 ctx 回归 | ② 已闭环；真正缺口是**验收措辞**（mock 下成本页无 kb-*）——已在 plan doc 段9 注明 | ✅ 已覆盖 |
| **X3** | ✅ 属实：`config.py:125-127` 与 env 同源比较恒真，自定义 `TABLETALK_DATA_DIR` 首启必被灌入 `~/.cleared`（连接 + API key）。**既有 bug，非本次改造引入**，但隔离/测试场景确会拿到生产凭证 | 已修：守卫改与字面 `~/.tabletalk` 比较；实测隔离目录首启连接数 0 | ✅ 修复 + 实测 |
| **X1** | ✅ 属实：`settings.py:412` `ModelConfig(**m)` 无字段容错，与 Policy 过滤风格不一致 | 已修：白名单过滤（Model + Embedding） | ✅ 修复 |
| **X2** | ✅ 属实：sensitive 精确名分支区分大小写（glob 分支已 lower） | 已修：dict 分支统一小写比较 + 测试 | ✅ 修复 |
| **X4** | ✅ 信息级，既有包体积问题 | 未动（分包独立优化，超出本次范围） | — |
| **X5** | ✅ 信息级；已 `break` 化 + 显式 `await r.aclose()`，残留为 httpx MockTransport GC 伪告警 | 维持信息级 | — |
| 非问题澄清 ×3 | ✅ 全部正确（schema API 不过滤=设计、graph 单调用=高置信覆盖、mock chat token=0=设计） | — | ✅ |

**修复后回归**：pytest **453 passed / 8 skipped**（+3：P1×2、X2×1）· typecheck/build 不变。
