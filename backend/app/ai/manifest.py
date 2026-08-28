"""出网清单 Egress Manifest — B1 本地可枚举（离线可审计，单管道校验）。

每次模型调用前生成，描述本次请求实际发送给模型的内容：
- tables: 候选表集合（结构）
- kb_docs: 知识库条目数
- history_turns: 历史轮数
- include_data: 是否含聚合行数据
- redactions: 脱敏项（B1 为空，B2 填充）
- mode: privacy_mode
- ts, model, provider

单管道约束：context 构建 → manifest 生成 → gateway 发送，三者按同一清单校验。
"""
from __future__ import annotations

import time
from app.core.timeutil import utcnow_iso
from typing import Any


def build_manifest(
    state: Any,
    conn_id: str,
    context_meta: dict[str, Any],
    messages: list[dict[str, Any]],
    include_data: bool,
    provider_cfg: dict[str, Any],
    context_text: str = "",
) -> dict[str, Any]:
    tables = list(context_meta.get("candidate_tables") or [])
    # kb_docs：优先取 meta 里的 kb_docs，否则从 context_text 估算
    kb_docs = context_meta.get("kb_docs")
    if kb_docs is None:
        # 回退：统计 context 中知识库块的行数（【知识库】后每行一条）
        if "【知识库】" in context_text:
            kb_docs = context_text.count("\n- [")
        else:
            kb_docs = 0
    # 历史轮数：user + assistant 消息数（不含 system）
    history_turns = sum(1 for m in messages if m.get("role") in ("user", "assistant", "tool"))
    # 兼容：若 messages 含 system，则 history_turns 为 user/assistant 对数
    # 前端展示按轮（每轮 user+assistant 算1），但清单存原始计数，展示层再折算
    mode = "standard"
    try:
        mode = state.runtime.get().privacy_mode
    except Exception:
        pass
    if mode not in ("strict", "standard", "open"):
        mode = "standard"
    ts = utcnow_iso()
    model = provider_cfg.get("model") or ""
    provider = provider_cfg.get("provider") or "mock"
    # redactions：B1 为空（B2 填充确定性 token 列表）
    redactions: list[str] = []
    return {
        "tables": sorted(tables),
        "kb_docs": int(kb_docs or 0),
        "history_turns": int(history_turns),
        "include_data": bool(include_data),
        "redactions": redactions,
        "mode": mode,
        "ts": ts,
        "model": model,
        "provider": provider,
    }


def manifest_to_human(m: dict[str, Any], locale: str = "zh-CN") -> str:
    """可读文案：3 张表结构 · 2 条知识库注释 · 6 轮历史 · 无行数据"""
    is_zh = locale.startswith("zh")
    parts: list[str] = []
    n_tables = len(m.get("tables") or [])
    if is_zh:
        parts.append(f"{n_tables} 张表结构")
        parts.append(f"{m.get('kb_docs', 0)} 条知识库注释")
        parts.append(f"{m.get('history_turns', 0)} 轮历史")
        parts.append("含聚合行数据" if m.get("include_data") else "无行数据")
        if m.get("mode"):
            mode_map = {"strict": "严格", "standard": "标准", "open": "开放"}
            parts.append(f"{mode_map.get(m['mode'], m['mode'])}模式")
    else:
        parts.append(f"{n_tables} tables")
        parts.append(f"{m.get('kb_docs', 0)} KB docs")
        parts.append(f"{m.get('history_turns', 0)} turns")
        parts.append("with rows" if m.get("include_data") else "no rows")
        if m.get("mode"):
            parts.append(f"{m['mode']} mode")
    return " · ".join(parts)
