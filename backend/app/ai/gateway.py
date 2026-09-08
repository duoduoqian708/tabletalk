"""LLM 网关：统一 OpenAI 兼容协议（云端 API / 本地 Ollama / vLLM 私有网关）。

内置 MockProvider：无 key 也能跑通全流程，复刻原型的读/写/DDL 意图。
思考等协议差异由 app/ai/providers/ 适配层收编：上层只说统一方言
（thinking: off/low/medium/high/auto），各家字段/枚举/降级矩阵在 adapter 内。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.ai.providers import get_adapter
from app.ai.providers.base import normalize_thinking


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None  # OpenAI 兼容: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}


@dataclass
class StreamChunk:
    """流式增量：delta=文本增量；reasoning=思考链增量（推理模型）；content=非流式最终文本（mock）；tool_calls=最终工具调用。"""
    delta: str | None = None
    reasoning: str | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


def build_provider(cfg: dict) -> "LLMGateway":
    return LLMGateway(cfg)


def _sanitize_payload(payload: dict) -> dict:
    """精简请求 payload 用于日志：截断 messages 内容，移除 api_key 等敏感字段。"""
    import copy
    out = copy.deepcopy(payload)
    # 截断每条 message 的 content（防超大上下文撑爆磁盘，保留前2KB）
    for m in out.get("messages", []):
        if isinstance(m.get("content"), str) and len(m["content"]) > 2048:
            m["content"] = m["content"][:2048] + f"...(truncated, total {len(m['content'])} chars)"
    # 工具列表只保留名字
    if "tools" in out:
        out["tools"] = [t.get("function", {}).get("name", "?") for t in out["tools"]]
    return out


def is_effective_mock(cfg: dict) -> bool:
    """是否实际走 mock 行为：provider 字段为 "mock"（内置模拟，无 key 可跑通全流程）。"""
    return cfg.get("provider") == "mock"


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"].replace("\n", " ")
    return ""


class LLMGateway:
    def __init__(self, cfg: dict) -> None:
        self.provider = cfg.get("provider", "mock")
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or ""
        self.temperature = cfg.get("temperature", 0.2)
        self.timeout = cfg.get("timeout", 120)
        # 统一思考方言（归一后 ∈ off/low/medium/high/auto；True/False/None 等历史形态在归一层兼容）
        self.reasoning = cfg.get("reasoning")
        # 适配器：协议差异（思考字段/枚举/流式思考字段/回填约束）全部收编于此
        self.adapter = get_adapter(self.provider, self.model)
        # 工具循环内本实例存活复用：累积上一轮流式思考链，供 DeepSeek 等强制回传的厂商回填
        self._last_reasoning: str | None = None
        self._degrade_notes: list[str] = []

    def _request(self, messages: list[dict], tools: list[dict] | None, stream: bool) -> tuple[str, dict, dict]:
        """构造 POST /chat/completions 的 url / payload / headers（chat 与 chat_stream 共用）。"""
        if not self.base_url and self.provider != "mock":
            raise ValueError("AI 网关未配置 base_url（provider 非 mock 时必填）")
        url = self.base_url + "/chat/completions"
        payload: dict = {"messages": messages, "temperature": self.temperature, "stream": stream}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if self.model:
            payload["model"] = self.model
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # 统一思考方言 → 各家参数（适配层内完成映射与降级；降级说明进 last_meta 供审计）
        thinking = normalize_thinking(self.reasoning)
        has_tools = bool(tools)
        notes = self.adapter.apply_thinking(payload, thinking, self.model, has_tools)
        notes += self.adapter.patch_messages(messages, thinking, has_tools, self._last_reasoning)
        self._degrade_notes = [n.as_note() for n in notes]
        headers: dict = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return url, payload, headers

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, allow_fallback: bool = True, ctx: dict | None = None) -> ChatResponse:
        # 显式 mock（测试/演示）走内置模拟；真实 provider 需配置 base_url
        # 测试连接传 allow_fallback=False 保持真实（无 key/坏 URL 即真实报错）
        _t0 = time.monotonic()
        try:
            if self.provider == "mock":
                self._record_egress(ctx, messages)  # 出网清单先行
                resp = await MockProvider.chat(messages, tools)
                self._record_llm(ctx, messages, {"messages": messages}, None, time.monotonic() - _t0, ok=True)
                return resp
            url, payload, headers = self._request(messages, tools, stream=False)
            self._record_egress(ctx, messages)  # 出网清单先于模型调用落审计
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content")
            # 累积思考链（供多轮工具循环回填 DeepSeek 等强制回传的厂商；思考内容本身不展示）
            rc = msg.get(self.adapter.reasoning_field)
            if rc:
                self._last_reasoning = rc
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
            usage = data.get("usage")
            # 存 last_meta 供调用方写 LLM 日志（请求 payload + 响应用量 + 协议降级说明）
            self.last_meta: dict = {
                "request_payload": _sanitize_payload(payload),
                "response_usage": usage,
                "response_model": data.get("model", self.model),
                "degrade": self._degrade_notes,
            }
            self._record_llm(ctx, messages, payload, usage, time.monotonic() - _t0, ok=True)
            return ChatResponse(content=content, tool_calls=tool_calls, usage=usage)
        except Exception as _exc:  # noqa: BLE001
            # 失败也记（token=0，可追溯），绝不漏记
            self._record_llm(ctx, messages, None, None, time.monotonic() - _t0, ok=False, note=str(_exc) or type(_exc).__name__)
            raise

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None, allow_fallback: bool = True, ctx: dict | None = None) -> "AsyncIterator[StreamChunk]":
        """SSE 流式：逐 token 产出 StreamChunk(delta)；工具调用在流末一次性产出。
        ctx 提供时在 finally 记账：流完成/被中断/异常都不漏（已累积的 usage 一并落）。"""
        _t0 = time.monotonic()
        _payload: dict | None = None
        _usage: dict | None = None
        try:
            if self.provider == "mock":
                self._record_egress(ctx, messages)  # 出网清单先行
                resp = await MockProvider.chat(messages, tools)
                if resp.tool_calls:
                    yield StreamChunk(tool_calls=resp.tool_calls)
                else:
                    yield StreamChunk(content=resp.content)
                return
            url, payload, headers = self._request(messages, tools, stream=True)
            self._record_egress(ctx, messages)  # 出网清单先于流式发送落审计
            _payload = payload
            acc: dict[int, dict] = {}
            _stream_usage: dict | None = None
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
                        # OpenAI 兼容：stream_options.include_usage=True 时，usage 在最后一条 chunk
                        chunk_usage = obj.get("usage")
                        if chunk_usage:
                            _stream_usage = chunk_usage
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
                        reasoning = delta.get(self.adapter.reasoning_field)
                        if reasoning:
                            # 累积思考链：同实例下一次请求回填（DeepSeek 思考+tools 强制）
                            self._last_reasoning = (self._last_reasoning or "") + reasoning
                        if reasoning or text:
                            yield StreamChunk(delta=text, reasoning=reasoning)
            _usage = _stream_usage
            # 流结束后存 last_meta（铁律：流完成再写，不丢信息）+ 协议降级说明
            self.last_meta = {
                "request_payload": _sanitize_payload(payload),
                "response_usage": _stream_usage,
                "response_model": _stream_usage.get("model") if _stream_usage else self.model,
                "degrade": self._degrade_notes,
            }
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
        finally:
            # 无条件记账：流完成/被中断/异常都不漏（已累积 usage 一并落）。
            # 按退出状态记 ok：正常完成 ok=True；异常（HTTP/解析）ok=False + note；
            # 客户端提前 aclose（GeneratorExit）→ ok=False + note="interrupted"（与 chat 的 except 语义一致）。
            _exc = sys.exc_info()[0]
            if _exc is None:
                _ok, _note = True, ""
            elif _exc is GeneratorExit:
                _ok, _note = False, "interrupted"
            else:
                _ok, _note = False, str(_exc) or _exc.__name__
            self._record_llm(ctx, messages, _payload, _usage, time.monotonic() - _t0, ok=_ok, note=_note)

    # ---------------------------------------------------------------- 中央记账拦截器
    # 一次请求/响应只记一组（llm_log 成本 + cost_tracker 摘要 + egress 出网清单审计）。
    # 由调用方传 ctx 开启（conn_id/skill/source/candidate_tables…）；后续全站点迁移后改为必记。

    def _record_llm(self, ctx: dict | None, messages: list[dict], payload: dict | None,
                    usage: dict | None, elapsed_ms: float, ok: bool = True, note: str = "") -> None:
        """LLM 调用记账（无条件拦截器）：llm_log + cost_tracker + egress 清单审计。
        ctx 缺省时用通用默认（skill=llm / connection=unknown），保证任何调用点都不漏。失败也记。"""
        try:
            from app.config import get_env
            from app.ai.llm_log import LlmCallLog
            from app.ai.cost_tracker import CostTracker

            env = get_env()
            cid = ctx.get("conn_id") if ctx else None
            skill = ctx.get("skill") if ctx else "llm"
            model = ctx.get("model") or self.model or ""
            provider = ctx.get("provider") or self.provider
            sid = ctx.get("session_id") if ctx else None
            in_t = int((usage or {}).get("prompt_tokens") or 0)
            out_t = int((usage or {}).get("completion_tokens") or 0)
            elapsed = round(float(elapsed_ms), 1)

            LlmCallLog(env.data_dir).log(
                conn_id=cid, skill=skill, model=model, provider=provider, session_id=sid,
                request_json=_sanitize_payload(payload) if payload else None,
                response_json=usage if ok else {"error": (note or "error")[:300]},
                input_tokens=in_t, output_tokens=out_t, elapsed_ms=elapsed,
            )
            CostTracker(env.data_dir).log(
                connection=cid, skill=skill, model=model, provider=provider,
                input_tokens=in_t, output_tokens=out_t, elapsed_ms=elapsed,
            )
        except Exception:  # noqa: BLE001 - 记账失败绝不影响主流程
            pass

    def _record_egress(self, ctx: dict | None, messages: list[dict]) -> None:
        """出网清单审计行（verdict=egress + manifest）。在模型发送前调用（清单先于出网）。
        站点已建权威清单时经 ctx['manifest'] 原样审计（内容逐字一致）；否则按 build_manifest 重建。"""
        try:
            from app.state import get_state  # noqa: PLC0415 - 延迟导入避免循环

            cid = ctx.get("conn_id") if ctx else None
            skill = ctx.get("skill") if ctx else "llm"
            model = ctx.get("model") or self.model or ""
            provider = ctx.get("provider") or self.provider
            state = get_state()
            context_meta = (ctx.get("context_meta") if ctx else None) or {}
            conn_label = (ctx.get("connection") if ctx else None) or cid or "unknown"
            prebuilt = ctx.get("manifest") if ctx else None
            if prebuilt and isinstance(prebuilt, dict):
                m = prebuilt
            else:
                provider_cfg = {"model": model, "provider": provider}
                try:
                    from app.ai.manifest import build_manifest  # noqa: PLC0415
                    m = build_manifest(state, conn_label, context_meta, messages, bool((ctx or {}).get("include_data")), provider_cfg)
                except Exception:  # noqa: BLE001
                    import time as _t  # noqa: PLC0415
                    m = {"model": model, "provider": provider, "ts": _t.strftime("%Y-%m-%dT%H:%M:%S"), "tables": list(context_meta.get("candidate_tables") or []), "kb_docs": 0, "history_turns": 0, "include_data": False, "redactions": [], "mode": "standard"}
                if (ctx or {}).get("redactions"):
                    m["redactions"] = ctx["redactions"][:5]
            state.audit.log(
                connection=conn_label, origin="ai", tier="read", verdict="egress",
                status=(ctx.get("status") if ctx else None) or "egress",
                source=(ctx.get("source") if ctx else None) or "llm",
                sql=f"[{skill}] {_last_user_text(messages)[:64]}",
                manifest=m,
            )
        except Exception:  # noqa: BLE001
            pass


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
            from app.core.settings import reasoning_for_model

            cfg = {
                "provider": target.provider,
                "base_url": target.base_url,
                "api_key": target.api_key,
                "model": target.model,
                "temperature": target.temperature,
                "timeout": target.timeout,
                "reasoning": reasoning_for_model(target),
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
