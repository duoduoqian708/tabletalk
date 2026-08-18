# V0.5 信任收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 兑现 PRD §9 三条信任承诺（审计闭环 / 报告模式一致性 / 敏感屏蔽）+ 会话信任级别前两档 + 形态口径正名，产出一版可运行、全量测试全绿的 V0.5。

**Architecture:** 后端 FastAPI sidecar（`backend/`）+ 前端 React SPA（`frontend/`）。本次 4 个 task 都走"后端小改 + 前端联动"，逐 task 实现并以全量测试（后端 pytest、前端 typecheck/build、关键 E2E）作为完成门槛。每个 task 产出独立可运行的自洽改动。

**Tech Stack:** Python 3 / FastAPI / pytest · React 18 + TypeScript + Vite · Playwright（E2E）

**执行进度（2026-08-18 完成）**：T1–T5 全部完成并提交（48d497f · 7ab5f2a · 7348db8 · f75b521 · e2fe457，另 4e10835 补 context 接线测试）。整版端到端验证通过（起真实后端：AI 对话 sql_card 带完整结果供前端直接渲染、审计 `source=loop_internal` 恰一条、敏感名单接线生效、信任级别切换 UI 就绪）。后端 171 passed · 前端 typecheck + build ✓。**V0.5 收口达成。**

**前置基线**：实现前先跑一遍全量基线确认当前全绿——
`cd backend && .venv/bin/python -m pytest -q`（预期 ~129 passed）
`cd frontend && npm run typecheck && npm run build`

---

## Task 1: 审计闭环（AI 对话内部读入审计 + 标记来源 + 消除同 SQL 双执行）

**Files:**
- Modify: `backend/app/audit/logger.py`（`log`/`list` 加 `source` 字段）
- Modify: `backend/app/ai/tools.py:122-149`（run_query 写审计 + card 带结果）
- Modify: `backend/app/ai/report.py`（报告子查询审计标 `source="report"`）
- Modify: `frontend/src/renderer/src/components/AiRail.tsx`（卡片缓存结果 + auto-run 复用）
- Test: `backend/tests/ai/test_tools_audit.py`（新建）、`backend/tests/api/test_audit.py`（若已有，追加）

- [ ] **Step 1: 后端 `AuditLogger` 加 `source` 字段**

```python
# backend/app/audit/logger.py
    def log(
        self,
        *,
        connection: str,
        origin: str,
        tier: str,
        verdict: str,
        status: str,
        sql: str,
        elapsed_ms: float | None = None,
        report_id: str | None = None,
        source: str | None = None,      # 新增：loop_internal / report / card / manual
    ) -> None:
        ...
        if source is not None:
            entry["source"] = source
```

`list()` 增加过滤参数 `source: str | None = None`，在循环内 `if source and e.get("source") != source: continue`。

- [ ] **Step 2: 写失败测试**（`backend/tests/ai/test_tools_audit.py`，新建）

```python
"""AI 对话内部真实读库必须写审计（origin=ai, source=loop_internal），且读卡带结果数据。"""
from app.ai.tools import execute_tool


async def test_run_query_tool_writes_audit(app_state, conn_id):
    out = await execute_tool(app_state, "run_query", {"sql": "SELECT * FROM products LIMIT 3"}, conn_id)
    assert out.card["verdict"] == "allow"
    assert "result" in out.card                      # 卡片带结果数据（供前端展示）
    assert out.card["result"]["row_count"] == 3
    entries = app_state.audit.list(source="loop_internal")
    assert any(e["origin"] == "ai" and e["sql"].startswith("SELECT") for e in entries)


async def test_run_query_tool_result_not_in_model_view(app_state, conn_id):
    out = await execute_tool(app_state, "run_query", {"sql": "SELECT * FROM products LIMIT 3"}, conn_id)
    # 喂模型的 result 默认不含 rows（隐私红线：include_data=False）
    assert "rows" not in out.result
```

（fixture `app_state`/`conn_id` 来自 `tests/conftest.py`：独立数据目录 + 演示库 sqlite 连接。）

- [ ] **Step 3: 运行失败测试**

Run: `cd backend && .venv/bin/python -m pytest tests/ai/test_tools_audit.py -v`
Expected: FAIL（`source` 参数不存在 / card 无 result / 审计无记录）

- [ ] **Step 4: 实现 `tools.py` run_query 分支**

```python
# backend/app/ai/tools.py，run_query 分支（原 122-149）
    if name == "run_query":
        sql = args.get("sql", "")
        assessment = safety_gate.assess_sql(sql, dialect, Origin.AI)
        if assessment.verdict == Verdict.ALLOW:
            from app.core.query import execute as run_query

            res = await run_query(state, conn_id, sql)
            result: dict[str, Any] = {
                "ok": True,
                "columns": res["columns"],
                "row_count": res["row_count"],
                "truncated": res["truncated"],
                "elapsed_ms": res["elapsed_ms"],
            }
            if include_data:
                result["rows"] = res["rows"]
            # ★ 卡片带完整结果（含 rows 供前端直接渲染，从而消除二次执行）
            card = {
                "tier": "read", "verdict": "allow", "sql": sql,
                "sub": _sub(sql, dialect),
                "result": {
                    "columns": res["columns"], "types": res["types"],
                    "rows": res["rows"], "row_count": res["row_count"],
                    "truncated": res["truncated"], "elapsed_ms": res["elapsed_ms"],
                },
            }
            # ★ 审计：对话内部真实读必须留痕（之前是盲区）
            state.audit.log(
                connection=cfg.name, origin="ai", tier="read", verdict="allow",
                status="对话内读（循环内测量）", sql=sql,
                elapsed_ms=res.get("elapsed_ms"), source="loop_internal",
            )
            return ToolOutcome(result=result, card=card,
                               think=f"只读查询，安全闸门放行（{res['row_count']} 行）。")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/ai/test_tools_audit.py -v`
Expected: PASS

- [ ] **Step 6: `report.py` 子查询审计标来源**（`app/ai/report.py` 约 209/216 行的两处 `state.audit.log(...)` 加 `source="report"`）

- [ ] **Step 7: 前端复用卡片结果、消除双执行**（`frontend/src/renderer/src/components/AiRail.tsx`）

在组件内新增 `const loopResultRef = useRef<Record<string, any> | null>(null)`；`sql_card` 事件处理里（现有 `cardsRef.current = [...]` 追加处）同步缓存：`if (ev.card?.result) loopResultRef.current = ev.card.result`。

修改 `maybeAutoRun`（现 473-480 行）：

```tsx
  function maybeAutoRun(card: AiCard | undefined): void {
    if (autoRanRef.current) return
    if (!card || card.verdict !== 'allow') return
    if (!streamDoneRef.current || !gateDoneRef.current) return
    autoRanRef.current = true
    // 卡片已带循环内执行的结果 → 直接渲染，不再 POST /query（消除同 SQL 双执行）
    if ((card as any).result) {
      handleQueryResult((card as any).result, card.sql, curQuestionRef.current)
      loopResultRef.current = null
      return
    }
    void exec(card.sql, false, curQuestionRef.current)
  }
```

`send()` 开头重置 `loopResultRef.current = null`（与 `autoRanRef` 一起）。

- [ ] **Step 8: 全量验证（本 Task 完成门槛）**

Run:
`cd backend && .venv/bin/python -m pytest -q`（全绿，含新测试）
`cd frontend && npm run typecheck && npm run build`
`cd frontend && npm run screenshot`（若脚本存在；或跑既有 E2E 脚本）——验证对话读查询后结果区出结果、审计记录中该 SQL 只有一条 `source=loop_internal`

- [ ] **Step 9: Commit**

```bash
git add backend/app/audit/logger.py backend/app/ai/tools.py backend/app/ai/report.py backend/tests/ai/test_tools_audit.py frontend/src/renderer/src/components/AiRail.tsx
git commit -m "feat: AI 对话内部读入审计(source=loop_internal)+读卡带结果消除双执行"
```

---

## Task 2: 报告模式 model_id / reasoning 一致性

**Files:**
- Create: `backend/app/ai/provider_cfg.py`
- Modify: `backend/app/ai/loop.py`（删本地 `_provider_cfg`，改用共享）
- Modify: `backend/app/ai/report.py`（`_provider_cfg` 改用共享）
- Test: `backend/tests/ai/test_report_cfg.py`（新建）

- [ ] **Step 1: 抽取共享 `resolve_provider_cfg`**（新建 `backend/app/ai/provider_cfg.py`）

```python
"""解析生效的 AI provider 配置：model_id 命中 ai_models 优先，支持逐次覆盖与 reasoning。"""
from __future__ import annotations

from typing import Any


def resolve_provider_cfg(state, req) -> dict[str, Any]:
    rs = state.runtime.get()
    mid = getattr(req, "model_id", None)
    if mid:
        target = next((m for m in rs.ai_models if m.id == mid), None)
        if target is not None:
            cfg = {
                "provider": target.provider, "base_url": target.base_url,
                "api_key": target.api_key, "model": target.model,
                "temperature": target.temperature, "timeout": target.timeout,
                "reasoning": target.reasoning,
            }
        else:
            cfg = rs.provider_config()
    else:
        cfg = rs.provider_config()
    for key in ("provider", "base_url", "api_key", "model"):
        override = getattr(req, key, None)
        if override is not None:
            cfg[key] = override
    if getattr(req, "reasoning", None) is not None:
        cfg["reasoning"] = req.reasoning
    if getattr(req, "temperature", None) is not None:
        cfg["temperature"] = req.temperature
    return cfg
```

- [ ] **Step 2: 写失败测试**（`backend/tests/ai/test_report_cfg.py`，新建）

```python
"""报告模式必须与查询模式一致：model_id 命中 ai_models、reasoning 透传。"""
from app.ai.provider_cfg import resolve_provider_cfg


async def test_report_cfg_respects_model_id(app_state):
    rs = app_state.runtime.get()
    target = rs.ai_models[0]
    class Req:  # 最小请求桩
        model_id = target.id
        provider = base_url = api_key = model = reasoning = temperature = None
    cfg = resolve_provider_cfg(app_state, Req())
    assert cfg["model"] == target.model


async def test_report_cfg_passes_reasoning(app_state):
    class Req:
        model_id = None
        provider = base_url = api_key = model = None
        reasoning = "high"
        temperature = None
    cfg = resolve_provider_cfg(app_state, Req())
    assert cfg["reasoning"] == "high"
```

- [ ] **Step 3: 运行失败测试** → FAIL（provider_cfg 不存在）

- [ ] **Step 4: 接线**：`loop.py` 删本地 `_provider_cfg`、`chat_stream` 改用 `from app.ai.provider_cfg import resolve_provider_cfg` 后 `provider = gw.build_provider(resolve_provider_cfg(state, req))`；`report.py` 的 `_provider_cfg` 直接 `return resolve_provider_cfg(state, req)`。

- [ ] **Step 5: 运行测试确认通过** → `pytest tests/ai/test_report_cfg.py -v` PASS

- [ ] **Step 6: 全量验证**：后端 `pytest -q` 全绿 + 前端 `typecheck && build`

- [ ] **Step 7: Commit**

```bash
git add backend/app/ai/provider_cfg.py backend/app/ai/loop.py backend/app/ai/report.py backend/tests/ai/test_report_cfg.py
git commit -m "refactor: 抽取共享 resolve_provider_cfg，报告模式支持 model_id/reasoning"
```

---

## Task 3: 敏感表/列屏蔽（glob 名单 + 上下文/知识库剔除 + 前端配置）

**Files:**
- Modify: `backend/app/core/connections.py`（`ConnectionConfig` 加 `sensitive`）
- Modify: `backend/app/core/schema.py` 或新建 `backend/app/core/sensitive.py`（过滤 helper）
- Modify: `backend/app/ai/context.py`（`assemble_context` 过滤后 summarize）
- Modify: `backend/app/ai/loop.py`（knowledge build 用过滤后 schema）
- Modify: `frontend/src/renderer/src/components/ConnectionModal.tsx` + `frontend/src/renderer/src/api/connections.ts`（敏感名单输入）
- Test: `backend/tests/core/test_sensitive.py`（新建）

- [ ] **Step 1: 连接模型加 `sensitive` 名单**（`connections.py`）

```python
@dataclass
class ConnectionConfig:
    ...
    sensitive: list[str] = field(default_factory=list)   # 敏感表/列 glob，如 ["payroll_*"]
```
`create`/`update` 支持 `sensitive`（`data.get("sensitive") or []` 存入；`allowed` 加 `"sensitive"`；`public()` 自然带出）。

- [ ] **Step 2: 写失败测试**（`backend/tests/core/test_sensitive.py`，新建）

```python
"""敏感名单：屏蔽命中表/列，使其不进 schema 上下文与知识库构建。"""
from app.core.sensitive import filter_sensitive


def test_filter_sensitive_removes_matched_tables():
    schema = {
        "tables": [
            {"name": "orders", "columns": [{"name": "id"}, {"name": "payroll_band"}]},
            {"name": "payroll_2024", "columns": [{"name": "id"}]},
            {"name": "products", "columns": [{"name": "id"}, {"name": "name"}]},
        ]
    }
    out = filter_sensitive(schema, ["payroll_*"])
    names = [t["name"] for t in out["tables"]]
    assert "payroll_2024" not in names                      # 表级 glob 命中剔除
    orders = next(t for t in out["tables"] if t["name"] == "orders")
    assert "payroll_band" not in [c["name"] for c in orders["columns"]]  # 列级剔除
```

- [ ] **Step 3: 运行失败测试** → FAIL（`sensitive` 模块不存在）

- [ ] **Step 4: 实现 `backend/app/core/sensitive.py`**

```python
"""敏感名单过滤：按连接级 glob 剔除表/列，屏蔽项不进发模型的上下文与知识库。"""
from __future__ import annotations

import fnmatch
from typing import Any


def _match_any(patterns: list[str], name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


def filter_sensitive(schema: dict[str, Any], patterns: list[str]) -> dict[str, Any]:
    if not patterns:
        return schema
    tables = schema.get("tables", [])
    kept = []
    for t in tables:
        tname = t.get("name", "")
        if _match_any(patterns, tname):
            continue
        cols = t.get("columns", [])
        kept_cols = [c for c in cols if not _match_any(patterns, c.get("name", ""))]
        if len(kept_cols) != len(cols):
            t = {**t, "columns": kept_cols}
        kept.append(t)
    return {**schema, "tables": kept}
```

- [ ] **Step 5: 接线**：`context.py` `assemble_context` 内 `schema = await get_schema(state, conn_id)` 后插 `schema = filter_sensitive(schema, state.connections.get(conn_id).sensitive)`；`loop.py` `chat_stream` 里 build 前的 `schema = await get_schema(...)` 同样过滤后再传给 `state.knowledge.build`。前端 `ConnectionModal` 加"敏感名单"文本域（按行/逗号分隔 glob），`connections.ts` 类型与 payload 加 `sensitive: string[]`。

- [ ] **Step 6: 运行测试确认通过** + 全量验证（后端 pytest 全绿、前端 typecheck/build）

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/connections.py backend/app/core/sensitive.py backend/app/ai/context.py backend/app/ai/loop.py backend/tests/core/test_sensitive.py frontend/src/renderer/src/components/ConnectionModal.tsx frontend/src/renderer/src/api/connections.ts
git commit -m "feat: 连接级敏感名单(glob) 屏蔽表/列，不进发模型上下文与知识库"
```

---

## Task 4: 会话信任级别前两档（全部介入 / 读自动·写确认）

**Files:**
- Modify: `frontend/src/renderer/src/store/chat.ts`（`trustLevel` 状态）
- Modify: `frontend/src/renderer/src/components/AiRail.tsx`（切换 UI + `maybeAutoRun` 按档位）
- Test: 前端 typecheck + build + E2E

- [ ] **Step 1: store 加 `trustLevel`**（`store/chat.ts`）：类型 `TrustLevel = 'all_confirm' | 'read_auto' | 'max_trust'`，状态默认 `'read_auto'`；V0.5 仅暴露前两档（`max_trust` 类型预留、UI 不展示）。随 conversation 持久化或全局（建议全局，简单）。

- [ ] **Step 2: 切换 UI + 读卡行为按档位**（`AiRail.tsx`）

对话头（`conv-title` 区）加一个小切换：`全部介入` / `读自动`（`read_auto` 默认）。`maybeAutoRun` 顶部加：

```tsx
  function maybeAutoRun(card: AiCard | undefined): void {
    if (autoRanRef.current) return
    if (!card || card.verdict !== 'allow') return
    if (!streamDoneRef.current || !gateDoneRef.current) return
    if (trustLevel === 'all_confirm') return   // 全部介入：读卡也不自动执行，等用户点
    autoRanRef.current = true
    ...
  }
```

`trustLevel` 从 store 取（`useChat((s) => s.trustLevel)`）。切换时 toast 提示当前档位。

- [ ] **Step 3: 全量验证**：`npm run typecheck && npm run build`；E2E/手动验证——切「全部介入」后发一个读查询，结果不自动进结果区、点卡片「▶ 运行」才出；切回「读自动」恢复自动。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/renderer/src/store/chat.ts frontend/src/renderer/src/components/AiRail.tsx
git commit -m "feat: 会话信任级别前两档(全部介入/读自动·写确认)，读卡自动执行按档位"
```

---

## Task 5: 形态口径正名（文档/文案，非代码）

**Files:** `CLAUDE.md` · `README.md`（若有）· `frontend/src/renderer/src/components/SettingsDrawer.tsx`（「服务」区描述）

- [ ] **Step 1: 定位文案**：把"桌面数据库客户端"相关表述统一改为 **"本地 AI 优先的数据库查询工具（self-hosting Web）"**；首段产品叙述对齐 PRD §0/§5。
- [ ] **Step 2: 设置页「通用」区"服务"描述**补一句口径（本机 sidecar · 同源托管 · 浏览器访问）。
- [ ] **Step 3: 验证**：`npm run typecheck && npm run build`（文案改动不影响逻辑，仍跑一遍确认无碍）。
- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md frontend/src/renderer/src/components/SettingsDrawer.tsx
git commit -m "docs: 对外口径正名为本地 AI 优先查询工具"
```

---

## 完成定义（V0.5 全量门槛）

- 后端 `cd backend && .venv/bin/python -m pytest -q` 全绿（基线 + 新增）
- 前端 `cd frontend && npm run typecheck && npm run build` 通过
- 端到端可运行：起后端 → 浏览器打开 → 演示库对话读查询（结果区出结果、审计恰一条 source=loop_internal）→ 切换全部介入读卡不自动执行 → 配置敏感名单后 AI 上下文不再出现屏蔽表
- 形态口径全仓一致（无"桌面客户端"残留）
