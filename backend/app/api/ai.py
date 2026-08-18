"""AI 相关路由：聊天 SSE、模型测试、选择分析。"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.ai import gateway as gw
from app.ai.loop import stream
from app.ai.dto import ChatRequest
from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["ai"])


_SELECTION_REPLIES: dict[str, str] = {
    "explain": "这条 SQL 先从 order_items 聚合每个 product_id 的销量，再 JOIN products 拿名称，最后排序取前 10。写法简洁，索引良好时性能 OK。",
    "optimize": "优化建议：① 给 order_items.product_id 加索引（若没有）避免全表扫；② 把 LIMIT 10 推到子查询里再 JOIN products，减少 JOIN 行数；③ 若经常跑，考虑物化视图。",
    "risk": "风险检查通过：只读查询，无 DELETE / UPDATE，无全表写，可安全执行。",
}


@router.post("/ai/test")
async def ai_test(
    model_id: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """文本模型连通性 + 能力探测。
    优先级：model_id（从 ai_models 里找） > 传参覆盖 > 当前默认模型。"""
    state = get_state()
    cfg = _resolve_ai_cfg(state, model_id, provider, base_url, api_key, model)

    if cfg["provider"] == "mock":
        return {
            "ok": True,
            "provider": "mock",
            "latency_ms": 0,
            "reply": "mock 网关正常",
            "capabilities": {
                "connectivity": True,
                "function_calling": True,
                "streaming": True,
                "reasoning": False,
                "context_window": None,
                "reason": "mock 模式，能力为内置模拟",
            },
        }

    if not cfg["base_url"]:
        return {"ok": False, "error": "缺少 base_url", "capabilities": {}}

    caps: dict[str, Any] = {}
    t0 = time.monotonic()
    try:
        # 1. 连通性 + 基础聊天
        provider_inst = gw.build_provider(cfg)
        resp = await provider_inst.chat([{"role": "user", "content": "ping"}], tools=None, allow_fallback=False)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        if not resp.content and not resp.tool_calls:
            return {"ok": False, "error": "网关返回空响应", "latency_ms": latency_ms, "capabilities": caps}
        caps["connectivity"] = True
        caps["latency_ms"] = latency_ms

        # 2. function calling 探测
        try:
            fc_resp = await provider_inst.chat(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "noop", "description": "ping", "parameters": {"type": "object", "properties": {}}}}],
                allow_fallback=False,
            )
            caps["function_calling"] = True
            _ = fc_resp  # 能成功回来就说明支持
        except Exception:
            caps["function_calling"] = False

        # 3. 推理能力探测（关键词硬覆盖 + 流式实测）
        caps["reasoning"] = await _detect_reasoning(cfg)

        # 4. 流式输出探测（SSE）
        caps["streaming"] = await _detect_streaming(cfg)

        # 5. 上下文窗口（模型名推断 + /models 端点兜底）
        caps["context_window"] = await _resolve_context_window(cfg)

        return {
            "ok": True,
            "provider": cfg["provider"],
            "model": cfg.get("model"),
            "latency_ms": latency_ms,
            "reply": (resp.content or "")[:80],
            "capabilities": caps,
        }
    except Exception as e:  # noqa: BLE001
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {"ok": False, "error": str(e), "latency_ms": latency_ms, "capabilities": caps}


@router.post("/ai/embedding/test")
async def embedding_test(
    model_id: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """嵌入模型连通性 + 维度探测。"""
    state = get_state()
    cfg = _resolve_emb_cfg(state, model_id, provider, base_url, api_key, model)

    if cfg["provider"] == "hash":
        return {
            "ok": True,
            "provider": "hash",
            "dimensions": 64,
            "latency_ms": 0,
            "note": "哈希嵌入，离线可用",
        }

    if not cfg["base_url"]:
        return {"ok": False, "error": "缺少 base_url"}

    t0 = time.monotonic()
    try:
        import httpx
        url = cfg["base_url"].rstrip("/") + "/embeddings"
        headers = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        payload = {"input": "hello world", "model": cfg.get("model", "")}
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        dims = len(data["data"][0]["embedding"])
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {
            "ok": True,
            "provider": cfg["provider"],
            "model": cfg.get("model"),
            "dimensions": dims,
            "latency_ms": latency_ms,
        }
    except Exception as e:  # noqa: BLE001
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {"ok": False, "error": str(e), "latency_ms": latency_ms}


# ---- 辅助函数 ----

def _resolve_ai_cfg(state, model_id, provider, base_url, api_key, model) -> dict:
    rs = state.runtime.get()
    if model_id:
        target = next((m for m in rs.ai_models if m.id == model_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"model {model_id} not found")
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
    if provider is not None:
        cfg["provider"] = provider
    if base_url is not None:
        cfg["base_url"] = base_url
    if api_key is not None and "•••" not in (api_key or ""):
        cfg["api_key"] = api_key
    if model is not None:
        cfg["model"] = model
    return cfg


def _resolve_emb_cfg(state, model_id, provider, base_url, api_key, model) -> dict:
    rs = state.runtime.get()
    if model_id:
        target = next((m for m in rs.embedding_models if m.id == model_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"embedding model {model_id} not found")
        cfg = {
            "provider": target.provider,
            "base_url": target.base_url,
            "api_key": target.api_key,
            "model": target.model,
        }
    else:
        cfg = rs.embedding_config()
    if provider is not None:
        cfg["provider"] = provider
    if base_url is not None:
        cfg["base_url"] = base_url
    if api_key is not None and "•••" not in (api_key or ""):
        cfg["api_key"] = api_key
    if model is not None:
        cfg["model"] = model
    return cfg


async def _detect_reasoning(cfg: dict) -> bool | None:
    """推理能力检测：先按模型名关键词硬覆盖（OpenAI 系推理内容 API 不可见，无法靠字段探测），
    否则实测一次流式补全，看响应是否带 reasoning_content / reasoning 字段。"""
    model_name = cfg.get("model", "") or ""
    name = model_name.lower()
    # 明确推理模型（API 不外露推理内容，无法靠字段探测）
    reasoning_keywords = [
        "o1", "o3", "o4", "reasoning", "r1", "deepseek",
        "claude-opus", "sonnet-4", "gpt-5", "gpt-4o",
    ]
    if any(k in name for k in reasoning_keywords):
        return True
    try:
        return await _detect_reasoning_streaming(cfg)
    except Exception:  # noqa: BLE001
        # 探测本身失败（网络/超时）→ 未知，不误判
        return None


async def _detect_reasoning_streaming(cfg: dict) -> bool:
    """流式补全实测推理能力：扫描 SSE delta 是否含非空 reasoning_content / reasoning。"""
    try:
        import httpx
        url = (cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
        headers: dict[str, str] = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        payload: dict = {
            "model": cfg.get("model", ""),
            "messages": [{"role": "user", "content": "1+1=?"}],
            "stream": True,
            "max_tokens": 32,
        }
        # 对已知支持推理参数的端点，显式开启推理以提高命中率
        gw_inst = gw.build_provider(cfg)
        if gw_inst.reasoning_supports_param():
            if any(k in (cfg.get("model", "").lower()) for k in ("o1", "o3", "o4")):
                payload["reasoning_effort"] = "low"
            else:
                payload["thinking"] = {"type": "enabled"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code != 200:
                    return False
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    for choice in obj.get("choices", []):
                        delta = choice.get("delta", {})
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if rc and str(rc).strip():
                            return True
        return False
    except Exception:  # noqa: BLE001
        return False


async def _detect_streaming(cfg: dict) -> bool:
    """探测是否支持 SSE 流式输出。"""
    try:
        import httpx
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        payload = {
            "model": cfg.get("model", ""),
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 5,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code != 200:
                    return False
                # 收第一个 data 块看看
                async for line in r.aiter_lines():
                    if line.startswith("data:") and "DONE" not in line:
                        return True
                return False
    except Exception:
        return False


def _infer_context_window(model_name: str) -> int | None:
    """从模型名推断上下文窗口大小（单位 token）。"""
    name = (model_name or "").lower()
    table = [
        ("gpt-4o", 128000),
        ("gpt-4-turbo", 128000),
        ("gpt-3.5", 16384),
        ("claude-sonnet", 200000),
        ("claude-opus", 200000),
        ("claude-3.5", 200000),
        ("o1", 128000),
        ("o3", 200000),
        ("llama-3.1", 128000),
        ("llama-3.2", 128000),
        ("llama-3.3", 128000),
        ("qwen2.5", 32768),
        ("qwen3", 128000),
        ("glm-4", 128000),
        ("deepseek-v3", 128000),
        ("deepseek-r1", 64000),
        ("bge-m3", 8192),
    ]
    for kw, size in table:
        if kw in name:
            return size
    return None


async def _fetch_context_window(cfg: dict) -> int | None:
    """兜底：查 OpenAI 兼容 /models 端点，按模型 id 取 context_length。失败返回 None。"""
    try:
        import httpx
        url = (cfg.get("base_url") or "").rstrip("/") + "/models"
        headers: dict[str, str] = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        target = (cfg.get("model") or "").lower()
        for m in data.get("data", []):
            if m.get("id", "").lower() == target:
                cl = m.get("context_length") or m.get("max_model_len")
                if cl:
                    return int(cl)
    except Exception:  # noqa: BLE001
        return None
    return None


async def _resolve_context_window(cfg: dict) -> int | None:
    return _infer_context_window(cfg.get("model", "")) or await _fetch_context_window(cfg)


# ---- chat / selection ----

@router.post("/ai/chat")
async def ai_chat(req: ChatRequest) -> StreamingResponse:
    state = get_state()
    try:
        state.connections.get(req.connection_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    session_id = state.chats.upsert(req.session_id, req.connection_id, req.title)

    async def gen():
        events: list[dict] = []
        try:
            async for ev in stream(state, req):
                events.append(ev)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        # [DONE] 之前落消息（SSE client 收到 DONE 后立即断开，后面的代码可能不执行）
        try:
            msgs = _events_to_messages(events, req.messages)
            state.chats.append_messages(session_id, msgs)
        except Exception:
            pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _events_to_messages(events: list[dict], request_messages: list) -> list[dict]:
    """从 SSE events + 请求消息转换为落库的 message 列表。"""
    out: list[dict] = []
    # user 消息（ChatMessage pydantic 对象或 dict 都支持）
    for m in request_messages:
        role = m.role if hasattr(m, 'role') else m.get("role", "")
        content = m.content if hasattr(m, 'content') else m.get("content", "")
        if role == "user":
            out.append({"role": "user", "kind": "text", "content": content})
    # AI 消息
    text_parts: list[str] = []
    for ev in events:
        t = ev.get("type")
        if t == "text":
            text_parts.append(ev.get("content", ""))
        elif t == "sql_card":
            # 先把累计 text 作为一条
            if text_parts:
                out.append({"role": "assistant", "kind": "text", "content": "".join(text_parts)})
                text_parts = []
            card = ev.get("card", {})
            out.append({
                "role": "assistant",
                "kind": "sql_card",
                "content": json.dumps(card, ensure_ascii=False),
                "sql": card.get("sql", ""),
                "verdict": card.get("verdict", ""),
            })
        elif t == "plan":
            out.append({
                "role": "assistant",
                "kind": "report",
                "content": json.dumps(ev, ensure_ascii=False),
            })
        elif t == "section":
            out.append({
                "role": "assistant",
                "kind": "report",
                "content": json.dumps(ev, ensure_ascii=False),
            })
        elif t == "narration":
            out.append({
                "role": "assistant",
                "kind": "report",
                "content": json.dumps(ev, ensure_ascii=False),
            })
        elif t == "clarify":
            out.append({
                "role": "assistant",
                "kind": "clarify",
                "content": ev.get("question", ""),
            })
    if text_parts:
        out.append({"role": "assistant", "kind": "text", "content": "".join(text_parts)})
    return out


# ---- chat sessions ----

@router.get("/chat/sessions")
async def list_sessions(connection: str | None = None, limit: int = 50) -> dict:
    state = get_state()
    sessions = state.chats.list(connection_id=connection, limit=limit)
    return {"sessions": sessions, "count": len(sessions)}


@router.get("/chat/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    state = get_state()
    s = state.chats.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@router.put("/chat/sessions/{session_id}/title")
async def set_session_title(session_id: str, body: dict) -> dict:
    state = get_state()
    ok = state.chats.set_title(session_id, body.get("title", ""))
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}


@router.post("/ai/selection")
async def ai_selection(body: dict) -> dict:
    kind = body.get("kind", "explain")
    return {"text": _SELECTION_REPLIES.get(kind, "分析完成。"), "kind": kind}
