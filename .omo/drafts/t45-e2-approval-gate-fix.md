# Draft — t45-e2-approval-gate-fix

## Request state

- intent: clear
- review_required: false
- classification: trivial
- status: awaiting-approval
- pending-action: after explicit user approval, run scaffold without `--draft-only` to create `.omo/plans/t45-e2-approval-gate-fix.md`, then APPEND todos (Metis gap analysis mandatory before handoff)

## Origin

实施文档 `docs/implementation/ai-planning-execution.md` §6 WS4 **T4.5 顺修 E2 审批绕闸**（进度快照指定下一步）。勘察结论文档已给出，本会话读码核实一致。

## Components ledger

| id | component | outcome | evidence |
|---|---|---|---|
| C1 | 新测试套件 `backend/tests/api/test_approvals.py`（RED） | 7 个用例先失败，覆盖三缺口+验收核心 | 无既有 approval 测试（`ls backend/tests/api/` 核实） |
| C2 | 修复 `backend/app/api/approvals.py`（GREEN） | create 记真实 verdict/reasons/cfg.name/approval_id；approve fail-closed | 读码：L36-46（create pass 丢弃 assess）、L76-90（approve fail-open）、审计 L50/L112 |
| C3 | 回归+收尾 | pytest 全绿、重启 sidecar、勾掉 §6 T4.5 + 更新快照 | 文档 §6/§7；会话死规矩 |

## Verified facts（探索已足，不再二次探索）

- `assess_sql(sql, sqlglot_dialect, origin) -> Assessment`（gate.py:20）；`Verdict` 值 allow/review/block（models.py:15）
- `is_team_mode()` 运行时读 `TABLETALK_AUTH_MODE`（core/auth.py:167）→ monkeypatch.setenv 可控
- `connections.get(conn_id)` 缺失抛 KeyError（core/connections.py:144）
- `AuditLogger.log(...)` 支持 reasons/tables/approval_id（audit/logger.py:136）
- conftest：`_isolate` autouse 每测试重置 state；`conn_id` fixture 建 demo sqlite 连接名 "demo"；asyncio_mode=auto
- 测试环境备忘（文档给定）：admin 经 fake Request（SimpleNamespace state）直调 handler 即可

## Adopted defaults（可逆内部决策，不问用户）

1. approve 路径闸门自身抛异常 → **fail-closed 403** + 审计 block 条目（安全纪律：未评估的 SQL 永不执行）
2. create 路径 assess 抛异常 → 按 REVIEW 标记 + 合成 gate-error reason（保守标记，仍按文档允许入队）
3. 本文件所有审批审计条目补 `approval_id` 关联（schema 已有列，代码注释本意即关联）
4. BLOCK SQL 创建时仍入队但审计标 block（文档既定行为，不改）

## Scope

- IN：上述 C1/C2/C3；验收核心 = "批准的无 WHERE UPDATE 被闸门拦"
- OUT / Must-NOT-Have：不加 DDL 执行工具；不动 confirm token 协议（T4.1-T4.4 已完）；不做 WS7；前端零改动；不改 ApprovalStore 存储结构

## Approval gate

Brief 已呈现，等待用户明确 okay。批准仅授权写计划文件，执行由用户另行启动 worker（/start-work）。
