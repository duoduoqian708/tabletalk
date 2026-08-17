# 设计文档：移除内置 Mock 模型 + 思考开关改为思考强度

日期：2026-08-17
状态：已与用户确认（brainstorming → 设计批准）

## 目标

1. **功能 A**：模型设置列表不再默认塞入内置 `Mock（内置）` 模型（`llm_mock`）。保留 mock provider 作为 "cloud 无 key 时静默降级" 的兜底，也允许用户手动添加 mock 模型。默认模型变为 DeepSeek（需配 key），无 key 时自动降级走 mock 行为，demo 不破。
2. **功能 B**：把对话区的 "推理思考" 布尔开关，改为 **思考强度** 四态控件（关闭 / 低 / 中 / 高），仅对检测支持推理的模型显示。后端按 OpenAI 协议统一映射为 `reasoning_effort`。

范围限定：本项目只走 OpenAI 兼容协议，故 reasonnding 强度 = `reasoning_effort` 字段，不再按模型关键字猜参数名。

## 功能 A：移除列表里的内置 Mock 选项

### 改动点（后端 `backend/app/core/settings.py`）
- 删除 `_builtin_mock()` 函数。
- `SettingsStore.__init__`（约 line 292、295）：默认 `ai_models` 不再包含 `_builtin_mock()`：
  - 有 env 模型时 → `[env_ai, _builtin_deepseek()]`
  - 否则 → `[_builtin_deepseek()]`
  - `default_ai_model` → `llm_deepseek`
- `_migrate_from_legacy`（约 line 239）：移除 `data["ai_models"].extend([_builtin_deepseek(), _builtin_mock()])` 中的 `_builtin_mock()`。
- `_ensure_builtins`（约 line 333-334）：移除 "若没有 `llm_mock` 就加回" 的逻辑（仅保留确保 `llm_deepseek` 存在）。

### 保留不变
- `gateway.is_effective_mock`：cloud 无 key 时静默降级为 mock 行为（保住无 key demo）。
- 模型添加 UI 的 provider 选项中保留 `mock`，用户可手动添加 `provider: mock` 模型。
- `_default_ai()` 的兜底 `ModelConfig(id="default-mock", provider="mock")` 保留为安全网（列表为空时才触发，正常不会发生）。

### 效果
- 无 key 时：`ai_provider` 默认 `mock` → 走 else 分支 → `ai_models=[deepseek]`，`default_ai_model=llm_deepseek`。DeepSeek 无 key → 静默降级 mock，demo 正常。
- 默认模型列表里不再出现 Mock 选项。

### 测试影响
- `tests/api/test_models.py::test_settings_has_ai_models_list`（line 16-17）：原断言默认列表含 `provider=="mock"`。改为：断言默认列表**不含** `id=="llm_mock"`，且包含 `provider=="cloud"`（deepseek）。
- `test_settings_put_ai_models_full_replace`（line 171-184）：用户手动传入 `llm_mock` 仍被保留，且 `llm_deepseek` 始终在；**不受影响**，无需改。
- 其余 mock 相关测试（`/ai/test` mock 模式、`_detect_reasoning`、`gateway` mock 流、report mock 流程）全部保留不动。

## 功能 B：思考开关 → 思考强度（对话级，OpenAI `reasoning_effort`）

### 前端（`frontend/src/renderer/src/components/AiRail.tsx`）
- 将 `wantThink` 布尔开关替换为四态控件：关闭 / 低 / 中 / 高（下拉或分段单选）。
- 显示门控不变：仅当 `defaultModel?.reasoning === true`（检测到推理能力）时显示；`provider === 'mock'` 仍禁用。
- 发送字段：`reasoning` 由 `bool | null` 改为 `str | null`，取值：
  - `"off"`（关闭，默认） / `"low"` / `"medium"` / `"high"`
  - 前端始终发送该字段；后端将 `"off"` 与 `None`/空 归一为"不追加任何思考参数"。
- 向后兼容：`wantThink` 旧语义（on = high）由新控件 "高" 覆盖。

### 后端

#### `app/ai/schemas.py`
- `ChatRequest.reasoning: bool | None = None` → `reasoning: str | None = None`，校验取值 ∈ {off, low, medium, high}（非法值按 off 处理）。

#### `app/ai/loop.py`（约 line 70-71）
- `cfg["reasoning"] = req.reasoning` 原样透传（字符串即可）。

#### `app/ai/gateway.py`
- 简化 `reasoning_supports_param()`：本项目只走 OpenAI 兼容协议，推理模型统一支持 `reasoning_effort`（去掉按 `deepseek/o1/qwen/glm...` 关键字的猜测逻辑；保留对 mock 返回 False）。
- 原 `reasoning` 布尔→`reasoning_effort` 的映射改为按强度：
  - `off` / `None` / 空 → 不追加任何思考参数（常规非思考请求）。
  - `low` / `medium` / `high` 且 `reasoning_supports_param()` → 请求体追加 `reasoning_effort: "low" | "medium" | "high"`。
  - 仅支持布尔思考、不支持 effort 的模型：任何非 off → 启用思考（保持原 `reasoning` 开启语义）；`reasoning_effort` 仅对支持者下发。
  - 不支持该参数的模型：忽略，无副作用（OpenAI 兼容网关通常忽略未知字段）。

#### 检测（`app/api/ai.py` `_detect_reasoning`）
- 保持布尔检测（是否支持推理），仅用于 **前端决定是否显示强度控件** 与能力徽标。
- 不尝试检测具体强度档位（不可靠）；强度映射尽力而为。

### 测试影响
- `tests/ai/test_loop.py`：调整 `reasoning` 用例，覆盖 `reasoning="high"` / `"off"` 透传；原布尔用例改为字符串或删除。
- `app/ai/gateway.py` 单测：覆盖 `low`/`medium`/`high` → `reasoning_effort` 映射；`off`/None → 不发参数；不支持参数时忽略。
- 保留 `_detect_reasoning` / `_detect_reasoning_streaming` 测试。

## 验证

- 后端：`cd backend && .venv/bin/python -m pytest -q` 全绿（含更新后的 mock 列表断言与新增强度测试）。
- 前端：`cd frontend && npm run typecheck && npm run build` 通过。
- 手动：
  - 系统设置 → 大模型：默认模型列表无 Mock 选项；手动添加 `provider: mock` 仍可。
  - 对话区：支持推理的模型显示强度四态（关闭/低/中/高）；发送后在网络/后端日志可见 `reasoning_effort` 字段；mock 模型不显示该控件。

## 非目标（明确不做）
- 不彻底移除 mock provider / 不删除 cloud 无 key 静默降级。
- 不做模型级或全局级思考强度（仅对话级）。
- 不做强度档位的精确检测（只检测"是否支持推理"）。
- 不改动 `app/safety/` 安全门逻辑。
