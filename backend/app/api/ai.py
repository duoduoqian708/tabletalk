"""AI 相关路由：聊天 SSE、模型测试、选择分析。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import httpx

from app.ai import gateway as gw
from app.ai.loop import stream
from app.ai.dto import ChatRequest
from app.ai.providers import builtin_embedding_providers, builtin_providers, get_adapter
from app.state import get_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["ai"])


_SELECTION_REPLIES: dict[str, str] = {
    "explain": "这条 SQL 先从 order_items 聚合每个 product_id 的销量，再 JOIN products 拿名称，最后排序取前 10。写法简洁，索引良好时性能 OK。",
    "optimize": "优化建议：① 给 order_items.product_id 加索引（若没有）避免全表扫；② 把 LIMIT 10 推到子查询里再 JOIN products，减少 JOIN 行数；③ 若经常跑，考虑物化视图。",
    "risk": "风险检查通过：只读查询，无 DELETE / UPDATE，无全表写，可安全执行。",
}


@router.get("/ai/providers")
async def ai_providers() -> dict:
    """内置供应商下拉清单（含 base_url 预设与推荐模型候选）。"""
    return {"providers": builtin_providers()}


@router.get("/ai/embedding/providers")
async def ai_embedding_providers() -> dict:
    """内置向量供应商下拉清单（三家 + 火山双路 + 自定义，含默认维度预填）。"""
    return {"providers": builtin_embedding_providers()}


@router.get("/ai/upstream/models")
async def ai_upstream_models(
    base_url: str = "",
    api_key: str | None = None,
    model_id: str | None = None,
) -> dict:
    """代理拉取上游 OpenAI 兼容 GET {base_url}/models。

    key 服务端持有：优先 api_key 参数；为空且给 model_id 时回退用已存模型配置的 key
    （掩码 key 永不出后端）。上游不支持 /models → ok=false（前端回退自由输入，零阻断）。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="base_url 必须是 http(s) 地址")
    key = api_key or ""
    if not key and model_id:
        for m in get_state().runtime.get().ai_models:
            if m.id == model_id:
                key = m.api_key
                break
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as ac:
            r = await ac.get(f"{base}/models", headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "supported": False, "error": f"拉取失败：{e.__class__.__name__}"}
    if r.status_code != 200:
        return {"ok": False, "supported": False, "error": f"HTTP {r.status_code}"}
    try:
        data = r.json().get("data") or []
    except ValueError:
        return {"ok": False, "supported": False, "error": "响应非 JSON，端点不支持 /models"}
    models = sorted({str(d.get("id")) for d in data if isinstance(d, dict) and d.get("id")})
    return {"ok": True, "models": models}


@router.post("/ai/test")
async def ai_test(
    model_id: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
):
    """文本模型连通性 + 能力探测（SSE 流式：每完成一步推一个事件）。"""
    state = get_state()
    cfg = _resolve_ai_cfg(state, model_id, provider, base_url, api_key, model)

    if cfg["provider"] == "mock":
        return {
            "ok": True, "provider": "mock", "latency_ms": 0, "reply": "mock 网关正常",
            "capabilities": {
                "connectivity": True, "function_calling": True, "streaming": True,
                "reasoning": False, "context_window": None, "reason": "mock 模式，能力为内置模拟",
            },
        }

    if not cfg["base_url"]:
        return {"ok": False, "error": "缺少 base_url，请在设置中配置向量模型接入点", "capabilities": {}}

    async def _gen():
        caps: dict[str, Any] = {}
        t0 = time.monotonic()
        try:
            # 1. 连通性（串行）
            provider_inst = gw.build_provider(cfg)
            resp = await provider_inst.chat([{"role": "user", "content": "ping"}], tools=None, allow_fallback=False)
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            if not resp.content and not resp.tool_calls:
                yield f"data: {json.dumps({'step': 'ping', 'ok': False, 'error': '网关返回空响应'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'step': 'done', 'ok': False, 'error': '网关返回空响应', 'latency_ms': latency_ms}, ensure_ascii=False)}\n\n"
                return
            caps["connectivity"] = True
            caps["latency_ms"] = latency_ms
            yield f"data: {json.dumps({'step': 'ping', 'ok': True, 'latency_ms': latency_ms}, ensure_ascii=False)}\n\n"

            # 2-5. 并行探测
            from app.ai.providers import resolve_adapter_name
            adapter_name = resolve_adapter_name(cfg.get("provider"))
            caps["adapter"] = adapter_name

            async def _probe_fc() -> bool:
                try:
                    await provider_inst.chat(
                        [{"role": "user", "content": "hi"}],
                        tools=[{"type": "function", "function": {"name": "noop", "description": "ping", "parameters": {"type": "object", "properties": {}}}}],
                        allow_fallback=False,
                    )
                    return True
                except Exception:
                    return False

            async def _probe_reasoning() -> tuple[bool, str | None]:
                if adapter_name != "custom":
                    return True, "low"
                r = await _detect_reasoning(cfg)
                e = await _detect_reasoning_effort(cfg) if r else None
                return r, e

            async def _probe_streaming() -> bool:
                return await _detect_streaming(cfg)

            async def _probe_context() -> int | None:
                return await _resolve_context_window(cfg)

            # 用 Queue 逐个推送（并行，谁先完成谁先推）
            import asyncio as _aio
            _queue: _aio.Queue[tuple[str, Any]] = _aio.Queue()

            async def _put(name: str, fn):
                try:
                    await _queue.put((name, await fn()))
                except Exception:
                    await _queue.put((name, None))

            probe_tasks = [
                _aio.create_task(_put("fc", _probe_fc)),
                _aio.create_task(_put("reasoning", _probe_reasoning)),
                _aio.create_task(_put("streaming", _probe_streaming)),
                _aio.create_task(_put("context_window", _probe_context)),
            ]
            for _ in range(4):
                name, result = await _queue.get()
                if name == "fc":
                    caps["function_calling"] = result
                    yield f"data: {json.dumps({'step': 'fc', 'ok': result}, ensure_ascii=False)}\n\n"
                elif name == "reasoning":
                    caps["reasoning"] = result[0]
                    caps["reasoning_effort"] = result[1]
                    if adapter_name != "custom":
                        caps["effort_effective"] = None
                elif name == "streaming":
                    caps["streaming"] = result
                    yield f"data: {json.dumps({'step': 'streaming', 'ok': result}, ensure_ascii=False)}\n\n"
                elif name == "context_window":
                    caps["context_window"] = result
                    yield f"data: {json.dumps({'step': 'context_window', 'ok': result is not None, 'value': result}, ensure_ascii=False)}\n\n"
            await _aio.gather(*probe_tasks)

            _persist_capabilities(state, model_id, caps)
            yield f"data: {json.dumps({'step': 'done', 'ok': True, 'provider': cfg['provider'], 'model': cfg.get('model'), 'latency_ms': latency_ms, 'reply': (resp.content or '')[:80], 'capabilities': caps}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            yield f"data: {json.dumps({'step': 'done', 'ok': False, 'error': str(e), 'latency_ms': latency_ms, 'capabilities': caps}, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


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

    if not cfg["base_url"]:
        return {"ok": False, "error": "缺少 base_url，请在设置中配置向量模型接入点"}

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
    if api_key is not None and api_key and "•••" not in api_key:
        cfg["api_key"] = api_key
    if model is not None:
        cfg["model"] = model
    return cfg


def _resolve_emb_cfg(state, model_id, provider, base_url, api_key, model) -> dict:
    rs = state.runtime.get()
    if model_id:
        target = next((m for m in rs.embedding_models if m.id == model_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"model {model_id} not found")
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
    if api_key is not None and api_key and "•••" not in api_key:
        cfg["api_key"] = api_key
    if model is not None:
        cfg["model"] = model
    return cfg


async def _detect_reasoning_effort(cfg: dict) -> str | None:
    """探测该模型能接受的最大 reasoning_effort 档位（high→medium→low 逐个试）。
    全部被拒 → 返回 None（推理经 thinking 仅启用，无档位）。"""
    headers: dict[str, str] = {}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    url = (cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
    for effort in ("high", "medium", "low"):
        payload = {
            "model": cfg.get("model", ""),
            "messages": [{"role": "user", "content": "1+1=?"}],
            "max_tokens": 16,
            "reasoning_effort": effort,
        }
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                continue
            return effort
        except Exception:  # noqa: BLE001 - 单个档位失败则试下一档
            continue
    return None


def _persist_capabilities(state, model_id: str | None, caps: dict[str, Any]) -> None:
    """把探测结果 {reasoning, reasoning_effort} 写回对应模型配置（settings 库），
    供 KB 阶段2/3 与后续功能读取「支持即开最大深度」。"""
    try:
        rs = state.runtime.get()
        target_id = model_id or rs.default_ai_model
        if not target_id:
            return
        models = []
        for m in rs.ai_models:
            d = asdict(m)
            if m.id == target_id:
                # 全量 caps 落库（含 adapter 归属 / effort 生效性校准），前端与校准直接可用
                d["capabilities"] = {
                    "reasoning": bool(caps.get("reasoning")),
                    "reasoning_effort": caps.get("reasoning_effort"),
                    "adapter": caps.get("adapter"),
                    "effort_effective": caps.get("effort_effective"),
                }
            models.append(d)
        if not any(m.id == target_id for m in rs.ai_models):
            return
        state.runtime.update({"ai_models": models})
    except Exception:  # noqa: BLE001 - 写回失败不影响探测响应
        pass


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
        # （探测请求的思考参数统一走适配器：各家字段/枚举差异不再关键词硬编码）
        adapter = get_adapter(cfg.get("provider"), cfg.get("model", ""))
        adapter.apply_thinking(payload, "low", cfg.get("model", ""), False)
        async with httpx.AsyncClient(timeout=20.0) as client:
            found = False
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
                            found = True
                            break
                    if found:
                        break
                # 显式收流：MockTransport 场景提前结束迭代会留下未 awaited 的 aiter_text 协程
                await r.aclose()
        return found
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
        ("deepseek-v4", 128000),
        ("deepseek-r1", 64000),
        ("bge-m3", 8192),
        ("mimo", 128000),
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

    session_id = req.session_id
    # T3.5 切库换 session：后端双保险——会话归属数据源必须与请求一致，否则明确拒绝（不发混合上下文）
    if session_id:
        stored_conn = state.chats.get_connection(session_id)
        if stored_conn is not None and stored_conn != req.connection_id:
            raise HTTPException(
                status_code=409,
                detail="该会话属于其它数据源，不能跨数据源连续提问；请新建会话",
            )
    session_id = state.chats.upsert(req.session_id, req.connection_id, req.title)
    req.session_id = session_id  # 回写解析后的会话 id，供 loop / 工具（load_result 按 session 隔离工件）使用

    # 卡死边界：知识库未构建/未确认的数据源 AI 不可用（SSE 内报错，保持流式协议）
    cfg = state.connections.get(req.connection_id)
    if cfg.kb_status != "ready":

        async def gen_err():
            yield f"data: {json.dumps({'type': 'error', 'code': 'kb_not_built', 'message': '该数据源知识库未构建，请先构建并确认启用'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen_err(), media_type="text/event-stream")

    async def gen():
        _completed = False  # 正常走完（含内部 error 事件）置 True；断连/异常保持 False → carry-over
        _user_committed = False  # 当前 user 问句随首次 _commit 落库（必须晚于 stream() 内的历史加载）
        try:
            _last_q = next(
                (m.content if hasattr(m, "content") else m.get("content", "")
                 for m in reversed(req.messages)
                 if (m.role if hasattr(m, "role") else m.get("role", "")) == "user"),
                None,
            )

            def _commit_msgs(msgs: list[dict]) -> list[dict]:
                nonlocal _user_committed
                if _last_q and not _user_committed:
                    msgs = [{"role": "user", "kind": "text", "content": _last_q}] + list(msgs)
                    _user_committed = True
                return msgs

            async for ev in stream(state, req):
                if ev.get("type") == "_commit":
                    # committed-turn：循环在每轮边界提交已产出事件（断连只丢当前轮）
                    try:
                        msgs = _commit_msgs(ev.get("messages") or [])
                        if msgs:
                            state.chats.append_messages(session_id, msgs)
                    except Exception:
                        pass
                    continue  # 内部事件不下发前端
                if ev.get("type") == "sql_card":
                    # sql_card 工件即时落库（本地；行数已由 query._auto_cap 封顶，truncated 随存）
                    try:
                        card = ev.get("card") or {}
                        rid = card.get("result_id")
                        res = card.get("result")
                        if rid and isinstance(res, dict):
                            _persist_card_artifact(state, session_id, rid, res)
                    except Exception:
                        pass
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            _completed = True
        except Exception as e:  # noqa: BLE001
            # 已向用户明示错误；本轮未提交部分视为中断 → carry-over 供续答
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            if not _completed:
                # 断连/超时（GeneratorExit）→ 取消在飞查询 + 记录中断摘要（下次请求注入续答）
                try:
                    from app.core.query import cancel as _qcancel

                    _qcancel(req.connection_id)
                except Exception:
                    pass
                # 本轮完全未提交 → 至少把用户问句落库（否则续答时问题丢失）
                if not _user_committed:
                    try:
                        _last_q2 = next(
                            (m.content if hasattr(m, "content") else m.get("content", "")
                             for m in reversed(req.messages)
                             if (m.role if hasattr(m, "role") else m.get("role", "")) == "user"),
                            None,
                        )
                        if _last_q2:
                            state.chats.append_messages(session_id, [{"role": "user", "kind": "text", "content": _last_q2}])
                    except Exception:
                        pass
                try:
                    from app.ai.loop import _save_carryover

                    _save_carryover(state, session_id, req.connection_id)
                except Exception:
                    pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _persist_card_artifact(state, session_id: str, result_id: str, res: dict) -> None:
    """单张 sql_card 工件即时落库（committed-turn：卡产生即存，断连不丢结果集）。"""
    try:
        max_rows = state.runtime.get().query_max_rows or 1000
    except Exception:
        max_rows = 1000
    data = {k: v for k, v in res.items() if k != "rows"}
    rows = res.get("rows")
    if rows is not None:
        capped = (rows or [])[: max_rows or len(rows or [])]
        data["rows"] = capped
        if len(rows or []) > len(capped):
            data["truncated"] = True
    try:
        state.chats.save_artifact(session_id, result_id, data)
    except Exception:
        pass


def _persist_artifacts(state, session_id: str, events: list[dict]) -> None:
    """WS3 T3.2：把 SSE 里的 sql_card 工件按 result_id 落库。

    行数上限与 run_query 一致（query._auto_cap 已封顶）；此处再防御性截断一次，
    保证超大异常数据也不会膨胀会话存储（acceptance：行数超过上限时截断存储）。
    本地落盘（chat.db）非出网，不受铁律1 的出网管道约束；load_result 回喂时才过脱敏。
    （committed-turn 后 gen() 已改为单卡即时落库 _persist_card_artifact；本函数保留供测试/工具复用。）
    """
    for ev in events:
        if ev.get("type") != "sql_card":
            continue
        card = ev.get("card") or {}
        rid = card.get("result_id")
        res = card.get("result")
        if not rid or not isinstance(res, dict):
            continue
        _persist_card_artifact(state, session_id, rid, res)


@router.post("/ai/dml/cancel")
async def dml_cancel(body: dict) -> dict:
    """WS4 T4.4：用户取消待确认写操作——清 pending_dml，并向会话写回系统消息（下轮模型可见）。"""
    state = get_state()
    sid = body.get("session_id") or ""
    if not sid:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    from app.safety.confirm import clear_pending

    had = clear_pending(state.chats, sid)
    if had:
        state.chats.append_messages(sid, [{
            "role": "assistant", "kind": "system",
            "content": "用户取消了该写操作，未执行任何变更。",
        }])
    return {"ok": True, "cancelled": had}


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


@router.post("/ai/continuation")
async def ai_continuation(body: dict) -> Any:
    """§19.5 continuation gate：统一"用户对 AI 回合的反应"入口。

    四 type 分发到既有处理器（不重写引擎执行）：
    - new_question：完整意图流程（harness 单循环），复用 /ai/chat SSE 链
    - clarify_report：续当前报告流（clarify 上下文 + 回答），复用 /ai/chat SSE 链
    - sql_option：轻量改写 SQL 重跑（不进意图分解/检索），复用 /ai/sql-option
    - confirm_write：DML 确认执行（preview 已给，confirm_token 闭环），复用 /api/v1/query
    铁律：new_question 必须是完整流程（追问可引出任意意图）；协议守 new/continuation 边界。
    """
    from fastapi.responses import StreamingResponse
    from app.ai.dto import ChatRequest

    t = str(body.get("type") or "").strip()
    conn_id = str(body.get("connection_id") or "")
    payload = body.get("payload") or {}
    if not conn_id:
        raise HTTPException(status_code=422, detail="需要 connection_id")

    if t in ("new_question", "clarify_report"):
        # SSE：构造 ChatRequest → 复用 ai_chat 全链路（会话归属/知识库就绪边界都在内部）
        raw = payload.get("question")
        messages = payload.get("messages")
        if not messages:
            q = str(raw or payload.get("content") or "").strip()
            if not q:
                raise HTTPException(status_code=422, detail="需要 question")
            messages = [{"role": "user", "content": q}]
        req = ChatRequest(
            connection_id=conn_id,
            session_id=body.get("session_id"),
            messages=messages,
            provider=payload.get("provider"),
            include_data=bool(payload.get("include_data", False)),
            mode=payload.get("mode"),
        )
        return await ai_chat(req)

    if t == "sql_option":
        return await ai_sql_option({
            "connection_id": conn_id,
            "sql": str(payload.get("sql") or ""),
            "option": payload.get("option") or {},
        })

    if t == "confirm_write":
        from app.api.query import QueryRequest, run_query as _query_handler
        qr = QueryRequest(
            connection_id=conn_id,
            sql=str(payload.get("sql") or ""),
            confirm=True,
            confirm_token=payload.get("confirm_token"),
            session_id=body.get("session_id"),
        )
        return await _query_handler(qr)

    raise HTTPException(status_code=422, detail=f"未知 continuation type：{t}")


@router.post("/ai/selection")
async def ai_selection(body: dict) -> dict:
    kind = body.get("kind", "explain")
    return {"text": _SELECTION_REPLIES.get(kind, "分析完成。"), "kind": kind}


@router.post("/ai/sql-option")
async def ai_sql_option(body: dict) -> dict:
    """S3-2：SQL 可选追加项轻量调用——用户点选项 → LLM 基于当前 SQL 直接改写。

    不走检索/分解管线（一次轻量调用）：输入 = 当前完整 SQL + 选项 label/hint
    + 该表已确认的过滤规则（纯取数）+ 方言 → 返回改写后的完整 SQL。
    服务端只拼 prompt 与解析，SQL 文本完全由 LLM 产出（引擎不改写）。
    """
    from app.ai import gateway as gw
    from app.ai.provider_cfg import resolve_provider_cfg as _resolve_cfg
    from app.config import get_env
    from app.state import get_state
    from app.safety.gate import sqlglot_dialect_for

    state = get_state()
    conn_id = str(body.get("connection_id") or "")
    sql = str(body.get("sql") or "").strip()
    option = body.get("option") or {}
    label = str(option.get("label") or "").strip()
    hint = str(option.get("hint") or "").strip()
    if not conn_id or not sql or not label:
        raise HTTPException(status_code=422, detail="需要 connection_id / sql / option.label")
    try:
        cfg = state.connections.get(conn_id)
    except Exception:
        raise HTTPException(status_code=404, detail="connection not found")
    dialect = sqlglot_dialect_for(cfg.dialect)

    # 该表已确认的过滤规则（知识库纯取数，无检索管线）
    rules_txt = ""
    try:
        state.knowledge.ensure_loaded(conn_id)
        tables: set[str] = set()
        try:
            tables = set(state.knowledge.semantic_store._tables.get(conn_id, {}).keys())
        except Exception:
            tables = set()
        if not tables:
            import sqlglot
            try:
                expr = sqlglot.parse_one(sql)
                tables = {t.name for t in expr.find_all(sqlglot.exp.Table) if t.name}
            except Exception:
                tables = set()
        fs = state.knowledge.filter_store.get_filters(conn_id, tables)
        if fs:
            rules_txt = "\n".join(f"- {t}: {' AND '.join(preds)}" for t, preds in fs.items())
    except Exception:
        rules_txt = ""

    from app.ai.prompts import render
    prompt = render("sql_rewrite", label=label, hint=hint or "", filter_rules=rules_txt or "", dialect=dialect, sql=sql)

    req = type("_SqlOptReq", (), {"model_id": body.get("model_id")})()
    pc = _resolve_cfg(state, req)
    if gw.is_effective_mock(pc):
        # mock 演示：不改写（真实模型才能理解改写意图）
        return {"sql": sql, "note": "当前为 mock 模式，无法改写 SQL；请在系统设置配置真实模型。"}
    try:
        provider = gw.build_provider(pc)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None, ctx={
            "conn_id": conn_id, "connection": cfg.name, "skill": "sql-option",
            "source": "egress", "status": "egress", "include_data": False,
        })
        new_sql = (resp.content or "").strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM 改写失败：{e}")
    if not new_sql:
        new_sql = sql
    return {"sql": new_sql}
