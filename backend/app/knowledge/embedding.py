"""文本嵌入：异步 Embedder 协议 + 哈希(离线默认) + OpenAI 兼容 API（真语义）。

- HashingEmbedder：字符 unigram/bigram 哈希，离线、确定性、零依赖。只桥接"表面重叠"，
  中文"退货率" ↔ 英文 return_rate 这类**语义**桥接需要真嵌入。
- ApiEmbedder：调用任意 OpenAI 兼容 /embeddings 端点（Ollama / vLLM / bge-m3 网关 / 云端），
  由运行时设置决定。数据流向由配置决定：本地端点不出内网。

接口为 async：网络调用不阻塞事件循环。替换实现只需满足 `Embedder` 协议。
"""
from __future__ import annotations

import math
import zlib
from typing import Protocol

import httpx

DIM = 256


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


def _grams(text: str) -> list[str]:
    """bigram 为主（权重 2），unigram 兜底（权重 1，CJK 单字重叠）。"""
    chars = [c for c in text.lower() if not c.isspace()]
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)] + chars


class HashingEmbedder:
    """字符 n-gram 哈希到 DIM 维，L2 归一化。离线、零依赖、确定性。"""

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        for g in _grams(text):
            idx = zlib.crc32(g.encode("utf-8")) % DIM
            vec[idx] += 2.0 if len(g) == 2 else 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class ApiEmbedder:
    """OpenAI 兼容 /embeddings 端点。维度由端点返回决定（bge-m3=1024 等）。"""

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or "bge-m3"
        self.api_key = api_key

    async def embed(self, text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.model, "input": [text]},
            )
            resp.raise_for_status()
            data = resp.json()
        vec = data["data"][0]["embedding"]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def make_embedder(provider: str, base_url: str = "", model: str = "", api_key: str = "") -> Embedder:
    """按运行时设置选择嵌入实现：hash（默认，离线）或 api（真语义，可本地/云端）。"""
    if provider == "api" and base_url:
        return ApiEmbedder(base_url, model, api_key)
    return HashingEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    """两个 L2 归一化向量的余弦相似度（即点积）。"""
    return sum(x * y for x, y in zip(a, b))
