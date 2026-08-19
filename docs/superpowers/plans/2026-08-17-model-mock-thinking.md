# 移除内置 Mock 模型 + 思考开关改思考强度 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模型列表不再默认塞入内置 Mock 模型（保留 mock provider 静默降级与手动添加）；对话区"推理思考"开关改为对话级"思考强度"四态（关闭/低/中/高），后端按 OpenAI 协议统一映射 `reasoning_effort`。

**Architecture:** 后端 `settings.py` 删除内置 mock 模型；`schemas.py`/`loop.py` 把 `ChatRequest.reasoning` 从 `bool` 改为强度字符串；`gateway.py` 把强度映射为 `reasoning_effort` 并简化 `reasoning_supports_param`（仅 OpenAI 兼容）。前端 `ai.ts` 类型改为强度枚举，`AiRail.tsx` 用分段选择替换布尔开关。

**Tech Stack:** Python 3.13 + FastAPI + Pydantic v2；React 18 + TypeScript（Vite）。测试：pytest（`asyncio_mode=auto`）、`tsc --noEmit`。

---

## File Structure

- **Modify** `backend/app/core/settings.py` — 删除 `_builtin_mock()` 及其所有引用（`__init__`、`_migrate_from_legacy`、`_ensure_builtins`）。
- **Modify** `backend/app/ai/schemas.py` — `ChatRequest.reasoning` 由 `bool | None` 改为 `Literal["off","low","medium","high"] | None`。
- **Modify** `backend/app/ai/loop.py` — `_provider_cfg` 透传字符串强度（基本无需改，仅需确认）。
- **Modify** `backend/app/ai/gateway.py` — 简化 `reasoning_supports_param()`；`_request` 中按强度映射 `reasoning_effort`。
- **Modify** `backend/app/api/ai.py` — `_detect_reasoning_streaming` 沿用简化后的 `reasoning_supports_param()`（代码不变，行为随之收敛）。
- **Create** `backend/tests/ai/test_gateway_reasoning.py` — gateway 强度映射单测。
- **Modify** `backend/tests/api/test_models.py` — 更新默认列表不含 `llm_mock` 的断言。
- **Modify** `backend/tests/ai/test_loop.py` — `ChatRequest(reasoning=False)` → `reasoning="off"`。
- **Modify** `frontend/src/renderer/src/api/ai.ts` — `ChatParams.reasoning` 改为 `ReasoningEffort | null`，新增类型。
- **Modify** `frontend/src/renderer/src/components/AiRail.tsx` — 强度四态控件替换布尔开关。

---

### Task 1: 后端移除内置 Mock 模型

**Files:**
- Modify: `backend/app/core/settings.py`
- Test: `backend/tests/api/test_models.py`

- [ ] **Step 1: 修改失败测试（先红）**

`test_settings_has_ai_models_list` 当前断言默认含 mock，改为断言**不含**内置 mock 且含 cloud(deepseek)：

```python
    # 默认列表不应含内置 mock 模型
    ids = {m["id"] for m in body["ai_models"]}
    assert "llm_mock" not in ids
    # 默认应有 cloud(deepseek) 内置模型
    assert "llm_deepseek" in ids
    providers = {m["provider"] for m in body["ai_models"]}
    assert "cloud" in providers
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_models.py::test_settings_has_ai_models_list -q`
Expected: FAIL（`assert "llm_mock" not in ids` 失败，因为当前仍塞入 mock）。

- [ ] **Step 3: 实现 — 删除内置 mock**

`settings.py`：
1. 删除函数：
```python
def _builtin_mock() -> dict[str, Any]:
    return asdict(ModelConfig(id="llm_mock", name="Mock（内置）", provider="mock", builtin=True))
```
2. `__init__`（约 292）：`self._data["ai_models"] = [asdict(env_ai), _builtin_deepseek()]`（去掉 `_builtin_mock()`）。
3. `__init__`（约 295）：`self._data["ai_models"] = [_builtin_deepseek()]`（去掉 `_builtin_mock()`）。
4. `_migrate_from_legacy`（约 239）：`data["ai_models"].extend([_builtin_deepseek()])`（去掉 `_builtin_mock()`）。
5. `_ensure_builtins`（约 333-334）：删除以下两行：
```python
        if "llm_mock" not in ids:
            models.append(_builtin_mock())
```
保留 `llm_deepseek` 的确保逻辑不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_models.py -q`
Expected: PASS（含 `test_settings_put_ai_models_full_replace`，其手动传入的 `llm_mock` 仍被保留，断言 `llm_deepseek` 在 — 不受影响）。

- [ ] **Step 5: 提交**

```bash
cd /Users/mac/demo-project/tabletalk && git add backend/app/core/settings.py backend/tests/api/test_models.py
git commit -m "feat: 模型列表不再默认塞入内置 Mock 模型（保留 mock provider 静默降级）"
```

---

### Task 2: schemas 把 reasoning 改为强度字符串

**Files:**
- Modify: `backend/app/ai/schemas.py`
- Modify: `backend/tests/ai/test_loop.py`

- [ ] **Step 1: 写失败测试**

在 `tests/ai/test_loop.py` 中，`test_provider_cfg_resolves_model_id` 的 `reasoning=False` 覆盖用例改为字符串（因为 `ChatRequest.reasoning` 即将变为强度枚举）：

```python
    # 显式 reasoning 覆盖选中模型（强度字符串）
    cfg_ov = _provider_cfg(_FakeState(), ChatRequest(connection_id="c", model_id="m2", reasoning="off"))
    assert cfg_ov["reasoning"] == "off"
```
（同时把原 `assert cfg_ov["reasoning"] is False` 改为 `== "off"`；其余 `reasoning=True` 出现在 `ModelConfig(...)` 构造与 `provider_config()` 返回里，那是**模型能力**标志，保持 `bool` 不变。）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/ai/test_loop.py::test_provider_cfg_resolves_model_id -q`
Expected: FAIL（pydantic 拒绝 `reasoning=False` 非枚举值）。

- [ ] **Step 3: 实现 — schemas 改类型**

`backend/app/ai/schemas.py` 顶部加入：
```python
from typing import Literal
```
并把（约 line 30-31）：
```python
    # 推理思考开关（None=跟随模型配置；显式传 True/False 覆盖本次对话）
    reasoning: bool | None = None
```
改为：
```python
    # 思考强度（对话级）：off=关闭(默认) | low | medium | high
    # None/空 视为 off；非法值由 pydantic 在请求校验阶段拒绝
    reasoning: Literal["off", "low", "medium", "high"] | None = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/ai/test_loop.py -q`
Expected: PASS（`cfg["reasoning"] is True` 来自 `ModelConfig.reasoning` 能力标志，仍为 bool，不受影响；`reasoning="off"` 透传正确）。

- [ ] **Step 5: 提交**

```bash
cd /Users/mac/demo-project/tabletalk && git add backend/app/ai/schemas.py backend/tests/ai/test_loop.py
git commit -m "feat: ChatRequest.reasoning 改为思考强度枚举(off/low/medium/high)"
```

---

### Task 3: gateway 强度映射 + 简化 supports_param

**Files:**
- Modify: `backend/app/ai/gateway.py`
- Create: `backend/tests/ai/test_gateway_reasoning.py`

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/ai/test_gateway_reasoning.py`：

```python
"""gateway 思考强度 → reasoning_effort 映射测试。"""
from app.ai.gateway import LLMGateway


def _payload(reasoning):
    gw = LLMGateway({
        "provider": "cloud", "model": "gpt-5",
        "base_url": "https://api.example.com/v1", "api_key": "k",
        "reasoning": reasoning,
    })
    _, payload, _ = gw._request([], None, False)
    return payload


def test_low_maps_to_effort_low():
    assert _payload("low").get("reasoning_effort") == "low"


def test_medium_maps_to_effort_medium():
    assert _payload("medium").get("reasoning_effort") == "medium"


def test_high_maps_to_effort_high():
    assert _payload("high").get("reasoning_effort") == "high"


def test_off_no_effort_param():
    assert "reasoning_effort" not in _payload("off")


def test_none_no_effort_param():
    assert "reasoning_effort" not in _payload(None)


def test_mock_never_sends_effort():
    gw = LLMGateway({"provider": "mock", "model": "mock", "reasoning": "high"})
    _, payload, _ = gw._request([], None, False)
    assert "reasoning_effort" not in payload
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/ai/test_gateway_reasoning.py -q`
Expected: FAIL（当前代码对 `low` 走 `thinking enabled` 分支、对 `mock` 仍可能注入，且不支持 medium）。

- [ ] **Step 3: 实现 — 简化 supports_param**

`gateway.py` 把 `reasoning_supports_param`（约 160-168）替换为：
```python
    def reasoning_supports_param(self) -> bool:
        """本项目仅走 OpenAI 兼容协议；非 mock 模型即视为可接收 reasoning_effort/thinking 参数。"""
        return self.provider != "mock"
```

- [ ] **Step 4: 实现 — _request 强度映射**

`gateway.py` `_request` 中（约 66-77）把：
```python
        # 推理开关：按模型供应商选择参数形态（Unknown 端点不传，走模型默认）
        if self.reasoning is not None and self.reasoning_supports_param():
            if self.reasoning:
                if "o1" in self.model or "o3" in self.model or "o4" in self.model:
                    payload["reasoning_effort"] = "high"
                else:
                    payload["thinking"] = {"type": "enabled"}
            else:
                if "o1" in self.model or "o3" in self.model or "o4" in self.model:
                    payload["reasoning_effort"] = "low"
                else:
                    payload["thinking"] = {"type": "disabled"}
```
替换为：
```python
        # 思考强度（OpenAI 兼容统一用 reasoning_effort）：off/None/空 → 不追加参数
        if self.reasoning not in (None, "", "off") and self.reasoning_supports_param():
            effort = {"low": "low", "medium": "medium", "high": "high"}.get(self.reasoning)
            if effort:
                payload["reasoning_effort"] = effort
            else:
                # 不支持 effort 档位的推理模型：仅启用思考
                payload["thinking"] = {"type": "enabled"}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/ai/test_gateway_reasoning.py tests/api/test_models.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
cd /Users/mac/demo-project/tabletalk && git add backend/app/ai/gateway.py backend/tests/ai/test_gateway_reasoning.py
git commit -m "feat: gateway 思考强度按 OpenAI reasoning_effort 映射并简化 supports_param"
```

---

### Task 4: 前端强度四态控件

**Files:**
- Modify: `frontend/src/renderer/src/api/ai.ts`
- Modify: `frontend/src/renderer/src/components/AiRail.tsx`

- [ ] **Step 1: 前端类型改为强度枚举**

`frontend/src/renderer/src/api/ai.ts` 在文件顶部（import 之后）新增：
```typescript
export type ReasoningEffort = 'off' | 'low' | 'medium' | 'high'
```
并把 `ChatParams` 接口里的（约 line 75-76）：
```typescript
  /** 推理思考开关：显式传 True/False 覆盖本次对话；None 跟随模型配置 */
  reasoning?: boolean | null
```
改为：
```typescript
  /** 思考强度（对话级）：off=关闭 | low | medium | high；仅支持推理的模型可用 */
  reasoning?: ReasoningEffort | null
```
（`GatewayCapabilities.reasoning` 仍是 `boolean | null` 的**能力探测**结果，不改。）

- [ ] **Step 2: AiRail 状态与发送**

`frontend/src/renderer/src/components/AiRail.tsx`：
1. 把（约 317-320）：
```typescript
  // 默认模型是否支持推理 → 决定是否显示「推理思考」开关
  const defaultModel = settings?.ai_models.find((m) => m.id === settings.default_ai_model)
  const supportsReasoning = !!defaultModel?.reasoning
  const [wantThink, setWantThink] = useState(true)
```
改为：
```typescript
  // 默认模型是否支持推理 → 决定是否显示「思考强度」控件
  const defaultModel = settings?.ai_models.find((m) => m.id === settings.default_ai_model)
  const supportsReasoning = !!defaultModel?.reasoning
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('off')
```
2. 发送处（约 565）`reasoning: supportsReasoning ? wantThink : null,` 改为：
```typescript
          reasoning: supportsReasoning ? reasoningEffort : null,
```

- [ ] **Step 3: AiRail 控件 JSX**

把（约 1020-1029）的 `推理` 按钮：
```tsx
          {supportsReasoning && (
            <button
              className={`rpt-btn think${wantThink ? ' on' : ''}`}
              title={wantThink ? '推理思考开启：模型先思考再回答' : '推理思考关闭'}
              disabled={busy}
              onClick={() => setWantThink((v) => !v)}
            >
              推理{wantThink ? ' ✓' : ''}
            </button>
          )}
```
替换为分段选择：
```tsx
          {supportsReasoning && (
            <select
              className="rpt-btn think"
              value={reasoningEffort}
              disabled={busy}
              title="思考强度：关闭 / 低 / 中 / 高（仅支持推理的模型）"
              onChange={(e) => setReasoningEffort(e.target.value as ReasoningEffort)}
            >
              <option value="off">思考：关</option>
              <option value="low">思考：低</option>
              <option value="medium">思考：中</option>
              <option value="high">思考：高</option>
            </select>
          )}
```

- [ ] **Step 4: 类型检查 + 构建**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: 均无错误，`vite build` 成功。

- [ ] **Step 5: 提交**

```bash
cd /Users/mac/demo-project/tabletalk && git add frontend/src/renderer/src/api/ai.ts frontend/src/renderer/src/components/AiRail.tsx
git commit -m "feat: 前端对话级思考强度四态控件(off/low/medium/high)"
```

---

### Task 5: 全量回归

**Files:** 无新增，仅验证。

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全部 PASS（含 Task 1-3 新测试；`test_detect_reasoning_*` 仍 PASS —— `reasoning_supports_param` 简化后，`_detect_reasoning_streaming` 对非 mock 模型会在探测时启用思考，非推理模型返回 400 被判为不支持，符合预期）。

- [ ] **Step 2: 前端类型检查 + 构建**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: 均无错误。

- [ ] **Step 3: 提交回归确认（可选，若前面已逐任务提交则跳过）**

```bash
cd /Users/mac/demo-project/tabletalk && git status --short
```
Expected: 工作区干净（所有改动已在各任务提交）。

---

## 非目标（明确不做）
- 不彻底删除 mock provider / 不删 cloud 无 key 静默降级。
- 不做模型级或全局级思考强度。
- 不做强度档位精确检测（仅检测"是否支持推理"以决定控件显隐）。
- 不改动 `app/safety/` 安全门。
