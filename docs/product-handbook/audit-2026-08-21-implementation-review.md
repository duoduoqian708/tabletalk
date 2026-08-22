# 实施审核报告 · 逐项深查版（v2）

> 日期：2026-08-21（复审） · 审核对象：commit `e879773` + 其后的工作区改动
> 方法：对照 product-handbook 02-07 逐项核对源码（整文件阅读而非抽样 grep），逐条对照验收标准；每查完一项即写入本文档。
> 状态图例：✅ 已落实 · ◐ 部分落实 · ✗ 未落实 · ⚠ 偏差（做法与文档不一致）
> 分类口径：**代码问题**（改代码）/ **文档问题**（改手册）/ **计划内未到期**（不算偏差）

---

## 一、02 功能需求详规 · 逐项复审

### A1 拦截可解释 -- ✅（细节比 v1 结论多两处小瑕疵）

**逐条验收核对**：

1. **`reasons: [{rule_id, message, objects}]` 结构化** ✅ -- `safety/models.py` `Assessment.reasons`（另含文档未要求的 `message_en`，双语超出规格）；`rules.py` `aggregate()` 按"最严重 verdict 聚合"生成（`picked[:3]` 截前 3 条，防刷屏，合理取舍）。每条规则 R1-R7 都带 `rule_id / 中英文案 / objects(表名)`。
2. **审计 JSONL 每条带 reasons** ✅ -- `audit/logger.py`：block/review 必写 reasons，且**空 reasons 时合成兜底条目**（rule_id="unknown"）--"100% BLOCK/REVIEW 含非空 reasons"这条验收在 logger 层被结构性保证，不依赖调用方自觉，好设计。ALLOW 记录不带 reasons（合理，验收也只要求 BLOCK/REVIEW）。
3. **前端弹层按列表渲染** ✅ -- `AiRail.tsx:474/508`：REVIEW 风险面板与 BLOCK 面板均逐条渲染 `rule_id 徽标 + 文案 + 涉及对象`；文案取 `t('gate.rule.<id>')` 词典优先、缺失时回退后端 `message/message_en`（动态策略/成本类 rule_id 无词典键，走回退，行为正确）。`AuditPage.tsx:385` 同样逐条渲染。
4. **规则文案中英双语** ✅ -- `rules.py` `_MSGS` 10 条规则内置中英双份；`locales/zh-CN.ts` + `en-US.ts` 的 `gate.rule.*` 与后端 rule_id 一一对应（含 `read-only` 共 11 键）。
5. **pytest 覆盖每条规则的 reason 生成** ◐ -- `tests/safety/test_rules.py::test_reasons_structured_per_rule` 覆盖 6 条规则（dml-no-where / dml-confirm / ddl-manual / parse-failure / multi-statement / ddl-ai）+ 双语断言 + 非空断言；`test_gate.py` 断言所有 BLOCK/REVIEW 输出结构化非空。**未覆盖**：`tcl-confirm`、`unknown-fallback`（均为低风险 REVIEW 规则）。小缺口，补两行用例即可。

**本次深查新发现（v1 未发现的小瑕疵，记入 A5/小项）**：
- `api/query.py:157`：成本升档分支写 `assessment.tier = Tier.READ`，但该文件只 `from app.safety.models import Origin, Verdict`，**`Tier` 未导入** -- 真实执行到此会抛 NameError，被外层 `except Exception: pass` 吞掉。恰好赋值顺序（verdict/reasons 在前、tier 在后）使升档 verdict 仍生效，功能上侥幸无损，但这说明该分支从未被测试覆盖到 tier 断言。**代码问题（小，与 A5 合并修）**。
- `rules.py:75` `objects` 对 parse-failure 取 `tables or [parse_error][:1]` -- 把错误文本当"对象"展示，语义略怪但无害。

**结论**：✅ 符合验收。遗留两处小项（Tier 导入、tcl/unknown 两条规则的 reason 用例）。

### A2 策略即配置 -- ◐（v1 结论修正：API 写通路整个是断的，且波及 B4）

**逐条验收核对**：

1. **策略对象三类** ✅ -- `core/settings.py` `Policy`（version / updated_by / updated_at 齐备）：`table_rules`（表->allow/review/block）、`pattern_rules`（[{id, description, action}]）、`threshold`（默认 10 万，与文档一致）。持久化到 settings.json ✓，PUT 时自动 version+1、写 updated_at ✓。
2. **闸门读取策略、reason 标注"策略 vN"** ◐ -- `api/query.py:96-137`：表级规则命中生成 `policy-table-<表>` reason（含"命中策略 vN"）✓；每次请求实时读 `state.runtime.get().policy`，**修改即时生效无需重启** ✓。但实现质量有四处折扣：
   - **模式级是空壳**：引擎只硬编码识别一个 pattern id（`delete-requires-time`），`action`/`description` 字段被忽略，其余 pattern id 全部无效。文档设想的"语句形态规则"通用引擎不存在，是单条 demo 规则。
   - **表级 ALLOW 不能降档**：`act=="allow"` 且当前 REVIEW/BLOCK 时直接跳过（保守方向，安全但与"表级优先级最高"的文档语义不符）。
   - 整段包在 `try/except Exception: pass` 里，策略引擎自身出错会静默失效（与 A1 发现的 `Tier` 未导入同一类问题）。
   - pattern 级只对"当前 verdict 仍是 REVIEW"的语句生效（query.py:121），BLOCK 语句不再叠加 pattern 判定--影响极小。
3. **settings API 暴露 policy** ◐ -- `GET /settings` **包含** policy（v1 说"不暴露"有误，GET 是有的）。但 **`PUT /settings` 写不进去**：`api/settings.py` 的 `SettingsUpdate` pydantic 模型**没有 `policy` 字段**，请求体带着也会被静默丢弃（已实测验证：`model_dump` 后 policy/privacy_mode 均为空）。商店层 `SettingsStore.update()` 明明实现了完整的 policy 更新逻辑（版本自增、threshold 同步），却因 API 模型缺字段永远收不到。**DBA 唯一改策略的方式是手改 data_dir/settings.json。**
4. **UI 策略页** ✗ -- SettingsDrawer 分节为 dsm/theme/llm/skills/safety/privacy/general，safety 页只有 maxRows/poolSize 两个输入框，无任何 policy 配置面。文档验收"设置抽屉新增策略页"未实现。
5. **做实 threshold 一族** ✅ -- `policy.threshold` 被 A5 成本检查真实读取（query.py:143），PUT policy 时同步回写 `gate_review_threshold`（settings.py:514-521）。债 #1 确认已做实。
6. **测试覆盖** ✗ -- 全部 backend/tests 中 grep 不到任何 policy 用例；执行层的表级/pattern 升档逻辑无测试。

**连带发现（记入 B4）**：`SettingsUpdate` 同样缺 `privacy_mode` 字段，而前端隐私页的三档 select 恰恰 `PUT {privacy_mode}`--**实测被丢弃，档位切换是假开关**（前端仅本地 state 乐观更新，刷新即回退）。B4 的"档位切换即时生效"验收实际不成立。

**归类**：**代码问题（高优先）**--一行字段补齐（SettingsUpdate 加 `policy`/`privacy_mode` 两字段）即可打通 API 写通路，商店层现成；策略 UI 页 + 模式级引擎通用化 + 表级/pattern 升档测试为后续补齐项。文档设计本身无问题。

### A3 爆炸半径 -- ✅（P1 卡片完整；星图联动为 P2 计划内未做，维持 v1 判断）

**逐条验收核对**：

1. **direct：触及表 + COUNT 预估** ✅ -- `safety/blast.py`：`preview_rows`（与 REVIEW 预览同一 COUNT 通路，`api/query.py:210` 传入）挂在第一张直接表上；无预览时 `estimated_rows: null`（测试 `test_blast_with_none_preview` 覆盖）。
2. **cascade：沿 FK 2 跳、防爆炸** ✅ -- BFS 以 FK 边（`kind=="fk"`，**值重叠边正确排除**）扩展，2 跳封顶（`test_two_hop_limit` 验证第 3 跳 categories 不出现），另有未在文档写明的 8 表截断（`cascade[:8]`，合理防御）。每项带 `via / fk / hops / has_fk`。demo 库 orders -> order_items/payments 的验收有 API 级测试（`test_query_review_returns_blast_via_api`）。
3. **constraints：命中 FK 名** ✅ -- 直接表关联的 FK 全列出（上限 10 条；schema 无显式约束名时按文档格式 `from.col->to.col` 合成）。与 `_build_graph` 实际边结构核对一致。
4. **确认弹层渲染** ✅ -- `AiRail.BlastView`：direct chips（含 ≈行数）+ cascade 路径（FK 标注）+ constraints 三段齐全，可折叠。
5. **星图联动（P2）** ✗ 未做 -- 计划内未到期，不算偏差（但 commit 声称"A3 完成"范围有水分，维持 v1 提示）。

**深查新发现（均为小项）**：
- blast 取 `state.knowledge._graph` 私有属性绕过公共接口，KB 内部结构一变即断（知识库已有 `graph()` 公共方法可走）。**代码问题（小）**。
- 文档"标注外键约束存在/不存在"：实现里 cascade 只沿 FK 边走，`has_fk` 恒为 true，"不存在"分支实际不可达（文档设想的 FK 缺失推断边场景未做）。**文档问题（小）**：把这句改为"级联路径均为显式 FK"更贴近现实。
- BlastView 双语用 `locale.startsWith('zh')` 内联三元而非 i18n 词典（功能等价，风格不一致）。
- cascade 表的行数未估算（文档只要求直接表行数，不算缺口）。

**归类**：P1 部分 ✅ 符合验收；小项三条（私有属性、has_fk 死字段、内联双语）。

### A4 回滚剧本 -- ◐（v1 的 ✅ 需降级：三个验收子项缺失）

**逐条验收核对**：

1. **UPDATE/DELETE 生成备份导出 + 逆向模板** ✅ -- `safety/rollback.py`：同 WHERE 的 `SELECT *` 备份 + 逆向语句模板（UPDATE 为注释模板提示用备份回填，DELETE 为 INSERT 回放模板）。仅处理首条语句，解析失败返回 None。
2. **INSERT 主键回放建议 / 明确说明无法自动回滚** ✅ -- 生成 `DELETE ... WHERE <主键>=<插入值> -- 需填主键` + note "INSERT 无法自动回滚，需按主键回放删除"。
3. **确认弹层附带 + 一键复制** ◐ -- `AiRail.RollbackView`：备份/回滚两段各带"复制"按钮 ✓；**"存为脚本文件"未实现**（文档功能逻辑明确要求，验收未直接提但属功能缺口）。
4. **剧本自身过闸门校验** ◐（实际是死代码）-- `api/query.py:218-222` 有校验逻辑（BLOCK 则加注），但 `rollback.py` 生成的 rollback_sql **全部以 `--` 注释开头**，`startswith("--")` 分支使校验永远不会触发。防御机制在但为死路径；且**无任何测试**（全 tests 目录 grep 不到 rollback 用例）--v1 说"建议补断言"，实为零覆盖。
5. **审计关联 rollback_ref** ✗ -- 全后端 grep 不到 `rollback_ref`。文档"审计记录关联 rollback_ref（导出脚本的位置或语句哈希）"未实现，审计日志无法回溯某次执行的回滚剧本。
6. **"不承诺时光机"文案** ✅ -- note "回滚剧本仅供参考，执行前请核对"。

**归类**：**代码问题**：rollback_ref 审计字段、存为脚本、剧本过闸的测试断言（并让过闸校验真正可达，比如对 backup_sql 也评估）。核心生成逻辑本身正确。

### A5 成本防护 -- ◐（v1 的 ✅ 需降级：机制在、可配置性与可验证性断裂，零测试）

**逐条验收核对**：

1. **`DialectAdapter.explain()` 三方言** ✅ -- sqlite：SCAN/SEARCH 判别 + `is_scan` 标记（行数由调用方结合 row_count 估算，与修订版文档方案一致）；postgres：`EXPLAIN (FORMAT JSON)` 取 `Plan Rows` ✓；mysql：取行中"任意正整数"的最大值当行数（实际是碰运气：MySQL EXPLAIN 的 `id` 列也是正整数，小表时估出 1-2 这类无害小值，`is_scan` 用 `"ALL" in detail` 子串判断有误报可能）。**代码问题（小）**：MySQL 按列序取 rows（index 9）而非任意整数。
2. **超阈值升 REVIEW + reason 标注** ◐ -- `api/query.py:139-160`：`cost-threshold` reason（含"预计扫描约 N 行"）✓，仅对 ALLOW 生效（含只读，符合"只读同样适用"）✓。但存在四个问题：
   - **阈值实际不可配置**：`thr = policy.threshold or gate_review_threshold`，而 `policy.threshold` 恒有默认值 100000 且 **policy 无法经 API 修改**（见 A2）--`gate_review_threshold` 虽可 PUT 却被 policy 恒遮蔽。**成本阈值事实上硬编码 100k，文档"阈值级配置（策略 A2）"在配置面上不通。**债 #1"做实 threshold"由此打了折扣：读取做实了，配置路径断了。
   - **`Tier` 未导入的 NameError**（A1 已记）：`query.py:157` `assessment.tier = Tier.READ` 抛 NameError 被 `except Exception: pass` 吞掉。侥幸的是 verdict/reasons 在其前已赋值、且读查询 tier 本就是 READ，功能侥幸无损。
   - **审计不含成本估算字段**：验收要求"审计含成本估算字段"，audit.log 的调用里没有任何成本/估算字段；代码注释还谎称"已在 _estimate_cost 内审计降级"（该函数没有任何审计调用）。
   - **估算回退过于粗暴**：SQLite SCAN 时按"表名是否为 SQL 子串"找 row_count，找不到就**取全库最大表行数**兜底--别名/引号包裹的表名会 miss 然后被误按最大表估算（方向是过度保守，可用性问题非安全问题）。
3. **EXPLAIN 失败降级不阻塞** ✅ -- 全链路 try/except，失败返回 None 放行。但"审计记录降级原因"未实现（同上，无审计调用）。
4. **SEARCH 不升档** ✅ -- 非 SCAN 不估算（文档"索引选择性粗估"未做，SEARCH 直接跳过，效果上满足"不升档"验收）。
5. **测试覆盖** ✗ -- 全 tests 目录无任何 cost/explain/阈值用例（grep 证实）；demo 库行数远小于 100k 且阈值不可配，**该验收在当前代码上无法复现**。

**归类**：**代码问题**：①阈值配置通路（依赖 A2 的 policy PUT 修复）；②MySQL explain 取列；③Tier 导入；④审计成本字段 + 降级原因；⑤补测试（含降低阈值的用例）。**文档问题（小）**："SEARCH 时按索引选择性粗估"实际未做且无必要（不升档即达标），建议删掉这句避免误导。

### B1 出网清单 -- ◐（主链路 ✅；验收"所有模型调用均有清单"不成立，另有两条新发现）

**逐条验收核对**：

1. **清单对象字段** ✅ -- `ai/manifest.py` `build_manifest`：tables / kb_docs / history_turns / include_data / redactions / mode / ts / model / provider，与文档规格逐字段一致；`manifest_to_human` 生成"3 张表结构 · 2 条知识库注释 · 6 轮历史 · 无行数据"可读文案（中英）。
2. **chat 主链路** ✅ -- `loop.py:161-188`：context 组装后 build_manifest -> 审计 egress 事件（manifest 全量入 JSONL）-> SSE `manifest` 事件；问题库命中分支也合成 provider=local 的清单（零模型调用也如实枚举，好）。B3 联动：敏感表的清单表名也代号化（context.py:148-158），清单本身不泄密 ✓。
3. **report 链路** ✅（带已知瑕疵）-- report.py：build_manifest + 审计 + SSE ✓；每章子查询独立过闸门 + 审计（挂 report_id、source=report，可回溯）✓。**瑕疵**：①异常兜底 manifest 硬编码 `mode:"standard"`（v1 已记）；②narration 是同一轮的**第二次**模型调用（聚合行数据直出，见 B2），清单只覆盖第一次，include_data=true 勉强涵盖但 tables 是上下文候选表而非各章 SQL 实际表集。
4. **UI** ✅ -- `AiRail.ManifestView`：折叠头（人类可读摘要）+ 展开（逐字段明细），流式中保持可展开 ✓。
5. **清单与 payload 一致 + 测试** ✅ -- `tests/test_egress_manifest.py`：字段/类型断言、tables 与 candidate_tables 一致性、SSE 事件与审计 egress 条目一致性（4+ 用例）。
6. **"所有模型调用（chat / selection / intent / report / test）均有清单"** ✗ -- 三处不在清单内：
   - **intent 分类是真实模型调用但零清单零审计**：`intent.py` `classify_mode`/`classify_tags` 直连 `provider.chat()`，用户问题原文出网，无 manifest、无 egress 审计（且发生在主清单生成**之前**，每轮 chat 都有 1-2 次此类调用）。
   - `/ai/test` 连通性测试：无清单（内容仅 ping，风险低，但验收口径未达）。
   - **`/ai/selection` 已被替换为硬编码 canned 回复**（`_SELECTION_REPLIES` 三条固定文案，不再调模型）--不算"无清单"问题（压根没有模型调用），但**功能性降级**：原来"选中单元格 -> AI 解读"变成了死文案，与产品的 AI-first 定位不符。**代码问题（中）**。
7. **出网可枚举率** ◐ -- 有审计的调用（chat/report/问题库）可 100% 枚举 ✓；intent/test 未入枚举，严格口径下"枚举率 100%"不成立。

**深查新发现（B4 相关，此处先记）**：`intent.py` 用 `rt.provider_config()` 而非 `resolve_provider_cfg` 构建 provider--**严格档（strict）的强制 mock 只作用于 loop/report，intent 分类在云模型配置下照样出网**（用户问题原文）。B4"严格档完全离线"验收在此被打穿。

**归类**：**代码问题**：①intent/test 补清单与审计（或并入单管道）；②intent 调用改走 `resolve_provider_cfg`（B4 一并修）；③`/ai/selection` 恢复真实模型调用（或明确降级并在文档记决策）；④report 兜底 manifest 硬编码档位。文档设计无问题。

### B2 本地脱敏网关 -- ⚠◐（引擎本体实测达标；接线有 4 处缺口，其中 fail-open 最严重）

**逐条验收核对**：

1. **规则式脱敏层（正则 + 列语义 + 列显式）** ✅ -- `safety/redact.py`：5 类正则（EMAIL/PHONE/ID/CARD/IP）、5 组列名语义 hint、`table.column` glob 显式名单（挂在连接配置 `sensitive` 字段，connections API 可写）。列级优先于正则 ✓。
2. **确定性 tokenization + 本地盐** ✅ -- HMAC-SHA256 截 8 hex，盐 `data_dir/redact.key`（chmod 600）。**本次实测**：同值同 token ✓（`[PHONE_46a4ca56]` 稳定）、盐复读稳定 ✓、token 可逆向还原（映射仅内存，逆向只服务本地展示，不出网）✓。
3. **引擎质量实测** ✅ -- 1000 行三列脱敏 **14.9ms**（验收 <50ms 达标，含 1000 次 HMAC）；脱敏输出扫描原始值（姓名/手机号/邮箱）**零命中** ✓；`redact_text` 对"问题里贴敏感串"的打码功能本身可用 ✓。
4. **seed 敏感列** ✅ -- demo 库 customers 表有 email/phone 列，验收"有米下锅"成立。
5. **标准档聚合行出网前脱敏** ✅ -- `ai/tools/sql.py:29-49`：standard 档 `include_data` 行经 `redact_rows` 后才进 result（喂模型）；卡片给用户看的仍是原文 ✓（"卡片原文、模型 token"分离，好设计）；open 档明文 ✓；strict 档 include_data 直接 BLOCK（比文档更严）✓。
6. **对话文本过脱敏** ✗ -- `redact_text` **在全后端零调用**（grep 证实）。文档"用户问题里贴的敏感串也被打码"未接线：用户在提问框贴手机号，原文直接出网。
7. **报告链路脱敏** ✗ --（v1 已记，维持）`_llm_narration` 把各章聚合行原文拼进 prompt 直出模型，无 redact、无三档判断。B2 最实质缺口。
8. **脱敏失败的行为** ⚠ -- `tools/sql.py:48-49`：redact 抛异常时 **`redacted_rows = res["rows"]`，明文照发**。fail-open 设计，与"出去的东西没有毒"的原则相反，应 fail-closed（发 columns+rowcount、丢 rows）。**代码问题（严重，v1 未发现）**。
9. **测试** ✗ -- 验收明确要求四类断言（每类正则/列规则单测、token 一致性、扫描器零命中、性能），**全无**：`redact.py` 零专属用例；egress 测试只断言 `redactions==[]`。本次审核的实测（上表）可作为用例底稿。
10. **redactions 回填清单/审计** ✗ -- 实际发生的脱敏 token 只进了模型可见的 result（且截 5 个），manifest 的 `redactions` 恒为空、审计 egress 事件不记录本轮真实脱敏量。出网清单与实际出网内容在"脱敏"维度脱钩。

**归类**：**代码问题（按严重度）**：①report 链路接入 redact + 三档（维持 v1 最高优先）；②redact 异常改 fail-closed；③chat 用户消息接 `redact_text`；④redact 单测四类断言补齐；⑤redactions 回填 manifest/审计。**文档无需改**（验收写得对，是代码没做到）。

### B3 敏感度路由 -- ◐（v1 结论有误需纠正；另发现一个 v1 漏掉的功能性断裂）

**先纠正 v1**：v1 称"代码对 draft 状态的敏感标签也代号化（注释自述兼容）"。**实际代码相反**：`codify.py:116` 明确要求 `status == "confirmed"`，118-119 行注释写明"按 07 §7 不变式，路由只认 confirmed，此处代号化也应只认 confirmed，故不处理 draft"。**实现与文档完全一致，v1 的"改文档认可 draft 即代号化"建议作废**--文档不用改，代码本来就是对的。

**逐条验收核对**：

1. **"敏感"标签走知识库 draft->确认流程** ✅ -- 复用现有标签体系：约定名为 `sensitive` 的标签 + 现有 tags/confirm 工作流。注：**魔法字符串耦合**，只认英文小写 `sensitive`，中文标签"敏感"不命中（`tn.lower()=="sensitive"`）。**代码问题（小）**：至少 UI/文档要说明这个约定，或两语都认。
2. **代号化：表/列 -> t_17/c_3，注释剥离** ✅ -- `codify_schema`：表名、列名、FK 两端、注释全处理，且知识库上下文文本、**manifest 的 candidate_tables** 也代号化（清单本身不泄密，超出文档要求的细致）。映射每连接一套，`codify-{conn_id}.json` chmod 600 永不出网 ✓；同表同码稳定（哈希+冲突递增）✓。
3. **执行链路** ✗ **（v1 漏掉的功能性断裂）** -- 模型收到的是代号 schema，写出的 SQL 引用 `t_17/c_3`；但 `ai/tools/sql.py` 拿 `args["sql"]` **原样过闸门、原样执行**（全后端 grep 证实 `decodify_text` 仅在 loop.py:209-211 对**展示用**的 card/result SQL 还原，从不还原**待执行**的 arguments）。结果：敏感表上的 AI 查询会在真库上直接报 `no such table: t_17`。**B3 的核心用户路径（问敏感表 -> 得到结果）在执行一步断掉**，功能实际上不可用。**代码问题（高，与 B2 report 同级）**。
4. **回答回显还原** ◐ -- 仅 SQL 卡还原（执行前/后语义见上）；**模型的叙述文本不还原**：AI 说"t_17 表的 c_3 列…"时用户看到的就是代号。验收"用户看到的回复中表名已还原"只对 SQL 成立。
5. **非敏感查询行为不变** ✅ -- 无 sensitive 确认标签时代码路径零改动。
6. **可选强制路由（敏感查询 -> 本地模型）** ✗ 未做 -- 文档标注"可选"，不算偏差。
7. **测试** ✗ -- codify 全模块零测试。

**归类**：**代码问题**：①**执行前还原**（tool arguments 先 `decodify_text` 再过闸门执行，展示层已还原无需二次处理）--这是 B3 可用性的关键一步；②叙述文本还原；③敏感标签约定（命名/双语）写进文档或放宽匹配；④codify 单测。**v1 的文档修订建议（认可 draft 代号化）作废**。

### B4 三档模式 -- ◐（档位切换通路断裂是核心问题；严格执行面基本成立）

**逐条验收核对**：

1. **三档行为** ◐ --
   - **严格档**：`resolve_provider_cfg` 强制 mock（loop/report 全覆盖，可完全离线）✓；`tools/sql.py` strict 下 include_data 一律 BLOCK（即使脱敏也不放行，比文档更严）✓；但 **intent 分类绕过**（B1 已记：`intent.py` 不走 `resolve_provider_cfg`，云模型配置下严格档照样把用户问题发云端）✗。
   - **标准档**：结构 + 脱敏聚合 ✓（B2 的 tools 路径）；报告路径例外（B2 已记）✗。
   - **开放档**：明文 ✓；但 **"逐查询授权弹层 + 写审计"完全未实现**--只有设置页文案写着"需逐查询授权"，代码里没有任何授权流，open 档直接明文出网。✗
2. **配置与切换** ✗ -- **档位切换是断的**（A2 已实锤：`SettingsUpdate` 缺 `privacy_mode` 字段，PUT 被静默丢弃；前端 select 仅乐观更新本地 state，刷新即回退）。当前唯一改档方式是手改 `data_dir/settings.json`。**这是 B4 一切验证的前提性断裂**。
3. **切换即时生效并审计** ✗ -- 即时生效 ✓（各处每次请求实时读 runtime），但切换无审计事件（settings API 无 audit 调用；且切换本身不工作）。
4. **严格档一键离线** ◐ -- 机制上"选严格即自动 mock"（服务端 resolve_provider_cfg 强制）✓；UI 只有一行提示文案而非独立开关，且因切换通路断裂整体不可用。**"禁用云端测试按钮"未做**（strict 下 /ai/test 仍可测云端）。
5. **manifest 标注档位** ✅ -- build_manifest 读取 privacy_mode 入清单（含校验非法值回退 standard）。
6. **严格档抓包验证** ◐ -- 主链路（chat/report/tools）在 strict 下确实无行数据出网（mock 强制 + include_data 拦截）；例外：intent 分类的云调用（用户问题文本）。嵌入模型 API 调用（若配置 api provider）也不受三档约束（小项）。

**归类**：**代码问题**：①打通 `privacy_mode` 的 PUT（与 A2 同一处修复）；②intent/embedding 调用纳入三档约束；③open 档逐查询授权流（弹层 + 审计）或先在文档降级为"个人版明文档，授权流推迟"；④档位切换写审计。**文档问题（小）**：open 档"逐查询授权"若 P2 不做，应在 02 明确分期，避免验收长期挂空。

### C1 编辑器实时闸门检查 -- ◐（灵魂"规则同源"兑现；规则集 4 条做了 3 条，lint 零测试）

**逐条验收核对**：

1. **选型** ✅ -- 后端轻端点方案（05 §4 的后端优先）：`POST /sql/lint` 复用后端 sqlglot + `assess_sql`，前端 CodeMirror 6 `@codemirror/lint`（按需分包已做）。零模型调用 ✓，mock/离线全量工作 ✓。
2. **同源原则（本功能灵魂）** ✅ -- lint 的规则级诊断**直接来自闸门 Assessment 的 reasons**（query.py:426-439），rule_id/文案/对象与执行时完全一致；read-only 连接的 lint 文案与执行拦截同文。绝无第二套规则。
3. **规则集 v1 逐条**：
   - 未知表/未知列 -> 红波浪 + 编辑距离建议 ✅（`_closest` 距离 ≤3 才建议，防乱建议；列级诊断按 sqlglot Column 抽取，带表限定/全库两级查找）。
   - 语句形态预判（无 WHERE UPDATE、缺 LIMIT 大表 SELECT）-> 行内警告 ✅（闸门 reasons 映射为 warning/error；"执行时将触发 REVIEW（规则：xxx）"语义通过闸门文案传达）。
   - 多语句混合读写 -> 提示将 BLOCK ✅（multi-statement 规则直接进诊断）。
   - **表名/列名绿色下划线装饰 + 悬停列卡片 ✗ 未做**（已知对象只有出错时才有标记，正向高亮缺失）。
4. **延迟** ◐ -- 前端 120ms 防抖 ✓、后端轻量（schema 30s 缓存）；但 **<100ms 无测试断言**（elapsed_ms 有返回却无人验），且定位用 `sql.find(表名)` 子串查找，同名子串会标错位置（如 orders 命中 orders_archive 的前缀段）。**代码问题（小）**。
5. **测试** ✗ -- `/sql/lint` 全后端零用例（未知表建议、规则映射、read-only 诊断全靠人工）。

**归类**：**代码问题（小）**：补 lint 单测（含 elapsed_ms 断言与定位修正）、绿色正向高亮可作增强项。**文档无需改**。v1 的 ✅ 降为 ◐。

### C2 猜你想问 -- ✅（维持 v1）

- 基于**已确认**领域标签本地拼装（AiRail.tsx:615-657）：每标签找一张代表表，生成"按月统计{标签}的{表}数量"式问题，中英跟随 locale，补齐到 3 条、上限 5 条；无确认标签时回退静态词典。零模型调用 ✓；点击即发送 ✓；引用表均属已确认标签 ✓。
- e2e：`verify-c2-c6.mjs` 断言 3-5 条 + 点击直接发送不重建。**注意**：该 e2e 当前因登录回归全红（见 06 节），断言存在但跑不过。

### C3 追问链条 -- ◐（v1 的 ✅ 降级：追问是硬编码模板，未"改写上一查询"）

- 每个答案卡下 2-3 个可点追问 ✓；点击续问不重建会话 ✓（followup 事件，e2e 有断言）。
- **差距**：`followUps` 是三条固定模板（"按周统计{首表}呢？""只看最近7天的？""加个同比对比呢？"），只替换了**第一张表名**；文档要求的"改写上一个意图的维度/过滤"与"无规则可用时让模型附带返回"均未实现。验收"追问改写命中上一查询的表集合"只对单表查询勉强成立，多表查询的追问会丢表。
- **归类**：代码问题（中低）--把 traceTables 全集与上轮列名注入模板即可显著达标；模型兜底可后置。

### C4 SQL 折叠 / 双视角 -- ✅（维持 v1）

- 默认折叠为"显示查询"小字行 ✓；记忆在 localStorage（`tabletalk-sql-fold`）✓；设置页"始终显示 SQL"开关 ✓（general 页）；折叠态操作入口（分析/格式化/执行按钮在卡片头部，不随折叠消失）✓；e2e 断言齐全。

### C5 答案可追溯 -- ◐（v1 的 ✅ 降级：表引用 ✓，KB 条目引用 ✗）

- 表引用 chips ✓：card.tables -> blast.direct -> SQL 正则三级来源（AiRail.tsx:385-394），点击跳星图定位（D1 联动）✓，e2e 断言"点击 chip 跳星图"；无引用时不渲染空区块 ✓。
- **知识库条目引用 ✗**：文档"参考了哪些知识库条目（点击展开原文）"未实现--SQL 卡上没有任何 KB 文档引用 chips；manifest 里只有 kb_docs 计数。
- 报告叙述的来源标注 ✓（[r1] refs 体系在报告模式已有）。
- **归类**：代码问题（中低）--loop 的检索阶段已知道命中哪些 KB 条目（to_context），透传到 sql_card 即可。

### C6 常用问题库 -- ◐（v1 的 ✅ 降级：闭环 ✓，但占位符与管理 UI 缺）

- **闭环 ✓**：`core/questions.py`（questions.json 每连接一套，chmod 600）+ CRUD API + 保存按钮（SQL 卡"存为常用"）+ **命中免模型调用**：loop 先查本地库，归一化文本相似度（阈值 60）+ 表集合加分，命中直接过闸门执行 + 审计"问题库命中" + 合成 provider=local 的清单。hit_count 统计 ✓。e2e 断言保存->再问->命中链路。
- **缺**：①**参数占位符（`{month}`）未实现**（文档功能逻辑明确要求，matched SQL 原样执行）；②**管理 UI 缺失**（后端有 GET/DELETE，前端只调了 POST--没有列表/删除/改名界面）；③后端零单测；④命中阈值偏宽（首尾 6 字符相同即 60 分，中文短问题易误命中）。
- **归类**：代码问题（中低）：管理 UI + 占位符 + match 单测（含误命中边界）。

### D1 搜索定位 -- ✅（维持 v1）

- 搜索框 + 实时过滤下拉（含行/列元信息）✓；无结果提示（i18n `graph.searchNoResult`）✓；回车取首项、点结果即飞 ✓。
- 飞行：`flyTo` 计算目标 yaw/pitch（最短 yaw 路径 + pitch 夹取 ±1.2），easeOutCubic **980ms**（<1.2s ✓），缩放至 1.28 居中；**任何 pointerdown/wheel 即打断**（cancelFlight）✓；`prefers-reduced-motion` 瞬切 ✓；飞到后琥珀色 searchFlash 临时点亮 + 选中回调 ✓。C5 的"跳星图定位"复用同一 flyTo 链路（`tabletalk:do-locate` 事件）✓。
- "14 表 demo 全部可定位"由 e2e（verify-graph/graph3d）覆盖，当前因登录回归跑不了（见 06 节）。

### D2 显式旋转控制 -- ✅（维持 v1）

- ⏸/▶ 按钮 + **sessionStorage**（严格符合"会话内持久"）✓；paused 状态下静置不自转（tick 的 `shouldSpin` 排除 paused）✓，恢复后经同一 0.02 缓升平滑启动 ✓；隐式行为（交互即停、1.2s 静置缓升）完整保留 ✓；按钮状态与实际自转一致（同一 pausedRef 驱动）✓。

### D3 选中后邻居局部散开 -- ✅（维持 v1）

- 选中时直接邻居径向外扩 `1 + 0.26×scatter`（26%，落在 20-30% 区间）✓；~1s 指数缓动（0.07/帧）✓；**连线跟随**（边端点取散开后的 POS）✓、散开期间可点击（hit 测试用同一 POS）✓；取消选中回位（scatterTarget=0）✓；连续切换不抖动（target 切换时 lerp 连续过渡）✓；实现于布局层（对基位置乘系数），FK 数据未动 ✓。

### D4 Onboarding 飞行 -- ✅（维持 v1，一处比 v1 记录更好）

- 首次进入 4s 引导飞行（yaw 自转一周缓入 + 拉近）✓；**任意交互立即打断且 localStorage 永久记忆**（`tabletalk-graph-onboarded`，比 v1 记的 sessionStorage 更符合"永不再次出现"）✓；文案走 i18n ✓；点击提示条本身也结束引导 ✓。

### 03 §5 星图行为基线（勿回退项）-- ✅（逐项复验）

`projBy`/`proj` 分离的历史 bug 修复**未回退**（Graph3D.tsx:391-392，边端点取 `projBy[a]/[b]`）；downHit 锁定、downMoving 漂移抑制（阈值 0.0006）、深度排序命中测试、hover 不抢占选中（onClick 只认 downHit）全部在位；自转参数 0.0016 rad/帧、1200ms 静置、0.02 缓升与 03 文档逐字一致；视觉基线（深空渐变底、双星云、130 星 0.35x 视差、赤道环）未回退。

### E1 凭证保险库 -- ◐（v1 的 ✅ 降级：主密钥无人生成，默认明文落盘）

**逐条验收核对**：

1. **前端永不见密码** ✅ -- `connections.public()` 把 password 恒替换为 `•••`；CRUD/test 全部走 public()，DevTools 抓不到明文 ✓。
2. **加密存储** ◐ -- `core/vault.py`：HMAC-SHA256 计数器流加密 + 截断 HMAC 校验（nonce 12B 随机），机制本身健全。但两个问题：
   - **主密钥无人生成**（v1 未发现）：全代码库没有任何地方创建 `master.key` 或要求 `TABLETALK_MASTER_KEY`；无密钥时 `encrypt()` 静默回退 `ENC@plain:`（base64 ≈ 明文）落盘。**默认部署下 PG/MySQL 密码实际是明文存储**，且无任何告警--又一处 fail-open。文档说"密钥来自部署时注入"，但代码既不生成也不提示，验收"服务端加密存储"默认不成立。
   - **文档性误导**：docstring 自称"AES-GCM 风格"，头部注释暗示"有 cryptography 时走 AES"，实际**全代码无一行 AES**，永远是 XOR 流。命名/注释应如实。
3. **明文迁移** ✅（惰性）-- 旧明文连接在下次 save 时自动加密（_load 保持明文内存态、save 走 encrypt），迁移工具不需要单独跑。
4. **连接 CRUD 按角色** ✗ 未做（依赖 E4，见下）。
5. **测试** ✗ -- vault 零单测（本地 ~/.tabletalk 全是 SQLite 无密码连接，掩盖了该缺陷）。

**归类**：**代码问题**：①首次启动生成 `master.key`（或无密钥时启动告警 + 拒绝明文回退改 fail-closed）；②注释改为如实描述；③补 vault 单测。**v1 ✅ 降级为 ◐**。

### E2 DML 审批流 -- ◐（minimal 达标项都在；但批准执行绕过闸门，是安全问题）

**逐条验收核对**：

1. **转审批队列 + 批准/驳回（附批注）** ✅ -- `/approvals` CRUD + admin 角色校验（approve/reject 仅 admin）+ 批注字段 + `approvals.json` 持久化；仅团队模式开放（单机 400）；AiRail 风险面板有"转审批"按钮 + ApprovalPage 审批视图。
2. **审计关联** ◐ -- 转审批/审批通过/审批驳回三步均写审计（source=approval）✓；申请人（requested_by）记录 ✓；但**执行结果未与审批 id 关联**（audit 里无 approval id 字段），"审计可完整还原审批链"只算部分。
3. **未批准语句无法执行** ✅ -- 状态机（pending -> approved/rejected）保证只有 approve 才执行 ✓。
4. **⚠ 批准后执行绕过安全闸门**（v1 未发现）-- `api/approvals.py:66-69`：注释自述"重新评估（防 TOCTOU）--为简化，直接执行"，`core_query.execute` **不经 assess_sql、不做 read_only 检查、无 confirm**。一条无 WHERE 的 UPDATE 若被（误）批准将直接全表执行--**击穿"一个闸门管所有执行"的产品第一不变式**。approval 创建时也未先过闸门验证语句类别。
5. **驳回通知** ✗ -- 无通知机制（P3 可后置）。
6. **策略配置哪些表/角色必须审批** ✗ -- 计划内未做。

**归类**：**代码问题（高，与 B2 report 同级）**：批准执行前必须重新 `assess_sql` + read_only 检查（且只有 ALLOW/已确认 REVIEW 可执行）；创建审批时也应先过闸门。**文档无问题**。

### E3 统一 AI 出口 -- ◐（P3 计划内部分完成；两个统计字段是死的）

- `/usage` 从审计 egress 事件聚合 by_model / by_user / total_egress ✓（口径与清单记录一致--同一数据源）；成员禁配云端 key ✓（团队模式下 PUT /settings 的 ai_models 仅 admin，settings.py:48-56）。
- **total_tokens 恒为 0**：代码统计 `prompt_tokens/completion_tokens`，但 manifest 与审计从不记录 token 数（build_manifest 无此字段、gateway 不回填）--死字段。
- **by_user 实际是 by_origin**：审计条目无 user_id（logger 无此参数，egress 写入也没带），全部落在 "ai"。
- 组织级模型配置 = 多模型列表 + admin 专属编辑，minimal ✓。
- **归类**：P3 项部分符合预期（维持 v1）；若 P3 要做用量，需先在 gateway 回填 token 数 + egress 审计带 user。

### E4 账号与鉴权 -- ◐（进行中；两个新发现：弱密码哈希、单人零配置被打破）

1. **账号体系** ◐ -- `core/auth.py`：用户/角色（admin/member）/改密/注销 + HMAC 签名 JWT（TTL 24h，密钥 data_dir 内）；首启内置 **admin/admin123**（is_initial 标记 + 首登改密提示）✓；LoginDialog + bootstrap 首取 token 后 JWT 化 ✓。
2. **未认证请求 401** ✓ -- 中间件守 /api/*，豁免仅 /health、/bootstrap、OPTIONS；静态 SPA 免鉴权 ✓。
3. **"单人模式零配置不变" ✗（新发现）**--`is_team_mode()` 的判定是"users.json 存在即团队"，而 `_load()` **首启必创建 admin 用户** => 所有新装实例都是团队模式 + 强制登录。原"开箱即用零配置"的单人体验被打破（与 03/06 的单人验收冲突）。
4. **密码哈希弱（新发现）**-- SHA-256 + **固定全局盐** `"tabletalk-salt"`（auth.py:29-30）：无 per-user 盐、快速哈希，可预计算表攻击。本地工具也该用 PBKDF2/scrypt/argon2 或至少随机盐。
5. **CORS** ◐ -- `cors_origins` 已 env 化（债 #3 兑现）但**默认仍是 `["*"]`**，与"网络化部署必须收紧"的文档要求之间缺一个部署时强制。
6. **回归**：e2e 全套未适配登录（06 节）。

**归类**：**代码问题**：①密码哈希升级；②单人/团队模式的显式选择（如首启向导或 env，避免"永远团队模式"）；③e2e 登录前置。**文档问题（小）**：E4 验收"单人模式零配置不变"需与登录必然性二选一（或明确"单人=默认账号一次登录"）。

---

## 附：首轮快查结论存档（v1，2026-08-21 上午）

> 以下为首轮快速扫描的结论表，本次 v2 逐项深查将逐条复核并可能修正其结论。

### 总体结论（v1）

**落实质量整体很高**：28 个功能条目中绝大多数按文档实现，且几处实现比文档更细致（A5 的方言分治、C1 的规则同源、B2 的三档执行点）。测试纪律基本兑现。发现 **1 个实质性安全缺口**（报告链路未脱敏）、**1 个系统性回归**（新增登录后 e2e 全套失效）、**3 处文档需要修订**。绝大部分偏差属于代码侧未完成或实现取舍，文档设计本身大体经受住了检验。

### v1 条目表（02 功能详规）

| 条目 | 状态 | 证据与判定 |
|------|------|-----------|
| A1 拦截可解释 | ✅ | `safety/models.py` 结构化 `reasons`（含 `message_en` 双语）；审计带理由。符合验收 |
| A2 策略即配置 | ◐ | **执行层完整**：`api/query.py` 落实 table_rules / pattern_rules / threshold，命中标注"策略 vN"。**缺口：settings API 不暴露 policy 字段、设置抽屉无策略页**--DBA 无法用 UI 配置（文档验收明确要求 UI + 即时生效）。**代码问题**：补 `GET/PUT /settings` 的 policy 透传 + 策略页 |
| A3 爆炸半径 | ◐ | 后端 `safety/blast.py` + 测试（124 行）✓；确认弹层呈现 ✓；**星图联动（P2 后半）未做**--属计划内未到期，非偏差，但 commit 声称"A3 完成"范围有水分 |
| A4 回滚剧本 | ✅ | `safety/rollback.py`（UPDATE/DELETE 导出 + 逆向模板，docstring 声明剧本过闸）+ `AiRail.RollbackView` UI。剧本过闸的测试覆盖未逐行验，建议补断言 |
| A5 成本防护 | ✅ | 三方言 `explain()`；SQLite 按修订后方案走 SCAN/SEARCH + row_count 估算（**与文档修订版一致，好**）；EXPLAIN 失败放行+审计降级；顺带做实了原空转的 threshold（债 #1）。符合验收 |
| B1 出网清单 | ✅ | `ai/manifest.py` + SSE `manifest` 事件 + `AiRail.ManifestView` + 审计 egress 事件 + `/audit/egress` + payload 一致性测试（117 行）。小瑕疵：`report.py:275` 异常兜底分支硬编码 `mode:"standard"`，可能记录错误档位。**代码问题（小）** |
| B2 脱敏网关 | ⚠◐ | chat 链路完整：`safety/redact.py`（HMAC 确定性 token，盐本地）+ `tools/sql.py` 三档执行（strict 拦 / standard 脱敏 / open 明文）+ seed 已补 email/phone 列 ✓。**实质缺口：报告链路（`ai/report.py`）的聚合数据未经 redact 直接出网**，而报告模式强制 `include_data=true`--标准档下与 B2 验收"出网零命中原始值"直接冲突。**代码问题（本报告最高优先级）** |
| B3 敏感路由 | ⚠ | `safety/codify.py` 代号化/还原 ✓、敏感标签挂钩 ✓。**偏差**：代码对 **draft 状态的敏感标签也代号化**（注释自述"兼容"），文档说标签走确认流程、07 不变式说 draft 不影响行为。代码比文档更保守（保护优先、错误方向安全），**建议改文档**：明确"敏感代号化从 draft 即生效（保护优先），路由仍只认 confirmed"的不对称规则，而不是改代码 |
| B4 三档模式 | ◐ | `privacy_mode` 配置+校验 ✓、chat 链路三档执行 ✓、manifest 标注档位 ✓；**报告链路不受三档约束**（同 B2 缺口）；"严格档一键离线"设置项未验。**代码问题**：报告链路补三档判断 |
| C1 编辑器实时检查 | ✅ | CodeMirror 6（选型符合 05 §4 后端优先）+ `/sql/lint` 与闸门共享 rule_id（同源原则兑现）+ 波浪线用 `var(--amber)/var(--red)`。lint 120ms 防抖。符合验收 |
| C2 猜你想问 | ✅ | AiRail 本地规则生成，零模型调用 |
| C3 追问链 | ✅ | 本地规则生成 2-3 个可点追问 |
| C4 SQL 折叠 | ✅ | 默认折叠 + localStorage 记忆 + 展开入口保留 |
| C5 答案可追溯 | ✅ | 表引用 chips（card tables / blast / SQL 解析三级来源） |
| C6 问题库 | ✅ | `core/questions.py`（含参数化与 visibility 字段，P3 预留）+ API + 命中免模型调用（loop 有 question_library 分支，manifest provider=local）+ e2e 脚本 |
| D1 搜索定位 | ✅ | 搜索 + 飞行动画（searchFlash） |
| D2 旋转控制 | ✅ | 暂停按钮 + sessionStorage（符合"会话内持久"） |
| D3 邻居散开 | ✅ | 散开进度 + 缓动 |
| D4 Onboarding 飞行 | ✅ | sessionStorage 防重放（符合"永不再次出现"） |
| E1 凭证保险库 | ✅ | `core/vault.py` AES-GCM + `TABLETALK_MASTER_KEY`/master.key；明文迁移工具未验 |
| E2 审批流 | ✅（minimal） | `api/approvals.py` + `ApprovalPage` + e2e 脚本；完整审批链审计还原未逐项验 |
| E3 统一 AI 出口 | ◐ | `/usage` 用量聚合（by_user/by_model/tokens，从 egress 事件统计）✓；组织级模型配置与成员禁配 key 未验。P3 项，部分符合预期 |
| E4 账号鉴权 | ⚠ 进行中 | `core/auth.py`（用户/密码/角色）+ `/auth/*` + LoginDialog（未提交的工作区改动）+ CORS env 化（债 #3 兑现）。**但引发系统性回归，见第五节 e2e 条目** |
| F1 License | ✅ | Apache-2.0（与手册一致）。CONTRIBUTING/安全披露渠道未验 |
| F2 gate 拆库 | ◐ | 攻击测试集**在库内**落地（cases.yaml 703 行 + README + CI 可跑，attack+safety 58 passed 实测）✓；**独立 pip 包未拆**。**文档问题**：拆包对 P1 而言过重且非必要，建议 06/F2 改为"先库内攻击集（已达成），独立包推迟到 Phase 3 开源分发前" |
| F3 演示标准 | ✅ | mock 全链路 + seed 敏感列；README 双语与动图未验 |

### v1 其余章节（03 交互 / 04 视觉 / 05 技术债 / 06 测试 / 07 知识库）

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 03 §5 星图行为基线（勿回退项） | ✅ | downHit / downMoving / hit 深度排序 / hover 不抢高亮全部保留；自转参数 0.0016 / 1200ms / 0.02 缓升与文档一致 |
| 03 §2.2 Esc 打断 AI 生成（待建 P1） | ✗ | 只找到面板关闭的 Escape；生成流中断未实现。**代码问题** |
| 03 §2.2 Cmd+Enter 执行 | ✗ | 仅找到 Cmd+1/2 切视图；编辑器执行快捷键未见。**代码问题（小）** |
| 03 §6 三级确认流 | ◐ | REVIEW 弹层含行数预览 + 回滚剧本 ✓；按钮文案带具体后果、BLOCK 红牌输入表名确认未逐项验 |
| 03 §8 i18n | ✅ | locales 中英词典均有新增条目 |
| 04 颜色 token 化 | ◐ | 新增 UI 组件无硬编码色值、REVIEW/BLOCK 语义色（--amber/--red）沿用 ✓；**但 `ReportCard.tsx:8` 图表调色板是硬编码六档纯蓝渐变，未用 04 §图表配色规定的五色可区分调色板（#4cc9f0 等），饼图相邻扇区难区分。代码问题（轻微）**；另 ConnectionModal.tsx:191 有 `var(--danger, #e57)` fallback 硬编码，顺手清理 |
| 04 星图视觉基线 | ✅ | 深空底/星云/自转参数均保留 |
| 04 亮色主题双验证 | 未验 | 建议双主题各跑一遍截图脚本 |
| 05 债#1 threshold 空转 | ✅ | 已做实（A5 链路读取） |
| 05 债#2 cloud 无 key 静默降级 | ✗ | 仍存在；文档要求显式报错。**代码问题** |
| 05 债#3 CORS 全开 | ✅ | `env.cors_origins` 显式配置 |
| 05 债#4 审计无 schema_version | ✅ | logger 已加 SCHEMA_VERSION |
| 05 债#5 chat 工具不写审计 | ✅ | `tools/sql.py` 已写审计 |
| 05 债#6 报告模式明文出网无提示 | ✗ | 未处理，与 B2 报告缺口同源。**代码问题** |
| 05 债#7 debuglog 残留 | ✅ | 全删 |
| 05 债#8 CLAUDE.md KB 描述过时 | ✅ | 已更新 |
| 05 新发现：chat 历史现状文档矛盾 | ⚠ | frontend/AGENTS.md 说 localStorage；CLAUDE.md 说 chat.db；实测真相是两者并存。**文档问题** |
| 06 后端 pytest | ✅ | 261 passed / 8 skipped，实测 |
| 06 攻击测试集 | ✅ | attack+safety 58 passed，实测 |
| 06 typecheck + build | ✅ | 实测通过 |
| 06 e2e 套件 | ⚠ 红 | 新增登录未适配，verify-* 全部无登录步骤；实测 verify-graph3d 超时于登录页。**代码问题（高优先）** |
| 06 北极星指标埋点 | ◐ | 出网可枚举率 + 用量聚合已具备；其余四项无埋点 |
| 06 反目标护栏 | ✅ | 未见越界 |
| 07 检索公式未漂移 | ✅ | 公式与文档一致 |
| 07 路由不变式 | ✅ | route_tables 只认 confirmed |
| 07 B3 敏感标签挂法 | ⚠ | 同 02-B3：draft 即代号化与文档表述冲突，**建议改文档** |
| 07 自研向量/图替代（不接第三方库服务） | ✅ | 依赖仅 numpy + sqlite-vec（本地扩展，无外部 DB 服务），红线守住。向量层四层分明（BatchIndex numpy 批量余弦 + argpartition、VectorStore 换装点、哈希/API 双嵌入、SqliteStorage 标准格式+vec0 备用），带实测基准注释与**性能回归测试**（1万×256维<300ms）；混维度对齐（\_aligned 主维度补零）是文档未要求的加分项。图层：FK+值重叠两类边（重叠边带包含率权重、id↔id 去噪、墓碑不复活）、expand_tables BFS 多跳、hop_sql 递归 CTE（纯 SQL 图查询）。知识库专项测试 7 文件 41 用例。固有边界（内存全扫 O(N·d)、哈希无语义）已按 07 §8 写明升级触发条件。小瑕疵：NumpyVectorStore.search() 全量排序未用 argpartition，但运行时只用 scores_all（融合需全量分），search 仅测试在用 |

### v1 偏差归类与行动清单

**代码问题（按优先级）**：
1. 报告链路未脱敏且不受三档约束（B2/B4/债#6 三条同源）--唯一实质安全缺口。
2. e2e 全套未适配登录（E4 引入的回归）。
3. A2 的 policy 未暴露 API/UI。
4. 债#2 cloud 静默降级未按文档改显式报错。
5. Esc 打断生成、Cmd+Enter 未做。
6. 小项：report fallback 硬编码档位；A4 剧本过闸补断言；指标埋点补齐。

**文档问题**：
1. 02-B3 / 07 §7：敏感代号化"draft 即生效"应改口径认可现状。
2. 02-F2：P1 拆独立 pip 包过重，建议推迟到 Phase 3。
3. 05 §5 债表补"chat 历史现状文档矛盾"并修正两份现状文档。

---

*审核人：Claude（对照 docs/product-handbook v1.0，v2 逐项深查进行中，2026-08-21）*
