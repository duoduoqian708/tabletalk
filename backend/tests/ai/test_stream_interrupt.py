"""段9 审查 P1 回归：chat_stream 中断/异常记账 ok 状态如实（不再恒真）。

- 正常完成 → ok=True（response_json 为 usage）；
- 流中抛错（HTTP/解析）→ ok=False + note=异常；
- 客户端提前 aclose（GeneratorExit）→ ok=False + note="interrupted"。
"""
from __future__ import annotations

import asyncio

import httpx

import app.ai.gateway as gw
from app.ai.llm_log import LlmCallLog


def _gw():
    return gw.LLMGateway({"provider": "cloud", "base_url": "http://fake/v1", "model": "x"})


class _BoomStream:
    """进上下文即抛 HTTP 错误：模拟服务端 500 / 网络错误。"""

    async def __aenter__(self):
        raise httpx.HTTPStatusError("500 boom", request=None, response=None)

    async def __aexit__(self, *a):
        return False


class _OneChunkStream:
    """产出 1 个 chunk 后挂起：模拟客户端读到一半 aclose。"""

    def __init__(self) -> None:
        self.lines = iter(['data: {"choices":[{"delta":{"content":"hi"}}]}'])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for l in self.lines:
            yield l
        await asyncio.sleep(3600)


def _patch_stream(monkeypatch, factory):
    monkeypatch.setattr(gw.httpx.AsyncClient, "stream", lambda self, method, url, **kw: factory())


async def _drain(agen):
    out = []
    try:
        async for ch in agen:
            out.append(ch)
    except Exception:
        pass
    return out


async def test_stream_error_records_ok_false(app_state, monkeypatch):
    """流式 HTTP 错误 → finally 记 ok=False（response_json 含 error）。"""
    from app.ai.gateway import StreamChunk

    _patch_stream(monkeypatch, _BoomStream)
    ctx = {"conn_id": "c", "connection": "c", "skill": "test"}
    gen = _gw().chat_stream([{"role": "user", "content": "q"}], allow_fallback=False, ctx=ctx)
    out = await _drain(gen)
    assert out == []  # 异常被吞（_drain 捕获）
    calls = LlmCallLog(app_state.env.data_dir).list_calls(conn_id="c", limit=5)
    assert calls, "异常也必须记账"
    assert "error" in (calls[0].get("response_json") or ""), f"应记失败，got {calls[0].get('response_json')}"


async def test_stream_early_aclose_records_interrupted(app_state, monkeypatch):
    """客户端读到一半 aclose → finally 记 ok=False + note=interrupted。"""
    _patch_stream(monkeypatch, _OneChunkStream)
    ctx = {"conn_id": "c2", "connection": "c2", "skill": "test"}
    gen = _gw().chat_stream([{"role": "user", "content": "q"}], allow_fallback=False, ctx=ctx)
    first = await gen.__anext__()  # 拿到第一个 chunk
    assert first.delta == "hi"
    await gen.aclose()  # GeneratorExit → finally 记录
    calls = LlmCallLog(app_state.env.data_dir).list_calls(conn_id="c2", limit=5)
    assert calls, "中断也必须记账"
    assert "interrupted" in (calls[0].get("response_json") or ""), \
        f"中断应记 interrupted，got {calls[0].get('response_json')}"
