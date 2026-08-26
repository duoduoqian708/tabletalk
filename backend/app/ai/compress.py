"""上下文压缩 v1（WS3 T3.4）：只做压缩，不做截断。机械优先，LLM 兜底走单管道。

D7 原则：压缩的是叙事、保住的是结构 —— 卡片骨架（result_id/表名/verdict/rowcount）不可压缩；
当前轮（尾部最近一次 user 及其响应）与未决状态（pending_dml，WS4）永不动。
机械路径零模型调用；LLM 摘要本身是一次出网（脱敏进 + manifest + egress 审计 source=egress-compress）。
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.state import AppState

MECH_THRESHOLD = 12000  # token 阈值（暂定，待真实长度分布再调）
HEAD_TURNS_KEEP = 1  # 保护尾部轮次数（当前轮上下文）
ONE_LINE_MAX = 120  # 机械压缩后一句摘要最长字符数
_CJK = re.compile(r"[　-鿿㐀-䶿]")


# ---- token 估算（近似即可）----
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if _CJK.match(ch))
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.35)  # 中文 1 字 ≈1.5 token，ASCII ≈0.35


def rows_tokens(rows: list[dict]) -> int:
    total = 0
    for m in rows:
        total += estimate_tokens(str(m.get("content") or "")) + estimate_tokens(str(m.get("sql") or ""))
    return total


# ---- 机械压缩（零模型调用）----
def _one_line(text: str) -> str:
    """本地截断为一句摘要（非 LLM）。去换行 + 砍头。"""
    t = (text or "").replace("\n", " ").replace("\r", " ").strip()
    if not t:
        return ""
    if len(t) > ONE_LINE_MAX:
        t = t[: ONE_LINE_MAX] + "…"
    return f"[压缩] {t}"


def _is_card_row(m: dict) -> bool:
    return m.get("kind") in ("sql_card",)


def _is_pending_dml(m: dict) -> bool:
    # WS4 未决状态（确认中 run_dml）：现存卡以 verdict=review 且含 dml 语境兜底；待 WS4 落 pending_dml 标志
    return bool(m.get("verdict") == "review") and m.get("kind") in ("sql_card",)


def mechanical_compress(
    rows: list[dict], threshold: int = MECH_THRESHOLD
) -> dict[str, Any]:
    """机械压缩：从最旧轮次开始，仅压缩不属于卡片骨架/尾部当前轮的叙事行（think 丢弃、text/report/clarify 截断言）。
    返回 {rows, compressed, tokens_before, tokens_after, over}。全程不调模型。"""
    tokens_before = rows_tokens(rows)
    if tokens_before <= threshold:
        return {"rows": rows, "compressed": 0, "tokens_before": tokens_before,
                "tokens_after": tokens_before, "over": False}
    # 定位尾部当前轮：最后一次 user 行及其后的响应一律不动
    head_idx = 0
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("role") == "user":
            head_idx = i
            break
    kept: list[dict] = []
    compressed = 0
    for i, m in enumerate(rows):
        if i >= head_idx or _is_card_row(m) or _is_pending_dml(m):
            kept.append(dict(m))  # 卡片骨架 / 尾部当前轮 / 未决 → 原样保留
            continue
        kind = m.get("kind", "text")
        if kind == "think":
            continue  # 元数据，丢弃
        cur = dict(m)
        c = str(m.get("content") or "")
        if c:
            cur["content"] = _one_line(c)
            compressed += 1
        kept.append(cur)
    tokens_after = rows_tokens(kept)
    return {"rows": kept, "compressed": compressed, "tokens_before": tokens_before,
            "tokens_after": tokens_after, "over": tokens_after > threshold}


# ---- LLM 兜底摘要（单管道：脱敏进 + manifest + egress 审计）----
async def llm_compress_oldest(
    state: "AppState", conn_id: str, rows: list[dict], threshold: int = MECH_THRESHOLD
) -> dict[str, Any]:
    """机械压完仍超限时的 LLM 摘要旧轮次。非 mock 才走；mock 降级维持机械结果（不再调模型）。
    返回 {rows, summarized, over, degraded}。LLM 调用恒走 脱敏→manifest→egress 审计(source=egress-compress)。"""
    tokens_before = rows_tokens(rows)
    if tokens_before <= threshold:
        return {"rows": rows, "summarized": 0, "over": False, "degraded": False}
    try:
        from app.ai import gateway as gw
        from app.ai.provider_cfg import resolve_provider_cfg

        cfg = resolve_provider_cfg(state, type("_R", (), {"reasoning": "off", "model_id": None},
                                                    )())
        if gw.is_effective_mock(cfg):
            return {"rows": rows, "summarized": 0, "over": True, "degraded": True}
        # 取最旧的叙事区（卡片与尾部当前轮之外）作摘要输入
        head_idx = 0
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].get("role") == "user":
                head_idx = i
                break
        old = rows[:head_idx]
        narr = [m for m in old if not _is_card_row(m) and m.get("kind") in ("text", "report", "clarify")]
        if not narr:
            return {"rows": rows, "summarized": 0, "over": True, "degraded": False}
        # 脱敏进（D9 单管道）：拼接旧叙事，redact 后送模型
        joined = "\n".join(str(m.get("content") or "") for m in narr)[:6000]
        try:
            from app.safety.redact import get_salt, redact_text
            from app.config import get_env

            salt = get_salt(get_env().data_dir)
            try:
                sensitive = state.connections.get(conn_id).sensitive
            except Exception:
                sensitive = []
            redacted, _mp = redact_text(joined, salt, sensitive)
        except Exception:
            redacted = joined
        # 出网清单（先于模型调用经中央拦截器落审计，source=egress-compress；清单随 ctx 原样审计）
        try:
            from app.ai.manifest import build_manifest

            manifest = build_manifest(state, conn_id, {"candidate_tables": [], "kb_docs": 0},
                                      [{"role": "user", "content": joined}], False, cfg, redacted)
        except Exception:
            manifest = {"tables": [], "kb_docs": 0, "history_turns": 1, "mode": "standard",
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": cfg.get("model", ""), "provider": "llm-compress"}
        provider = gw.build_provider(cfg)
        resp = await provider.chat([
            {"role": "system", "content": "把下面的历史对话浓缩成一句话（中文，≤80 字），只保留事实性要点，不编造；直接给摘要正文。"},
            {"role": "user", "content": redacted},
        ], ctx={
            "conn_id": conn_id, "connection": conn_id, "skill": "compress",
            "source": "egress", "status": "egress-compress",
            "include_data": False, "context_meta": {"candidate_tables": [], "kb_docs": 0},
            "manifest": manifest,
        })
        summary = (getattr(resp, "content", "") or "").strip()
        if not summary:
            return {"rows": rows, "summarized": 0, "over": True, "degraded": False}
        marker = {"role": "assistant", "kind": "text", "content": f"[早期对话 LLM 摘要] {summary[:200]}", "sql": None, "verdict": None}
        new_rows = [marker] + [m for m in old if _is_card_row(m)] + rows[head_idx:]
        tokens_after = rows_tokens(new_rows)
        return {"rows": new_rows, "summarized": len(narr), "over": tokens_after > threshold, "degraded": False}
    except Exception:
        return {"rows": rows, "summarized": 0, "over": True, "degraded": True}


async def compress_hist(state: "AppState", conn_id: str, rows: list[dict]) -> dict[str, Any]:
    """对外一步：机械优先 → 仍超限再 LLM 兜底。返回最终 rows + 压缩统计。"""
    mech = mechanical_compress(rows)
    rows = mech["rows"]
    stats = {"mechanical_compressed": mech["compressed"], "tokens_before": mech["tokens_before"], "tokens_after": mech["tokens_after"]}
    if mech["over"]:
        res = await llm_compress_oldest(state, conn_id, rows)
        rows = res["rows"]
        stats.update({"llm_summarized": res["summarized"], "degraded": res["degraded"]})
    return {"rows": rows, "stats": stats}