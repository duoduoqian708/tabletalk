"""LLM 网关：统一 OpenAI 兼容协议（云端 API / 本地 Ollama / vLLM 私有网关）。

内置 MockProvider：无 key 也能跑通全流程，复刻原型的读/写/DDL 意图。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class StreamChunk:
    """流式增量：delta=文本增量；content=非流式最终文本（mock）；tool_calls=最终工具调用。"""
    delta: str | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


def build_provider(cfg: dict) -> "LLMGateway":
    return LLMGateway(cfg)


def is_effective_mock(cfg: dict) -> bool:
    """是否实际走 mock 行为：显式 mock，或开箱默认 cloud 模型无 key 时的降级。"""
    return cfg.get("provider") == "mock" or (cfg.get("provider") == "cloud" and not cfg.get("api_key"))


class LLMGateway:
    def __init__(self, cfg: dict) -> None:
        self.provider = cfg.get("provider", "mock")
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or ""
        self.temperature = cfg.get("temperature", 0.2)
        self.timeout = cfg.get("timeout", 120)
        # 推理强度：off/low/medium/high（None/空/False 视为 off；True 视为 high）
        self.reasoning = cfg.get("reasoning")

    def _request(self, messages: list[dict], tools: list[dict] | None, stream: bool) -> tuple[str, dict, dict]:
        """构造 POST /chat/completions 的 url / payload / headers（chat 与 chat_stream 共用）。"""
        if not self.base_url and self.provider != "mock":
            raise ValueError("AI 网关未配置 base_url（provider 非 mock 时必填）")
        url = self.base_url + "/chat/completions"
        payload: dict = {"messages": messages, "temperature": self.temperature, "stream": stream}
        if self.model:
            payload["model"] = self.model
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # 思考强度（OpenAI 兼容统一用 reasoning_effort）
        # 归一：布尔能力标志也兼容（True→high, False→off）；off/None/空 → 不追加参数
        r = self.reasoning
        if r is True:
            r = "high"
        elif r is False or r in (None, ""):
            r = "off"
        if r != "off" and self.reasoning_supports_param():
            effort = {"low": "low", "medium": "medium", "high": "high"}.get(r)
            if effort:
                payload["reasoning_effort"] = effort
            else:
                # 不支持 effort 档位的推理模型：仅启用思考
                payload["thinking"] = {"type": "enabled"}
        headers: dict = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return url, payload, headers

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, allow_fallback: bool = True) -> ChatResponse:
        # 开箱默认 cloud 模型无 key 时静默降级 mock，保住无 key demo；
        # 测试连接传 allow_fallback=False 保持真实（无 key/坏 URL 即真实报错）
        if self.provider == "mock" or (allow_fallback and self.provider == "cloud" and not self.api_key):
            return await MockProvider.chat(messages, tools)
        url, payload, headers = self._request(messages, tools, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        # 带思考链的模型返回 reasoning_content（思考内容本身不展示，仅取正式回答）
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{int(time.time()*1000)}",
                    name=tc["function"].get("name", ""),
                    arguments=args,
                )
            )
        return ChatResponse(content=content, tool_calls=tool_calls)

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None, allow_fallback: bool = True) -> "AsyncIterator[StreamChunk]":
        """SSE 流式：逐 token 产出 StreamChunk(delta)；工具调用在流末一次性产出。"""
        if self.provider == "mock" or (allow_fallback and self.provider == "cloud" and not self.api_key):
            resp = await MockProvider.chat(messages, tools)
            if resp.tool_calls:
                yield StreamChunk(tool_calls=resp.tool_calls)
            else:
                yield StreamChunk(content=resp.content)
            return
        url, payload, headers = self._request(messages, tools, stream=True)
        acc: dict[int, dict] = {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta", {})
                    for t in delta.get("tool_calls") or []:
                        idx = t.get("index", 0)
                        slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if t.get("id"):
                            slot["id"] = t["id"]
                        fn = t.get("function", {})
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        slot["arguments"] += fn.get("arguments") or ""
                    text = delta.get("content")
                    if text:
                        yield StreamChunk(delta=text)
        if acc:
            calls: list[ToolCall] = []
            for i in sorted(acc):
                slot = acc[i]
                try:
                    args = json.loads(slot["arguments"] or "{}")
                except Exception:
                    args = {}
                calls.append(ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=args))
            yield StreamChunk(tool_calls=calls)

    def reasoning_supports_param(self) -> bool:
        """本项目仅走 OpenAI 兼容协议；非 mock 模型即视为可接收 reasoning_effort/thinking 参数。"""
        return self.provider != "mock"


class MockProvider:
    """按意图关键词生成 tool call；收到 tool 结果后收尾。"""

    @classmethod
    async def chat(cls, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        if messages and messages[-1].get("role") == "tool":
            return cls._final_text(messages[-1])
        q = cls._last_user(messages)
        return cls._intent_tool_call(q)

    @staticmethod
    def _last_user(messages: list[dict]) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return m.get("content", "")
        return ""

    @staticmethod
    def _intent_tool_call(q: str) -> ChatResponse:
        ql = q.lower()
        if any(k in ql for k in ("提价", "涨价", "10%", "库存为 0", "price")):
            return ChatResponse(tool_calls=[ToolCall(
                id="call_mock_dml", name="run_dml",
                arguments={"sql": "UPDATE products\nSET price = price * 1.1\nWHERE stock = 0;"},
            )])
        if any(k in ql for k in ("索引", "ddl", "建表", "删表", "改表", "alter", "drop", "create index")):
            return ChatResponse(tool_calls=[ToolCall(
                id="call_mock_ddl", name="draft_ddl",
                arguments={"sql": "CREATE INDEX idx_orders_order_date\nON orders (order_date);"},
            )])
        if any(k in ql for k in ("字段", "列", "结构", "describe", "有哪些")):
            return ChatResponse(tool_calls=[ToolCall(
                id="call_mock_desc", name="describe_table", arguments={"table": "orders"},
            )])
        if any(k in ql for k in ("退货", "return_rate", "退款")):
            return ChatResponse(tool_calls=[ToolCall(
                id="call_mock_q1", name="run_query",
                arguments={"sql": "SELECT p.product_name,\n       COUNT(*) AS orders,\n       COUNT(r.id) AS returns,\n       ROUND(COUNT(r.id) * 100.0 / COUNT(*), 1) AS return_rate\nFROM products p\nJOIN order_items oi ON oi.product_id = p.id\nLEFT JOIN returns r ON r.order_item_id = oi.id\nGROUP BY p.product_name\nORDER BY return_rate DESC\nLIMIT 10;"},
            )])
        if any(k in ql for k in ("客户", "clv", "生命周期", "价值", "revenue")):
            return ChatResponse(tool_calls=[ToolCall(
                id="call_mock_q3", name="run_query",
                arguments={"sql": "SELECT c.name, c.region, COUNT(DISTINCT o.id) AS orders, SUM(oi.quantity * oi.unit_price) AS revenue\nFROM customers c\nJOIN orders o ON o.customer_id = c.id\nJOIN order_items oi ON oi.order_id = o.id\nGROUP BY c.id\nORDER BY revenue DESC\nLIMIT 6;"},
            )])
        if any(k in ql for k in ("周转", "积压", "滞销", "库存", "inventory")):
            return ChatResponse(tool_calls=[ToolCall(
                id="call_mock_q4", name="run_query",
                arguments={"sql": "SELECT p.product_name, c.name AS category, i.qty AS stock, i.last_moved_at\nFROM inventory i\nJOIN products p ON p.id = i.product_id\nJOIN categories c ON c.id = p.category_id\nORDER BY i.last_moved_at ASC\nLIMIT 6;"},
            )])
        if any(k in ql for k in ("总结", "这堆", "最严重", "结果")):
            return ChatResponse(content="我基于当前结果的列名与行数回答：明细数据未回传模型。想深入的话，告诉我按哪个维度重新聚合。")
        # 通用兜底：mock 只内置了演示库（orders/products/...）的关键词，无法为任意真实库生成准确 SQL。
        # 直接引导用户配置真实模型，而不是编造一张大概率不存在的表。
        return ChatResponse(content="当前为 mock 模式（仅演示用），无法为你的实际数据生成准确 SQL。请在「系统设置 → 大模型接入」配置真实模型后再试。")

    @staticmethod
    def _final_text(tool_msg: dict) -> ChatResponse:
        name = tool_msg.get("name") or ""
        try:
            content = json.loads(tool_msg.get("content") or "{}")
        except Exception:
            content = {}
        if name == "run_query":
            if content.get("ok"):
                return ChatResponse(content=f"已生成只读查询并通过安全闸门放行，结果推送到左侧工作区（{content.get('row_count', 0)} 行）。明细未回传模型。")
            return ChatResponse(content=f"该查询被安全闸门拦截：{content.get('reason', '未知原因')}")
        if name == "run_dml":
            if content.get("verdict") == "review":
                return ChatResponse(content=f"这是写操作，安全闸门判定需确认，预估影响 {content.get('preview_rows', '?')} 行。请在卡片上确认后执行。")
            return ChatResponse(content=f"写操作被拦截：{content.get('reason', '')}")
        if name == "draft_ddl":
            return ChatResponse(content="DDL 脚本已生成并发送到编辑器——我的工具集里没有 DDL 工具，改表结构需要你手动执行。")
        if name == "describe_table":
            return ChatResponse(content="已返回该表的列定义与注释（只读结构）。")
        if name == "get_schema":
            return ChatResponse(content="已返回连接的表结构摘要（只读结构）。")
        return ChatResponse(content="完成。")


def resolve_provider_cfg(state, req) -> dict[str, Any]:
    """解析生效的 AI provider 配置：model_id 命中 ai_models 优先，支持逐次覆盖与 reasoning。

    loop（查询）与 report（报告）共用，保证两模式对「按对话切模型 + 思考强度」行为一致。
    """
    rs = state.runtime.get()
    mid = getattr(req, "model_id", None)
    if mid:
        target = next((m for m in rs.ai_models if m.id == mid), None)
        if target is not None:
            cfg = {
                "provider": target.provider,
                "base_url": target.base_url,
                "api_key": target.api_key,
                "model": target.model,
                "temperature": target.temperature,
                "timeout": target.timeout,
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
