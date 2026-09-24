"""统一提示词管理：外部模板文件 + locale 感知渲染。

用法：
    from app.ai.prompts import render
    text = render("main_chat")              # 自动从 contextvar 取 locale
    text = render("ai_review", locale="en", sql="SELECT ...")

模板文件路径：prompts/{locale}/{name}.txt
变量占位符：__VAR__（双下划线 + 大写变量名 + 双下划线）
"""
from __future__ import annotations

import contextvars
import logging
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent
_DEFAULT_LOCALE = "zh-CN"
_CACHE: dict[str, str] = {}
_MTIMES: dict[str, float] = {}

_LOCALE: contextvars.ContextVar[str | None] = contextvars.ContextVar("prompt_locale", default=None)


def set_locale(locale: str | None) -> contextvars.Token:
    """设置当前请求的语言（由中间件在请求入口调用）。"""
    return _LOCALE.set(locale)


def current_locale() -> str:
    """获取当前 locale（contextvar → zh-CN 兜底）。"""
    return _LOCALE.get() or _DEFAULT_LOCALE


def _resolve_dir(locale: str) -> Path:
    """locale → 模板目录（en-US → en/，zh-CN → zh/，兜底 zh/）。"""
    lang = locale.split("-")[0].lower() if locale else "zh"
    d = _PROMPTS_DIR / lang
    if d.is_dir():
        return d
    return _PROMPTS_DIR / "zh"


def _load(name: str, locale: str) -> str:
    """加载模板文件（带 mtime 缓存，文件变更自动重载）。"""
    d = _resolve_dir(locale)
    path = d / f"{name}.txt"
    if not path.exists():
        # 兜底：尝试 zh 目录
        fallback = _PROMPTS_DIR / "zh" / f"{name}.txt"
        if fallback.exists():
            path = fallback
        else:
            raise FileNotFoundError(f"提示词模板 '{name}' 不存在（locale={locale}，路径={d}）")
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    cached = _CACHE.get(key)
    if cached is not None and _MTIMES.get(key) == mtime:
        return cached
    content = path.read_text(encoding="utf-8")
    _CACHE[key] = content
    _MTIMES[key] = mtime
    return content


def render(name: str, locale: str | None = None, **vars: str) -> str:
    """渲染提示词模板。

    Args:
        name: 模板名（不含 .txt 后缀）
        locale: 语言代码（None 则从 contextvar 自动取）
        **vars: 模板变量（__VAR__ 占位符会被替换）

    Returns:
        渲染后的提示词文本
    """
    loc = locale or current_locale()
    text = _load(name, loc)
    for k, v in vars.items():
        text = text.replace(f"__{k.upper()}__", str(v))
    return text
