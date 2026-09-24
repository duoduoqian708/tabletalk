# TableTalk 项目长期记忆

## 文档状态（2026-08-23）
- **docs/ 目录已整体删除**（含截图；快照：git `44ea39b`；删除：`ae11ffa` 及后续截图删除 commit，可随时找回）。
- 用户要求重新讨论 **skill / tool / 安全模式（三档）/ 智能体运行方式（意图/preflight/loop/会话）** 的新方案，聊定后形成新文档；旧文档体系（08 v0.2、D1~D15）不再是约束，只作素材。
- CLAUDE.md / AGENTS.md 仍引用已删的 docs/product-handbook（死链），新文档落定后需更新。

## 旧体系要点存档（讨论新方案时的底料）
- 分层：意图(路由) → skill(能力：白名单+指导书+剧本，只收窄不扩权) → tool(执行：原子、自带 trust 只读/变更/破坏性；安全执行点永远在 tool 层)。
- 三铁律：行数据不出网(工件进出模型上下文恒过脱敏管道)；模型可见世界恒代号(B3)；技能只收窄不扩权、禁止靠缺席。
- 意图两平面：数据面 query/report/schema/write/ddl + 控制面 audit(分批) + offtopic 兜底；拿不准当 query；单标签封闭集。
- 安全三档：strict(强制 mock/完全离线)/standard(结构+脱敏聚合)/open(明文)。
- DML 三段确认协议：REVIEW 终止 loop → confirm_token(绑 SQL 哈希/一次性/10min) → 确认后重新过闸门执行，审计闭环。
- 会话：服务端 session 落库、连接边界=会话边界、result_id 工件寻址、压缩 v1 只压叙事保骨架。
- 实施 WS1~WS8 已全部 ✅（截至 2026-08-23），代码现状见 CLAUDE.md。
