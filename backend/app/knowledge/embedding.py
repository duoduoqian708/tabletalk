"""文本嵌入：异步 Embedder 协议 + OpenAI 兼容 API。

- ApiEmbedder：调用任意 OpenAI 兼容 /embeddings 端点（Ollama / vLLM / bge-m3 网关 / 云端），
  由运行时设置决定。数据流向由配置决定：本地端点不出内网。

接口为 async：网络调用不阻塞事件循环。替换实现只需满足 `Embedder` 协议。
"""
from __future__ import annotations

import logging
import math
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class ApiEmbedder:
    """OpenAI 兼容 /embeddings 端点。维度由端点返回决定（bge-m3=1024 等）。"""

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or "bge-m3"
        self.api_key = api_key
        self.last_usage: dict[str, int] | None = None  # 每次调用后的 usage（OpenAI 兼容）
        self.total_usage: dict[str, int] = {"prompt_tokens": 0, "total_tokens": 0}  # 累计

    async def embed(self, text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    headers=headers,
                    json={"model": self.model, "input": [text]},
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as e:
                status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                logger.warning(
                    "[kb.embedding] embed 请求失败 model=%s base=%s status=%s：%s",
                    self.model, self.base_url, status, e,
                )
                raise
        vec = data["data"][0]["embedding"]
        # 解析 usage（OpenAI 兼容 /embeddings 响应含 usage 字段）
        usage = data.get("usage")
        if usage and isinstance(usage, dict):
            self.last_usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "total_tokens": usage.get("total_tokens", usage.get("prompt_tokens", 0)),
            }
            self.total_usage["prompt_tokens"] += self.last_usage["prompt_tokens"]
            self.total_usage["total_tokens"] += self.last_usage["total_tokens"]
        else:
            self.last_usage = None
        logger.debug("[kb.embedding] model=%s tokens=%s", self.model, self.total_usage["total_tokens"])
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def make_embedder(provider: str, base_url: str = "", model: str = "", api_key: str = "") -> Embedder:
    """按运行时设置创建嵌入实现。base_url 为空时抛出 ValueError。"""
    if not base_url:
        raise ValueError("向量模型未配置（base_url 为空）。请在「系统设置 → 向量模型接入」配置 base_url、model、api_key 后重试。")
    return ApiEmbedder(base_url, model, api_key)

