# 技能编排框架 + AI 目录结构调整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 CLEARED 的 AI 层重构成"Tool 原子层 × Skill 剧本层 × 意图调度"的可插拔技能平台，同时完成 `app/ai/` 目录整顿（合并碎片、消除镜像、统一命名）。

**Architecture:** 现有自研 ReAct 循环（`loop.py`）升级为 Agent；新建 `skills/`（注册表+剧本）与 `tools/`（原子工具）两目录；`loop.py`/`report.py` 抽共享基座为统一 Agent 循环；`provider_cfg.py` 并入 gateway、`schemas.py` 更名 `dto.py` 消三义。安全闸门是所有技能统一底座，任何 SQL 执行仍过同一闸门（铁律，HITL 测试 `test_hitl_invariant.py` 必须持续通过）。

**Tech Stack:** Python 3 / FastAPI / pytest · React 18 + TS + Vite（前端入场在 Phase 7-8）

**基线**：`cd backend && .venv/bin/python -m pytest -q` 当前 174 passed。每 task 结束时全量测试必须全绿。

**设计依据**：`docs/superpowers/specs/2026-08-18-skill-orchestration-design.md`

---

## Task 0: 跑基线确认全绿

- [ ] **Step 1** Run: `cd backend && .venv/bin/python -m pytest -q`
- [ ] **Step 2** Expected: `174 passed, 8 skipped`（若否，先修到全绿再继续）

---

## Task 1: 合并 `provider_cfg.py` 消除镜像

**现状**：`app/ai/provider_cfg.py`（38 行）有 `resolve_provider_cfg`；`report.py` 内还有 `_provider_cfg`（38-41 行，委托共享）——但 `loop.py` 也 import 共享函数。先确认无其它镜像，把 `resolve_provider_cfg` 并入 `gateway.py`，统一入口。

**Files:**
- Create: `backend/app/ai/gateway.py`（追加 `resolve_provider_cfg` 函数）
- Modify: `backend/app/ai/provider_cfg.py`（删除，改为从 gateway 再导出兼容，或改所有 import）
- Modify: `backend/app/ai/loop.py`（改 import）
- Modify: `backend/app/ai/report.py`（删本地 `_provider_cfg`，改 import）

- [ ] **Step 1: 确认现状** — `cd backend && grep -rn "provider_cfg\|_provider_cfg\|resolve_provider_cfg" app/ai/`
- [ ] **Step 2: 把 `resolve_provider_cfg` 追加到 `gateway.py` 末尾**（函数体原样复制，无改动）
- [ ] **Step 3: 新建 `provider_cfg.py` 为兼容转发层**

```python
# backend/app/ai/provider_cfg.py —— 兼容层，后续随引用清理删除
from __future__ import annotations
from app.ai.gateway import resolve_provider_cfg  # noqa: F401
```

- [ ] **Step 4: 改 `report.py`**——删本地 `_provider_cfg`（40-43 行附近），把内部调用 `_provider_cfg(state, req)` 全改为 `resolve_provider_cfg(state, req)`，并加 import `from app.ai.gateway import resolve_provider_cfg`

- [ ] **Step 5: 跑测试** — `.venv/bin/python -m pytest -q` → 全绿（provider 逻辑无改动，纯搬移）
- [ ] **Step 6: Commit** — `git add backend/app/ai/ && git commit -m "refactor: resolve_provider_cfg 并入 gateway，消除 report 本地镜像"`

---

## Task 2: `schemas.py` 更名 `dto.py` 消除三义

**现状**：`app/ai/schemas.py`（pydantic DTO）× `app/core/schema.py`（schema 发现）× `app/api/schema.py`（路由）三义撞名。

**Files:**
- Rename: `backend/app/ai/schemas.py` → `backend/app/ai/dto.py`
- Modify: 所有 `from app.ai.schemas import ...` 的 import 改为 `from app.ai.dto import ...`

- [ ] **Step 1: grep 引用** — `cd backend && grep -rn "app.ai.schemas\|from app.ai import schemas" app/ tests/`
- [ ] **Step 2: git mv** — `git mv app/ai/schemas.py app/ai/dto.py`
- [ ] **Step 3: 逐个改 import**（loop.py / report.py / api/ai.py / tests 里引用的）
- [ ] **Step 4: 跑测试全绿** + Commit `refactor: ai/schemas.py -> dto.py 消除三义命名`

---

## Task 3: 建 `tools/` 原子工具目录（先把分发做抽象，不拆实现）

**目标**：建立"工具注册表"概念——`execute_tool` 从"一串 if/elif"改成"按名查注册表分发"。**V1 不真的把 run_query 等拆成多文件**（避免大 diff），先做注册表抽象 + 让 `TOOL_SCHEMAS` 由注册表生成。

**Files:**
- Create: `backend/app/ai/tools/__init__.py`（注册表 + execute_tool 迁入）
- Create: `backend/app/ai/tools/registry.py`
- Modify: `backend/app/ai/tools.py` → 变为 `tools/` 包的一部分（先保留为兼容转发，或删后全改 import）

- [ ] **Step 1: 建 `backend/app/ai/tools/registry.py`**

```python
"""原子工具注册表：每个工具 = 名称 + schema + 执行函数，execute_tool 按名分发。

ToolOutcome 定义在本文件；各工具（sql.py/schema_tools.py）import 后调用 register_tool 注册。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

@dataclass
class ToolOutcome:
    result: dict[str, Any]
    card: dict[str, Any] | None = None
    think: str | None = None

ToolHandler = Callable[..., Awaitable[ToolOutcome]]

TOOL_SCHEMAS: list[dict] = []
_TOOL_HANDLERS: dict[str, ToolHandler] = {}

def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}}

def register_tool(name: str, description: str, props: dict, required: list[str],
                  handler: ToolHandler) -> None:
    TOOL_SCHEMAS.append(_tool(name, description, props, required))
    _TOOL_HANDLERS[name] = handler

def tool_schemas(readonly: bool = False) -> list[dict]:
    if not readonly:
        return list(TOOL_SCHEMAS)
    allow = {"get_schema", "describe_table", "run_query"}
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in allow]

async def execute_tool(state, name: str, args: dict, conn_id: str,
                       include_data: bool = False) -> ToolOutcome:
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(result={"ok": False, "error": f"未知工具: {name}"})
    return await handler(state=state, args=args, conn_id=conn_id, include_data=include_data)
```

> 说明：本 Task 建立注册表骨架；工具注册与 `execute_tool` 的实际拆分放到 Task 4（为避免 Task 3 悬空，Task 3 和 Task 4 合并为"建 tools/ + 迁入现有 5 工具"，见 Task 4 结构）。**若按此结构，Task 3 与 Task 4 应合并执行**。

**（合并后的）Task 3+4: 迁入现有 5 工具到 `tools/`**

- [ ] **Step A: 建目录结构**

```
backend/app/ai/tools/__init__.py    # 导出 ToolOutcome/execute_tool/TOOL_SCHEMAS/register
backend/app/ai/tools/registry.py    # 注册表 + _tool/register_tool/tool_schemas
backend/app/ai/tools/sql.py         # run_query / run_dml / draft_ddl
backend/app/ai/tools/schema_tools.py# get_schema / describe_table
backend/app/ai/tools/__init__.py    # 注册入口：import 各模块触发 register
```

- [ ] **Step B: `registry.py`** 写入上面的注册表实现 + `execute_tool` 分发（按 `get_handler(name)` 分发，找不到返回 `ToolOutcome(result={"ok":False,"error":f"未知工具:{name}"})`）
- [ ] **Step C: `schema_tools.py`** —— 把 `tools.py` 的 `get_schema`/`describe_table` 分支迁为 `register_tool(...)` 注册
- [ ] **Step D: `sql.py`** —— 把 `run_query`/`run_dml`/`draft_ddl` 分支迁为注册（含审计、card 带结果、dbg 日志原样保留）
- [ ] **Step E: `__init__.py`** —— `from . import registry, sql, schema_tools`（import 即注册）；导出 `ToolOutcome, execute_tool, TOOL_SCHEMAS, tool_schemas`
- [ ] **Step F: 改引用**——`loop.py`/`report.py`/`api/ai.py` 的 `from app.ai.tools import TOOL_SCHEMAS, execute_tool, ToolOutcome` 保持可 import（`tools` 现在是包，`__init__` 重新导出）；删除旧 `app/ai/tools.py`
- [ ] **Step G: 跑测试全绿**（`test_loop`/`test_report`/`test_tools_audit`/`test_hitl_invariant` 全部依赖这些导出）
- [ ] **Step H: Commit** `feat: 工具层抽象为 tools/ 注册表(原子工具可插拔)`

---

## Task 4: 建 `skills/` 技能注册表 + Skill/ScriptSpec 模型

**Files:**
- Create: `backend/app/ai/skills/__init__.py`
- Create: `backend/app/ai/skills/skill.py`（Skill / ScriptSpec / ScriptStep 数据类）
- Create: `backend/app/ai/skills/registry.py`（SkillRegistry）
- Create: `backend/app/ai/skills/builtin/__init__.py`（内置 query + report 技能注册）
- Test: `backend/tests/ai/test_skills.py`

- [ ] **Step 1: 写失败测试**（`test_skills.py`）

```python
"""技能注册表：可注册/列表/按 id 获取；内置 query+report 技能存在；只读标记正确。"""
from app.ai.skills.registry import get_skill, list_skills, register_skill
from app.ai.skills.skill import Skill, ScriptSpec

def test_registry_builtin():
    ids = [s.id for s in list_skills()]
    assert "query" in ids and "report" in ids

def test_query_skill_readwrite():
    s = get_skill("query")
    assert s.read_only is False          # query 含写(需确认)
    assert "run_query" in s.tools and "run_dml" in s.tools

def test_report_skill_readonly():
    s = get_skill("report")
    assert s.read_only is True           # report 物理只读
    assert "run_dml" not in s.tools
```

- [ ] **Step 2: 运行确认失败**（模块不存在）
- [ ] **Step 3: `skills/skill.py`** 实现 Skill/ScriptSpec/ScriptStep 数据类（按设计文档 §2.2/§5）
- [ ] **Step 4: `skills/registry.py`** 实现 `SkillRegistry`（`register/get/list`，内置只读不可删）
- [ ] **Step 5: `skills/builtin/__init__.py`** 注册内置 query(读写,含 run_dml)+report(只读,工具=get_schema/describe_table/run_query) 两技能
- [ ] **Step 6: 跑测试通过** + 全量全绿
- [ ] **Step 7: Commit** `feat: skills/ 技能注册表(可插拔)+Skill/ScriptSpec 模型+内置 query/report`

---

## Task 5: 意图调度升级（dispatcher：多技能路由）

**Files:**
- Create: `backend/app/ai/agent/dispatcher.py`
- Modify: `backend/app/ai/intent.py`（加 `dispatch_skill`，兼容保留现有 `classify_mode`/`classify_tags`）
- Test: `backend/tests/ai/test_dispatcher.py`

- [ ] **Step 1: 写失败测试**

```python
"""意图调度：识别技能；拿不准/mock 兜底回 query。"""
from app.ai.agent.dispatcher import dispatch_skill

async def test_dispatch_defaults_to_query(app_state):
    # mock 下无关键词 → query
    assert await dispatch_skill(app_state, "随便一句话") == "query"

async def test_dispatch_report_keyword(app_state):
    # mock 下"报告"关键词 → report
    assert await dispatch_skill(app_state, "出一份销售分析报告") == "report"
```

- [ ] **Step 2: 运行失败**（dispatcher 不存在）

- [ ] **Step 3: 实现 `dispatcher.py`**

```python
"""意图→技能路由：真实 LLM 从注册表技能里选一个；mock/降级用关键词；兜底 query。"""
from __future__ import annotations
from app.ai import gateway as gw
from app.ai.intent import is_report_intent
from app.ai.skills.registry import list_skills

_DEFAULT = "query"

async def dispatch_skill(state, question: str) -> str:
    q = (question or "").strip()
    if not q:
        return _DEFAULT
    rt = state.runtime.get()
    if gw.is_effective_mock(rt.provider_config()):
        if is_report_intent(q):
            return "report"
        return _DEFAULT
    skills = [s for s in list_skills() if s.id != "query"]  # query 是兜底
    desc = "\n".join(f"- {s.id}: {s.description}" for s in skills)
    prompt = (
        f"用户问题：{question}\n"
        f"可用技能（除默认查询外）：\n{desc}\n"
        "返回最匹配的一个技能 id；都不匹配或拿不准返回 query。只返回一个词。"
    )
    try:
        provider = gw.build_provider(rt.provider_config())
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        text = (resp.content or "").strip().lower()
        for s in skills:
            if s.id in text:
                return s.id
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT
```

- [ ] **Step 4: 跑测试通过** — `.venv/bin/python -m pytest tests/ai/test_dispatcher.py -v` 全绿
- [ ] **Step 5: 全量全绿** + Commit `feat: dispatcher 多技能意图路由(LLM+mock 兜底)`

---

## Task 6: 统一 Agent 循环（loop 抽共享基座，report 变 report 技能）

**目标**：`loop.py` 的 `chat_stream` 按 dispatcher 选中的技能加载工具集与 system prompt，不再硬编码 query/report 分流。`report_stream` 保留为 report 技能的剧本实现，但入口统一。

**Files:**
- Modify: `backend/app/ai/loop.py`（`stream`/`resolve_mode`/`chat_stream` 改为按技能加载）
- Modify: `backend/app/ai/report.py`（保持 report_stream，作为 report 技能）
- Test: `backend/tests/ai/test_loop.py`、`test_report.py`（视为行为不变，应全绿）

- [ ] **Step 1: 改 `loop.py` 的 `stream`**：不再 `resolve_mode` 二分，改 `skill_id = await dispatch_skill(state, user_text)`；`skill_id in ("report",)` 走 `report_stream`，其余走 `chat_stream`（加载 query 技能）

- [ ] **Step 2: `chat_stream` 内**：从 `get_skill("query")` 取 tools（全量 `tool_schemas()`）与 system prompt；`report` 走 report_stream（沿用其只读工具集 + report_system_prompt）

- [ ] **Step 3: 跑测试全绿**（现有 test_loop/test_report 定义"query 走 chat_stream / report 走 report_stream"行为应不变）
- [ ] **Step 4: Commit** `refactor: loop 按 dispatcher 选技能，report 作为 report 技能统一入口`

---

## Task 7: 后端发前置阶段事件（intent/retrieval）+ 测试

**目标**：让前端四步展示有真实阶段数据——从 `assemble_context` 里抽出意图分类与候选表清单，作为独立的 SSE `stage` 事件在 `chat_stream` 里发出去。

**Files:**
- Modify: `backend/app/ai/context.py`（新增返回意图+候选表的接口，或增加 `collect_context_meta`）
- Modify: `backend/app/ai/loop.py`（chat_stream 在 assemble 后 yield stage 事件）
- Test: `backend/tests/ai/test_loop.py` 追加

- [ ] **Step 1: `context.py` 加 `assemble_context_meta`**——返回 `(text, {"intent": tags, "candidate_tables": routed})`，复用现有 classify_tags + route_tables 逻辑（抽出来避免重复执行）

- [ ] **Step 2: `chat_stream`** 在 `yield {"type":"turn_start"}` 后，`yield {"type":"stage","stage":"intent","value":meta["intent"]}` 与 `yield {"type":"stage","stage":"retrieval","tables":meta["candidate_tables"]}`

- [ ] **Step 3: 写测试**——断言事件流含 `type=stage stage=intent` 与 `stage=retrieval tables=[...]`
- [ ] **Step 4: 全量全绿 + Commit** `feat: 对话流下发 intent/retrieval 真实阶段事件`

---

## Task 8: 前端四步真实阶段展示 + 技能展示（移除 mock 播放器）

**Files:**
- Modify: `frontend/src/renderer/src/components/AiRail.tsx`
- Modify: `frontend/src/renderer/src/api/ai.ts`（AiEvent 加 stage 类型）
- Modify: `frontend/src/renderer/src/components/ModulePages.tsx`（拆单组件，可选）

- [ ] **Step 1: `api/ai.ts`** 的 `AiEvent` 联合类型加：`{ type:'stage'; stage:string; value?:any; tables?:string[] }`
- [ ] **Step 2: `AiRail.tsx`** 的 SSE 事件处理：收到 `stage` 事件 → 更新对应 turn 的步骤 detail（intent→显示 value，retrieval→显示 tables 清单）；把 `playSteps` 的 mock 定时器逻辑改为"跳过 mock 文案，等真实 stage/sql 事件驱动步骤状态"
- [ ] **Step 3: 移除 `THINK_LINES` mock 文案**（`thinkMode`/`THINK_LINES`/`STEP_MS`/`LINE_MS` 相关 mock 播放）——零定制红线落地；步骤状态改由真实事件推进
- [ ] **Step 4: 技能展示**——对话头显示"当前技能：query / report"（从首次 stage 事件或 dispatcher 结果），或加技能选择器
- [ ] **Step 5: 前端验证** — `npm run typecheck && npm run build`
- [ ] **Step 6: 端到端** — 起后端(mock)跑一句查询，浏览器/日志确认：四步逐步点亮、检索阶段显示候选表清单、移除了 mock 假文案
- [ ] **Step 7: 全量（后端+前端）全绿 + Commit** `feat: 前端四步真实阶段展示+技能展示，移除 mock 播放器`

---

## 完成定义

- 后端 `cd backend && .venv/bin/python -m pytest -q` 全绿（含新增 skills/dispatcher 测试，基线 174 只增不减）
- 前端 `cd frontend && npm run typecheck && npm run build` 通过
- `app/ai/` 结构：`agent/` `skills/` `tools/` `dto.py` `gateway.py` `prompt.py`（或按最后结构）
- 无 `provider_cfg.py` 镜像、无 `ai/schemas.py`、无 `THINK_LINES` mock 播放器
- 内置 query + report 技能已注册，dispatcher 能路由（mock 下至少 report 关键词命中）
- 端到端：起后端 → 问一句查询 → 前端四步逐步点亮 + 检索显示候选表 + 结果进工作区；`test_hitl_invariant.py` 仍全绿（写操作永不无确认执行）

## 说明

- 本 plan 的第一批 task（1-6）是**后端结构调整 + 技能骨架**，无前端改动，每步可独立测试。
- Task 7-8（阶段事件 + 前端展示）在骨架稳固后接入。
- **MCP 本 plan 不涉及**（设计文档 §7：V1 只做统一工具层 + 预留适配器，团队网关时才实现）。
- **ops/数据运维技能本 plan 不做**（V1.5 多智能体场景）。
