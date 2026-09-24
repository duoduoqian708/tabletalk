"""供应商注册表：provider 字符串（含历史自由命名）→ 规范适配器。

别名归一：现网 ModelConfig.provider 是用户可见的自由命名（如 '火山方舟'），
归一表把常见命名收敛到规范 adapter；未命中一律 custom（= 旧全局行为，不比现状差）。
"""
from __future__ import annotations

from app.ai.providers.base import ProviderAdapter
from app.ai.providers.openai_compat import (
    CustomAdapter,
    DeepSeekAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    QwenAdapter,
    ZhipuAdapter,
)
from app.ai.providers.volcano import ArkAdapter

ADAPTERS: dict[str, type[ProviderAdapter]] = {
    a.name: a for a in (
        OpenAIAdapter, DeepSeekAdapter, QwenAdapter, ZhipuAdapter,
        OllamaAdapter, ArkAdapter, CustomAdapter,
    )
}

# 别名归一表（小写匹配；中文原名原样匹配）
_ALIASES: dict[str, str] = {
    "openai": "openai", "gpt": "openai", "azure": "openai",
    "deepseek": "deepseek", "deep_seek": "deepseek",
    "volcano": "volcano", "volcengine": "volcano", "ark": "volcano",
    "doubao": "volcano", "火山方舟": "volcano", "豆包": "volcano", "方舟": "volcano",
    "zhipu": "zhipu", "glm": "zhipu", "bigmodel": "zhipu", "chatglm": "zhipu", "智谱": "zhipu",
    "qwen": "qwen", "dashscope": "qwen", "通义千问": "qwen", "通义": "qwen", "千问": "qwen",
    "ollama": "ollama",
    "opencode go": "openai", "opencode": "openai",
    "mock": "custom",   # mock 在 gateway 提前分流，注册表兜底 custom
    "custom": "custom",
}


def resolve_adapter_name(provider: str | None) -> str:
    """provider 字符串 → 规范 adapter 名。未命中 → custom。"""
    if not provider:
        return "custom"
    key = str(provider).strip()
    if key in _ALIASES:
        return _ALIASES[key]
    low = key.lower()
    return _ALIASES.get(low, "custom")


def get_adapter(provider: str | None, model: str = "") -> ProviderAdapter:
    """取适配器实例。model 传入预留（未来按模型族细分 caps 的钩子位）。"""
    return ADAPTERS[resolve_adapter_name(provider)]()


def builtin_providers() -> list[dict[str, str]]:
    """内置供应商清单（前端下拉数据源）。

    条目与适配器解耦：火山按量 / Agent Plan 两条路共用 volcano 适配器，
    仅 base_url 预设不同；display 为下拉显示名（用户指定文案）。
    models 为推荐模型候选（前端"获取列表"失败时的预设兜底）。
    """
    return [
        {"name": "openai", "display": "OpenAI", "adapter": "openai",
         "base_url": "https://api.openai.com/v1", "models": "gpt-4o-mini|o4-mini"},
        {"name": "deepseek", "display": "DeepSeek", "adapter": "deepseek",
         "base_url": "https://api.deepseek.com", "models": "deepseek-v4-flash|deepseek-v4-pro"},
        {"name": "volcano", "display": "火山（按量）", "adapter": "volcano",
         "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": "deepseek-v4-flash|deepseek-v4-pro"},
        {"name": "volcano", "display": "火山（Agent Plan）", "adapter": "volcano",
         "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3", "models": "deepseek-v4-flash"},
        {"name": "zhipu", "display": "智谱GLM", "adapter": "zhipu",
         "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": "glm-5.3-flash|glm-5.2"},
        {"name": "qwen", "display": "通义千问", "adapter": "qwen",
         "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": "qwen3.8-flash|qwen-plus"},
        {"name": "ollama", "display": "Ollama", "adapter": "ollama",
         "base_url": "http://127.0.0.1:11434/v1", "models": ""},
        {"name": "custom", "display": "自定义（OpenAI 兼容）", "adapter": "custom",
         "base_url": "", "models": ""},
    ]


def builtin_embedding_providers() -> list[dict[str, str]]:
    """内置向量供应商清单（前端向量面板下拉数据源）。

    与 chat 清单解耦：向量只收三家（智谱/通义/火山双路）+ 本地 Ollama + 自定义。
    dimensions 为该模型默认向量维度（选中时预填）。
    """
    return [
        {"name": "zhipu", "display": "智谱GLM", "adapter": "zhipu",
         "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": "embedding-3", "dimensions": "2048"},
        {"name": "qwen", "display": "通义千问", "adapter": "qwen",
         "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": "text-embedding-v4", "dimensions": "1024"},
        {"name": "volcano", "display": "火山（按量）", "adapter": "volcano",
         "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": "doubao-embedding-vision", "dimensions": "2048"},
        {"name": "volcano", "display": "火山（Agent Plan）", "adapter": "volcano",
         "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3", "models": "doubao-embedding-vision", "dimensions": "2048"},
        {"name": "ollama", "display": "Ollama", "adapter": "ollama",
         "base_url": "http://127.0.0.1:11434/v1", "models": "bge-m3", "dimensions": "1024"},
        {"name": "custom", "display": "自定义（OpenAI 兼容）", "adapter": "custom",
         "base_url": "", "models": "", "dimensions": ""},
    ]
