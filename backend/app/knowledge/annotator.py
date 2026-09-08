"""AI 自动注释：DDL 上下文 + 样本值 → 模型 → 中文注释/标签草案（draft）。

构建管线（store.build）调用 annotate_tables() + annotate_domain()，
独立 API 保留 annotate_knowledge() / annotate_domain() 原入口。

隐私：样本值发送由构建前弹窗的 include_samples 控制；mock 网关时生成
确定性伪注释，保证无 key 也能跑通管线。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.ai import gateway as gw
from app.core.schema import get_schema, sample_values
from app.core.timeutil import utcnow_iso
from app.knowledge.ddl_context import is_noise_column

if TYPE_CHECKING:
    from app.state import AppState

logger = logging.getLogger(__name__)

# KB 阶段2/3 推理调用专用读超时（秒）：推理/plan 模式一次生成可达 2~5 分钟，
# 默认 120s 容易被 106 限流/慢推理撞爆 → 单独放宽，别拖垮普通 chat 的默认目标。
_KB_REASONING_TIMEOUT = 480.0

# 阶段一逐表注释读超时：单表一次 LLM 不该跑 8 分钟（480s 是图谱全局扫描的放宽值）。
# 实测单表最长成功 ≈270s，180s 超时的表快速失败跳过（单表失败不影响其余表）。
_KB_ANNOTATE_TIMEOUT = 180.0

# 注释缓存版本：prompt 模板 / 解析契约 / 缓存键组成变更时手动 bump（防旧缓存污染新输出）。
# v2：表级新增 profile 字段（AI 表画像 = 唯一向量文本来源，取代代码拼接）。
# v3：注释/画像风格专业化——书面精炼、术语准确，口语叫法只进「又称」（2026-09 用户裁定）。
PROMPT_VERSION = "v3"


def _kb_reason_provider_cfg(rt, reasoning: bool = True, timeout: float | None = None) -> dict[str, Any]:
    """KB 构建专用 provider cfg：统一思考方言直传，协议映射/降级全在 providers 适配层。

    - reasoning=False（阶段2 领域划分：归类命名任务，秒级）→ off 显式关思考；
    - reasoning=True → 运行时 kb_build_reasoning_effort 档位（off/low/medium/high）直传。
      适配层按供应商映射（方舟 effort 全档直传 / DeepSeek medium→high 服务端映射 /
      GLM-5.3 强制思考降级等），KB 不感知协议差异，亦无 thinking_budget 特判
      （历史问题：budget 在方舟 deepseek-v4 上被静默忽略 → 单表 1.3 万 reasoning tokens）。
    timeout：调用方可覆盖读超时下限（如逐表注释 180s；缺省用 _KB_REASONING_TIMEOUT=480s）。"""
    cfg = rt.provider_config()
    cfg["timeout"] = max(float(cfg.get("timeout") or 120), timeout or _KB_REASONING_TIMEOUT)
    effort = (getattr(rt, "kb_build_reasoning_effort", "low") or "low").strip().lower()
    if not reasoning or effort == "off":
        cfg["reasoning"] = "off"
        return cfg
    cfg["reasoning"] = effort if effort in ("low", "medium", "high") else "auto"
    return cfg


def _prompt_schema(schema: dict[str, Any], samples: dict[str, dict[str, list[Any]]]) -> str:
    lines: list[str] = []
    for t in schema["tables"]:
        cols = [c for c in schema["columns"] if c["table"] == t["name"]]
        tc = f"（已有注释：{t['comment']}）" if t.get("comment") else ""
        lines.append(f"- 表 {t['name']}{tc}")
        for c in cols:
            extra = ""
            sv = samples.get(t["name"], {}).get(c["name"]) if samples else None
            if sv:
                vals = [str(v) for v in sv if v is not None][:5]
                if vals:
                    extra = f"，样本值: {vals}"
            cc = f"（已有注释：{c['comment']}）" if c.get("comment") else ""
            lines.append(f"  - {c['name']}: {c['type']}{cc}{extra}")
    return "\n".join(lines)


def _parse_items(text: str) -> list[dict[str, Any]]:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        data = json.loads(t)
    except Exception:
        m = re.search(r"\[.*\]", t, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict) or not it.get("table"):
            continue
        comment = str(it.get("comment", "")).strip()
        if not comment:
            continue
        item: dict[str, Any] = {"table": it["table"], "column": it.get("column"), "comment": comment}
        values = str(it.get("values") or "").strip()  # 取值对照（可选，"P=待付款；S=已发货"）
        if values:
            item["values"] = values
        if item["column"] is None:
            profile = str(it.get("profile") or "").strip()  # 表画像（向量文本唯一 AI 来源）
            if profile:
                item["profile"] = profile[:600]  # 提示词要求 150~300 字，代码只兜超长上限
        if item["column"] and it.get("core") is True:
            item["core"] = True  # AI 关键列提名（合成器选材信号）
        out.append(item)
    return out


def _mock_comments(schema: dict[str, Any], samples: dict[str, dict[str, list[Any]]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for t in schema["tables"]:
        if not t.get("comment"):
            items.append({
                "table": t["name"], "column": None,
                "comment": f"{t['name']} 表：{t.get('column_count', 0)} 列的业务实体。",
            })
    for c in schema["columns"]:
        if c.get("comment"):
            continue
        extra = ""
        sv = samples.get(c["table"], {}).get(c["name"]) if samples else None
        if sv:
            vals = [str(v) for v in sv if v is not None][:3]
            if vals:
                extra = f"，示例取值如 {vals}"
        items.append({
            "table": c["table"], "column": c["name"],
            "comment": f"列 {c['name']}，类型 {c['type']}{extra}。",
        })
    return items


# ---------- 单表 DDL annotation（构建管线调用） ----------

ENUM_MAX_VALUES = 50  # 单列去重取值数超过此数视为非枚举（长文本/主键），不产 values


def _annotation_prompt_sampled(
    table_ddl: str, samples_ref: str, db_tables: str = ""
) -> str:
    """有采样版 prompt：样本取值段 + values 对照指令 + 含 values 的 JSON 契约。

    已有注释内嵌在 DDL 的 /* comment */ 里，随 DDL 发给模型；准则里声明其仅供参考。
    db_tables：库内全部表清单（含表注释），帮模型理解该表在整体业务中的位置。
    """
    from app.ai.prompts import render
    return render(
        "annotator_column_sampled",
        table_ddl=table_ddl, samples_ref=samples_ref, db_tables=db_tables,
    )


def _annotation_prompt_unsampled(table_ddl: str, db_tables: str = "") -> str:
    """无采样版 prompt：纯结构，不提 values（不诱导编造取值含义）。"""
    from app.ai.prompts import render
    return render("annotator_column_unsampled", table_ddl=table_ddl, db_tables=db_tables)


def _first_example(samples: dict[str, list[Any]] | None, column: str) -> str:
    """示例值：该列首个非空样本，截断 60 字符（无样本/未授权 → 空，天然门控）。"""
    for v in (samples or {}).get(column, []) or []:
        if v is not None and str(v) != "":
            return str(v)[:60]
    return ""


def _distinct_values(samples: dict[str, list[Any]] | None, column: str) -> list[str]:
    """去重取值（保序），供低基数离散列的 values 对照生成。"""
    seen: list[str] = []
    for v in (samples or {}).get(column, []) or []:
        if v is None:
            continue
        s = str(v)
        if s and s not in seen:
            seen.append(s)
            if len(seen) > ENUM_MAX_VALUES:
                break
    return seen


def _mock_values_for(column: str, samples: dict[str, list[Any]] | None) -> str:
    """mock：对去重值 ∈ [2,50] 的列生成确定性占位对照（真实含义待人工确认）。"""
    distinct = _distinct_values(samples, column)
    if len(distinct) < 2 or len(distinct) > ENUM_MAX_VALUES:
        return ""
    return "；".join(f"{v}={v}（业务含义待确认）" for v in distinct)


def _split_enum_items(text: str) -> list[str]:
    """按逗号切分枚举值列表（兼容值内带逗号/引号），去空。"""
    items: list[str] = []
    cur, in_q, q = "", False, ""
    for ch in text:
        if in_q:
            cur += ch
            if ch == q:
                in_q = False
        elif ch in ("'", '"'):
            in_q, q = True, ch
        elif ch == ",":
            if cur.strip():
                items.append(cur.strip().strip("'").strip('"'))
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip().strip("'").strip('"'))
    return items


def _explicit_enum(table_ddl: str, column: str, col_type: str = "") -> list[str] | None:
    """结构显式枚举（不依赖样本）：MySQL ENUM 类型 或 DDL 的 CHECK (col IN (...)) 约束。

    返回去重保序的枚举值列表；无显式枚举 → None。仅凭结构，不编造。
    """
    values: list[str] = []
    m = re.search(r"ENUM\s*\(([^)]*)\)", col_type or "", re.IGNORECASE)
    if m:
        values.extend(_split_enum_items(m.group(1)))
    if not values:
        col_pat = re.escape(column)
        m = re.search(
            rf"CHECK\s*\(\s*[`\"\[\]]?{col_pat}[`\"\]]?\s+IN\s*\(([^)]*)\)",
            table_ddl, re.IGNORECASE,
        )
        if m:
            values.extend(_split_enum_items(m.group(1)))
    if not values:
        return None
    seen: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return seen or None


def _enum_placeholder(enum: list[str]) -> str:
    """结构显式枚举 → 占位 key-value 对照（无业务含义时的兜底，与 mock 占位同款）。"""
    return "；".join(f"{v}={v}（业务含义待确认）" for v in enum)


def _mock_table_comments_from_ddl(
    table_name: str,
    columns: list[dict[str, Any]],
    samples: dict[str, list[Any]] | None,
    table_comment: str = "",
    table_ddl: str = "",
) -> list[dict[str, Any]]:
    """mock：为单表生成伪注释（无 LLM 时的 fallback）；低基数列附 values/example。

    表级：始终产出 {comment 提案, profile 画像}（mock 扮演 LLM，与真实模型输出契约一致；
    提案不覆盖当前值，库注释权威性不受影响）。values 来源：有样本 → 去重值占位对照；
    无样本 → 结构显式枚举（CHECK/ENUM）。example 仅在授权样本时产生。
    """
    items: list[dict[str, Any]] = []
    biz_cols = [c["name"] for c in columns if not is_noise_column(c["name"])][:8]
    profile = f"{table_name} 表。关键字段：{'、'.join(biz_cols)}。" if biz_cols else f"{table_name} 表。"
    items.append({
        "table": table_name, "column": None,
        "comment": table_comment or f"{table_name} 表：{len(columns)} 列的业务实体。",
        "profile": profile[:300],
    })
    for c in columns:
        extra = ""
        if samples:
            vals = [str(v) for v in (samples or {}).get(c["name"], []) if v is not None][:3]
            if vals:
                extra = f"，示例取值如 {vals}"
        item: dict[str, Any] = {
            "table": table_name, "column": c["name"],
            "comment": f"列 {c['name']}，类型 {c.get('type', '')}{extra}。",
        }
        values = _mock_values_for(c["name"], samples)
        if not values:
            enum = _explicit_enum(table_ddl, c["name"], c.get("type", ""))
            if enum:
                values = _enum_placeholder(enum)
        if values:
            item["values"] = values
        example = _first_example(samples, c["name"])
        if example:
            item["example"] = example
        items.append(item)
    return items


def annotation_input_hash(
    table_name: str,
    table_ddl: str,
    samples: dict[str, list[Any]] | None,
    model: str,
) -> str:
    """注释缓存键：表名 + DDL + 规范化样本 + PROMPT_VERSION + 模型名。

    规范化：样本列排序、值序列化后排序（采样顺序抖动不产生新键）。
    采样与否已体现在样本内容里（未授权 → 空 dict → 不同键）。
    """
    norm_samples = {
        col: sorted(str(v) for v in (vals or []) if v is not None)
        for col, vals in sorted((samples or {}).items())
    }
    canon = json.dumps({
        "pv": PROMPT_VERSION,
        "table": table_name,
        "ddl": table_ddl,
        "samples": norm_samples,
        "model": model or "",
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canon.encode()).hexdigest()


def _cache_put(cache: Any, table_name: str, input_hash: str, items: list[dict[str, Any]]) -> None:
    """注释缓存写入：仅成功产出非空 items（后处理后的最终形态）才缓存；
    失败/空解析不缓存 → 重试/单表重注自然重新调 LLM。"""
    if cache is None or not input_hash or not items:
        return
    cache[table_name] = {
        "input_hash": input_hash,
        "items": json.loads(json.dumps(items, ensure_ascii=False)),
        "created_at": utcnow_iso(),
    }


def _related_tables(schema: dict[str, Any], table_name: str, cap: int = 30) -> list[str]:
    """逐表注释的业务上下文表清单：FK 邻表 + 同前缀表（大库 token 治理）。

    小库（≤cap）返回全部表（行为与历史一致）；大库只给相关表——
    每表 prompt 附带全库清单的 token 成本随表数平方增长，裁剪后线性。
    无任何关联信号时回退字母序前 cap 张。
    """
    tables = sorted(t["name"] for t in schema.get("tables", []))
    if len(tables) <= cap:
        return tables
    related: set[str] = set()
    for f in schema.get("foreign_keys", []):
        if f["table"] == table_name:
            related.add(f["ref_table"])
        if f["ref_table"] == table_name:
            related.add(f["table"])
    prefix = table_name.split("_")[0]
    related |= {t for t in tables if t.split("_")[0] == prefix}
    related.discard(table_name)
    if not related:
        return tables[:cap]
    ordered = sorted(related)
    if len(ordered) > cap:
        ordered = ordered[:cap]
    return ordered


async def annotate_table(
    state: "AppState",
    conn_id: str,
    table_name: str,
    table_ddl: str,
    schema: dict[str, Any],
    samples: dict[str, dict[str, list[Any]]] | None = None,
    cache: Any = None,
) -> list[dict[str, Any]]:
    """为单张表生成列级注释（DDL 做上下文，可选样本值）。

    无论表已有注释与否，都让 AI 生成语义注释（有注释时 AI 参考但不盲信）。
    返回 items 列表（未落库，由调用方统一 annotate_drafts）：
    [{table, column, comment, values?, example?}]。
    values/example 仅在授权样本（include_samples）时产生——天然门控。

    cache：注释缓存访问器（facade 注入，见 KnowledgeBase.annotation_cache）。
    None = 不走缓存。命中（input_hash 一致）直接返回缓存 items，跳过 LLM；
    仅成功产出非空 items 后才写缓存（失败不缓存，重试自然重新调用）。

    返回 (items, source)：source ∈ cache|llm|mock|empty（成本可观测）。
    """
    rt = state.runtime.get()
    columns = [c for c in schema.get("columns", []) if c["table"] == table_name]
    table_comment = next(
        (t.get("comment", "") for t in schema.get("tables", []) if t["name"] == table_name), ""
    )
    table_samples = samples.get(table_name) if samples else None

    provider_cfg = _kb_reason_provider_cfg(rt, timeout=_KB_ANNOTATE_TIMEOUT)  # 阶段一逐表注释：独立读超时（180s），不被图谱档位的 480s 拖累

    input_hash = ""
    if cache is not None:
        input_hash = annotation_input_hash(table_name, table_ddl, table_samples, provider_cfg.get("model", ""))
        cached = cache.get(table_name)
        if cached and cached.get("input_hash") == input_hash:
            items = json.loads(json.dumps(cached.get("items") or []))  # 深拷贝，调用方可自由改写
            logger.debug("[kb.annotate] conn=%s 注释缓存命中 table=%s", conn_id, table_name)
            return items, "cache"
    if gw.is_effective_mock(provider_cfg):
        logger.debug("[kb.annotate] conn=%s mock 伪注释 table=%s", conn_id, table_name)
        items = _mock_table_comments_from_ddl(table_name, columns, table_samples, table_comment, table_ddl)
        _cache_put(cache, table_name, input_hash, items)
        return items, ("mock" if items else "empty")

    # 样本值段（授权才有样本 → values/example 的天然门控）；已有注释内嵌在 DDL，不单独重复
    samples_ref = "【样本取值（真实数据；用于辅助理解字段含义）】\n"
    if table_samples:
        sample_lines = []
        for c in columns:
            vals = table_samples.get(c["name"])
            if vals:
                val_strs = [str(v) for v in vals if v is not None][:5]
                if val_strs:
                    sample_lines.append(f"- {c['name']}: {val_strs}")
        if sample_lines:
            samples_ref += "\n".join(sample_lines)
        else:
            samples_ref = ""
    else:
        samples_ref = ""

    # 两套模板（段4）：有采样版（样本段+values 指令+含 values 契约）/
    # 无采样版（纯结构，不提 values）。按是否有授权样本分流，绝不共用。
    # 库内相关表清单（FK 邻表 + 同前缀，大库裁剪）作为业务上下文
    related = _related_tables(schema, table_name)
    tables_idx = {t["name"]: t for t in schema.get("tables", [])}
    db_tables = "\n".join(
        f"- {t}" + (f"（{tables_idx[t]['comment']}）" if tables_idx[t].get("comment") else "")
        for t in related
    )
    prompt = (
        _annotation_prompt_sampled(table_ddl, samples_ref, db_tables)
        if table_samples
        else _annotation_prompt_unsampled(table_ddl, db_tables)
    )
    provider = gw.build_provider(provider_cfg)
    # 中央记账拦截器 ctx：一次请求/响应一组（llm_log + cost_tracker + egress 清单）
    resp = await provider.chat(
        [{"role": "user", "content": prompt}], tools=None,
        ctx={
            "conn_id": conn_id, "connection": conn_id, "skill": "kb-annotation",
            "source": "kb_build", "status": "egress-annotation",
            "context_meta": {"candidate_tables": [table_name]},
        },
    )
    items = _parse_items(resp.content or "")
    # 表级定位必填闸（prompt 任务一）：缺失视为格式错误 → 追加纠偏消息重试一次；
    # 重试仍缺不硬造（表头由合成端兜底链补齐），只留 warning 便于观察模型遵循度。
    if not any(it.get("column") is None for it in items):
        try:
            fix = await provider.chat(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": (resp.content or "")[:2000]},
                    {"role": "user", "content": "你的输出缺少表级定位项（无 column 字段的 {\"table\",...} 条目，40~60 字，可含「又称：…」）。请重新返回完整 JSON 数组：表级项在最前，再跟全部列级项。"},
                ],
                tools=None,
                ctx={
                    "conn_id": conn_id, "connection": conn_id, "skill": "kb-annotation",
                    "source": "kb_build", "status": "egress-annotation-retry",
                    "context_meta": {"candidate_tables": [table_name]},
                },
            )
            items_retry = _parse_items(fix.content or "")
            if any(it.get("column") is None for it in items_retry):
                items = items_retry
                logger.info("[kb.annotate] conn=%s 表级缺失纠偏成功 table=%s", conn_id, table_name)
            else:
                logger.warning("[kb.annotate] conn=%s 表级定位仍缺失 table=%s（交合成端兜底）", conn_id, table_name)
        except Exception as e:  # noqa: BLE001 —— 纠偏重试失败不致命，走兜底
            logger.warning("[kb.annotate] conn=%s 表级纠偏重试失败 table=%s：%s", conn_id, table_name, e)
    # 表级项（column 省略）保留：AI 表级语义描述作为补充写入 proposed_comment，
    # 与库注释（db_comment）共存——库注释权威存 db_comment，AI 描述存 comment，
    # 两者不竞争（confirm 提升 proposed_comment 到 comment；向量文本取 vector_profile）。
    # 表级 profile（AI 表画像）随 items 落 proposed_profile，confirm 后成为向量文本唯一 AI 来源。
    # 无采样硬闸（段4）：无授权样本 → 丢弃 LLM 可能硬凑的 example；
    # values 回退到结构显式枚举（CHECK/ENUM），没有则不带键——不编造数据知识。
    if not table_samples:
        for it in items:
            it.pop("values", None)
            it.pop("example", None)
            col_name = it.get("column")
            if col_name:
                ctype = next((c.get("type", "") for c in columns if c["name"] == col_name), "")
                enum = _explicit_enum(table_ddl, col_name, ctype)
                if enum:
                    it["values"] = _enum_placeholder(enum)
    else:
        # example 从样本提取（后端规范化，不依赖 LLM）：首个非空值截断 60
        for it in items:
            if it.get("column"):
                example = _first_example(table_samples, it["column"])
                if example:
                    it["example"] = example
    # 缓存写入（统一走 _cache_put：失败/空解析不缓存）
    _cache_put(cache, table_name, input_hash, items)
    return items, ("llm" if items else "empty")


async def annotate_tables(
    state: "AppState",
    conn_id: str,
    ddl_map: dict[str, str],
    schema: dict[str, Any],
    samples: dict[str, dict[str, list[Any]]] | None = None,
    on_progress: Any | None = None,
    p0: int = 15,
    p1: int = 40,
    concurrency: int | None = None,
    limiter: "_AdaptiveLimiter | None" = None,  # 共享全局限流（构建期各阶段共用一个实例）
    cache: Any = None,
) -> dict[str, Any]:
    """逐表 DDL annotation 批量入口：注释落库 + 结构化结果。

    ddl_map: {table_name: ddl_string}，由 ddl_context.generate_ddls_all 生成。
    on_progress(stage, percent, detail, phase) 用于构建进度回调（阶段一「AI 正在处理」）。
    cache: 注释缓存访问器（透传给 annotate_table；None = 不缓存）。

    返回 {added, failed_tables, cached_tables, llm_tables}：
    - failed_tables：重试耗尽仍失败的表（审核镜头展示 + 单表重试入口的数据源）；
    - cached_tables / llm_tables：缓存命中 / 实际调 LLM 的表（成本可观测）。

    并发：每表独立 LLM 调用并与 provider 并发上限自适应——撞 429/瞬断则指数退避重试 +
    并发额度减半（最低 1）；连续成功再温和回升。避免硬顶 N 并发把低配额 provider 打爆。
    concurrency 缺省取环境变量 TABLETALK_KB_ANNOTATION_CONCURRENCY（默认 10）；
    limiter 传入时忽略 concurrency（构建期与阶段二~四共享同一自适应实例）。
    """
    items_all: list[dict[str, Any]] = []
    failed_tables: list[str] = []
    cached_tables: list[str] = []
    llm_tables: list[str] = []
    _t0ai = time.monotonic()
    table_names = [t["name"] for t in schema.get("tables", []) if t["name"] in ddl_map]
    n = max(1, len(table_names))
    if not table_names:
        return {"added": 0, "failed_tables": [], "cached_tables": [], "llm_tables": []}
    if limiter is None:
        cap = concurrency if concurrency else int(
            os.environ.get("TABLETALK_KB_ANNOTATION_CONCURRENCY", "10")
        )
        limiter = _AdaptiveLimiter(cap)
    done = {"n": 0}
    logger.info("[kb.annotate] conn=%s 开始逐表注释（并发 ≤ %s）：tables=%s", conn_id, limiter.cap, len(table_names))

    async def _one(name: str):
        await limiter.acquire()
        _t_tbl = time.monotonic()
        try:
            try:
                tbl_items, src = await _retry_chat(
                    lambda: annotate_table(state, conn_id, name, ddl_map[name], schema, samples, cache=cache),
                    limiter,
                )
            except Exception as e:  # noqa: BLE001 —— 重试耗尽后单表失败不影响其余表
                logger.warning("[kb.annotate] conn=%s 单表注释失败 table=%s：%s", conn_id, name, e)
                tbl_items, src = [], "failed"
                failed_tables.append(name)
            if tbl_items:
                (cached_tables if src == "cache" else llm_tables).append(name)
            elif src == "empty":
                # 空产出：不算失败（AI 可能确实无话可说），记录便于观察
                logger.info("[kb.annotate] conn=%s 单表零产出 table=%s", conn_id, name)
            done["n"] += 1
            _tbl_el = time.monotonic() - _t_tbl
            if _tbl_el > 30:
                logger.warning("[kb.annotate] conn=%s 慢表 table=%s 耗时 %.1fs（含重试）", conn_id, name, _tbl_el)
            if on_progress:
                on_progress(
                    "AI 正在处理", p0 + (p1 - p0) * done["n"] // n,
                    f"已完成 {done['n']}/{n}", phase="annotate",
                    step="per_table", step_index=done["n"], step_total=n,
                )
            return name, tbl_items
        finally:
            await limiter.release()

    results = await asyncio.gather(*(_one(t) for t in table_names), return_exceptions=True)
    for r in results:
        # 裸异常元素（如 on_progress 抛出的协作取消）不是二元组——先判型再解包，
        # 否则 TypeError 打穿整批，破坏"单表失败不影响其余表"的隔离契约。
        if isinstance(r, asyncio.CancelledError):
            raise r  # 协作取消必须向上传播（except Exception 拦不住 BaseException）
        if isinstance(r, BaseException):
            logger.warning("[kb.annotate] conn=%s 单表注释任务异常：%s", conn_id, r)
            continue
        name, tbl_items = r
        if tbl_items:
            items_all.extend(tbl_items)
    if not items_all and table_names:
        logger.warning("[kb.annotate] conn=%s LLM 返回解析为空：处理了 %s 张表但零产出", conn_id, len(table_names))
    added = await state.knowledge.annotate_drafts_async(conn_id, items_all)
    logger.info("[kb.annotate] conn=%s 逐表注释完成：items=%s added=%s cached=%s llm=%s failed=%s（总耗时 %.1fs）",
                conn_id, len(items_all), added, len(cached_tables), len(llm_tables),
                len(failed_tables), time.monotonic() - _t0ai)
    return {"added": added, "failed_tables": failed_tables,
            "cached_tables": cached_tables, "llm_tables": llm_tables}


# ---------- 并发自适应（阶段一逐表注释用） ----------

_REQUEUABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
_KB_CONCURRENCY_MAX_RETRIES = 4
_KB_CONCURRENCY_BACKOFF_BASE = 1.5
_KB_CONCURRENCY_RECOVER_STREAK = 4  # 连续成功 N 颗才回升 1 档并发


class _AdaptiveLimiter:
    """逐表注释并发限制器：动态调节最大并发。

    - 撞 429（限流）→ 并发额度**减半**（≥1）；provider 只给 1~2 并发也能最终匹配；
    - 连续成功 _RECOVER_STREAK 次 → 温和回升 1 档（上限 cap）；
    - 等待者由 Condition 唤醒；限制只对下次 acquire 生效，不影响在飞请求。
    """

    def __init__(self, cap: int) -> None:
        self.cap = max(1, cap)
        self.limit = self.cap
        self._active = 0
        self._cond = asyncio.Condition()
        self._streak = 0

    async def acquire(self) -> None:
        async with self._cond:
            while self._active >= self.limit:
                await self._cond.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify(1)

    async def on_rate_limit(self) -> None:
        """限流 → 并发下调（不立即踢人，只让后续 acquire 更严格）。"""
        async with self._cond:
            new_limit = max(1, self.limit // 2)
            if new_limit < self.limit:
                prev = self.limit
                self.limit = new_limit
                self._streak = 0
                logger.info("[kb.annotate] provider 限流 → 并发额度 %s→%s", prev, new_limit)

    async def on_success(self) -> None:
        async with self._cond:
            self._streak += 1
            if self._streak >= _KB_CONCURRENCY_RECOVER_STREAK and self.limit < self.cap:
                self.limit = min(self.cap, self.limit + 1)
                self._streak = 0
                self._cond.notify_all()
                logger.info("[kb.annotate] provider 稳定 → 并发回升 1 档（%s/%s）", self.limit, self.cap)


def _retryable(e: Exception) -> tuple[bool, bool]:
    """→ (是否可重试, 是否限流)；限流同时触发并发下调。"""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code in _REQUEUABLE_HTTP_CODES, code == 429
    # 读超时（ReadTimeout）= 模型本身慢/推理过久，重试无意义且耗时翻倍 → 不重试，交由上层降级
    if isinstance(e, httpx.ReadTimeout):
        return False, False
    if isinstance(e, httpx.RequestError):
        return True, False
    return False, False


async def _retry_chat(coro_factory: Any, limiter: _AdaptiveLimiter, *, max_retries: int = _KB_CONCURRENCY_MAX_RETRIES) -> Any:
    """对一次 LLM 调用做指数退避重试（限流/瞬断才重试）；限流同时下调并发。"""
    last: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await coro_factory()
            await limiter.on_success()
            return result
        except Exception as e:  # noqa: BLE001
            last = e
            retryable, rate_limited = _retryable(e)
            if not retryable:
                raise
            if rate_limited:
                await limiter.on_rate_limit()
            backoff = min(30.0, _KB_CONCURRENCY_BACKOFF_BASE * (2 ** (attempt - 1)))
            delay = backoff + random.uniform(0.0, 0.4)
            logger.warning(
                "[kb.annotate] 表注释%s限流/瞬断（尝试 %s/%s，%s）→ %.1fs 后重试",
                "触发" if rate_limited else "遇", attempt, max_retries, e, delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"重试 {max_retries} 次仍失败：{last}") from last


# ---------- 独立 API 路径（保持向后兼容） ----------


async def annotate_knowledge(
    state: "AppState",
    conn_id: str,
    include_samples: bool | None = None,
    schema: dict[str, Any] | None = None,
    samples: dict[str, dict[str, list[Any]]] | None = None,
) -> dict[str, Any]:
    """独立 API 入口：为全部表生成注释草案。

    schema / samples 由外部传入时直接使用（构建管线场景），
    否则自行从数据库读取（独立 API 调用场景）。
    """
    if schema is None:
        schema = await get_schema(state, conn_id)
    rt = state.runtime.get()
    if include_samples is None:
        include_samples = rt.kb_ai_annotation_samples

    if samples is None:
        samples = {}
        if include_samples and rt.kb_sample_rows > 0:
            for t in schema["tables"]:
                try:
                    samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
                except Exception:
                    samples[t["name"]] = {}

    # 出网不变量：授权样本发送前统一值级截断（用户决策：无列级过滤，唯一防护是截断）
    from app.knowledge.ddl_context import truncate_samples  # noqa: PLC0415
    samples = truncate_samples(samples) if samples else samples

    provider_cfg = _kb_reason_provider_cfg(rt, timeout=_KB_ANNOTATE_TIMEOUT)  # 单表注释 API 同样用逐表档位（180s）
    if gw.is_effective_mock(provider_cfg):
        items = _mock_comments(schema, samples)
    else:
        # 独立 API 时无 DDL，使用旧 schema 格式
        from app.ai.prompts import render
        prompt = render("annotator_kb_comment", schema_block=_prompt_schema(schema, samples))
        provider = gw.build_provider(provider_cfg)
        resp = await provider.chat(
            [{"role": "user", "content": prompt}], tools=None,
            ctx={
                "conn_id": conn_id, "connection": conn_id, "skill": "kb-annotation",
                "source": "kb_build", "status": "egress-annotation",
                "context_meta": {"candidate_tables": [t["name"] for t in schema.get("tables", [])]},
            },
        )
        items = _parse_items(resp.content or "")
    # API 版走 annotator_kb_comment 旧 prompt（表级为附带任务），不强制表级闸

    added = await state.knowledge.annotate_drafts_async(conn_id, items)
    return {"items": len(items), "added": added, "samples_used": include_samples}


# ---------- 领域划分（KC2·段6：画像打包 → 单轮划分） ----------

_MOCK_TAG_HINTS = {
    "order": "订单", "product": "商品", "customer": "客户", "return": "退货",
    "pay": "支付", "ship": "物流", "inventory": "库存", "review": "评价",
    "categor": "分类", "supplier": "供应商", "campaign": "营销", "address": "地址",
}


def _climb_pct(elapsed: float, p_from: int, p_to: int, climb_seconds: float) -> int:
    """两段式时间爬坡（纯函数）：前 20s 爬完窗口一半（保留快速反馈），
    剩余一半在 climb_seconds 内匀速走完——长推理（数分钟级）期间条持续缓慢移动、
    永不提前封顶，只有真实完成才由调用方打满。恒 ≤ p_to-1 且单调不减。"""
    span = max(1, p_to - p_from)
    if climb_seconds <= 0:
        return min(p_to - 1, p_from + span // 2)
    fast = min(elapsed, 20.0)
    pct = p_from + span // 2 * (fast / 20.0)
    if elapsed > 20.0:
        slow = min(elapsed - 20.0, climb_seconds)
        pct += (span - span // 2) * (slow / climb_seconds)
    return max(p_from, min(p_to - 1, int(pct)))


def _count_json_items(text: str, array_keys: tuple[str, ...] = ("[",)) -> int:
    """容忍式部分 JSON 计数：从流式累积文本里数"已完整的顶层对象"。

    数组元素判定：跟踪字符串/转义与括号深度，深度回到 1（在数组内）即计一个完整对象。
    解析不了（模型还没吐出数组开括号 / 文本非 JSON）→ 0，静默——只喂进度，不参与权威解析。
    """
    depth = 0
    in_str = False
    esc = False
    in_array = False
    count = 0
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
            if depth == 1:
                in_array = True
        elif ch == "]":
            depth = max(0, depth - 1)
            if depth == 0:
                in_array = False
        elif ch == "{":
            depth += 1
            if in_array and depth == 2:
                count += 1
        elif ch == "}":
            depth = max(0, depth - 1)
    return count


def _live_tail(buf: str, max_chars: int = 240) -> str:
    """滚动缓冲 → 单行尾文：压换行/多空白，保末 max_chars 字符。"""
    flat = " ".join((buf or "").split())
    return flat[-max_chars:]


async def _chat_with_beat(
    provider: Any,
    messages: list[dict[str, Any]],
    tools: Any,
    ctx: dict[str, Any] | None,
    *,
    on_progress: Any | None,
    stage: str, phase: str, step: str,
    step_index: int, step_total: int, percent: int,
    interval: float = 2.0,
    p_to: int | None = None,
    limiter: "_AdaptiveLimiter | None" = None,
    climb_seconds: float = 0.0,
    stream: bool = False,
    count_detail: str | None = None,
) -> Any:
    """LLM 调用期间进度驱动（阶段2/3 长推理专用）。

    - p_to 缺省 → 心跳帧 percent 恒为传入值（老语义，纯动画）；
    - p_to 给定 → **两段式时间爬坡**（_climb_pct）：前 20s 爬窗口一半，剩余在
      climb_seconds 内匀速，长推理期间条持续走、永不提前封顶，完成瞬间打满。
    - stream=True → 消费 provider.chat_stream：边收边累积全文 + 容忍式部分 JSON
      计数（count_detail 模板如"已识别 {n} 条关系"）→ detail 出**真实条目计数**；
      权威解析仍由调用方对返回的完整文本做（流式只喂进度，正确性零风险）。
      流式无 tool_calls 场景（阶段2/3 均无工具）；mock provider 走非流式直连。
    - busy 帧不校验取消（协作式取消仍只在真实 report 点收尾，语义不变）；
    - on_progress=None（独立 API 场景）→ 不启心跳，直连调用；
    - limiter 给定 → 本次调用按次占坑。
    """
    if limiter is not None:
        await limiter.acquire()
    try:
        return await _chat_with_beat_inner(
            provider, messages, tools, ctx,
            on_progress=on_progress, stage=stage, phase=phase, step=step,
            step_index=step_index, step_total=step_total, percent=percent,
            interval=interval, p_to=p_to, climb_seconds=climb_seconds,
            stream=stream, count_detail=count_detail,
        )
    finally:
        if limiter is not None:
            await limiter.release()


async def _chat_with_beat_inner(
    provider: Any,
    messages: list[dict[str, Any]],
    tools: Any,
    ctx: dict[str, Any] | None,
    *,
    on_progress: Any | None,
    stage: str, phase: str, step: str,
    step_index: int, step_total: int, percent: int,
    interval: float = 2.0,
    p_to: int | None = None,
    climb_seconds: float = 0.0,
    stream: bool = False,
    count_detail: str | None = None,
) -> Any:
    """主体：流式累积/心跳爬坡 + provider 调用。返回 LLMResponse（content 为完整文本）。"""
    if on_progress is None:
        if stream and hasattr(provider, "chat_stream"):
            text_parts: list[str] = []
            async for chunk in provider.chat_stream(messages, tools=tools, ctx=ctx):
                if chunk.delta:
                    text_parts.append(chunk.delta)
                elif chunk.content:
                    text_parts.append(chunk.content)
            return gw.ChatResponse(content="".join(text_parts))
        return await provider.chat(messages, tools=tools, ctx=ctx)

    conn_id = (ctx or {}).get("conn_id", "?")
    _t0 = time.monotonic()
    logger.info("[kb.llm] conn=%s 调用开始 stage=%s step=%s stream=%s",
                conn_id, stage, step, stream)
    stop = asyncio.Event()
    _warned = 0.0
    state = {"n": 0, "reasoning_tail": "", "content_tail": ""}

    def _report(elapsed: float) -> None:
        nonlocal _warned
        # 超长告警：等待 ≥60s 未返回 → warning 一次，此后每 30s 复报
        if elapsed >= 60 and elapsed - _warned >= 30:
            _warned = elapsed
            logger.warning("[kb.llm] conn=%s 调用超长：stage=%s step=%s 已等待 %.0fs（仍在等待）",
                           conn_id, stage, step, elapsed)
        detail = f"AI 思考中 {int(elapsed)}s"
        if state["n"] > 0 and count_detail:
            detail = count_detail.format(n=state["n"]) + f" · {int(elapsed)}s"
        # 生成尾巴：正式内容开始输出后切内容尾，否则思考链尾（单行，浮卡"在输出"证据）
        tail = state["content_tail"] or state["reasoning_tail"]
        live = _live_tail(tail) if tail else None
        pct = _climb_pct(elapsed, percent, p_to, climb_seconds) if p_to is not None else percent
        on_progress(
            stage, pct, detail,
            phase=phase, step=step, step_index=step_index, step_total=step_total,
            busy=True, check_cancel=False, live=live,
        )

    async def _beat() -> None:
        while not stop.is_set():
            await asyncio.sleep(interval)
            _report(time.monotonic() - _t0)

    use_stream = stream and hasattr(provider, "chat_stream")
    beat = asyncio.create_task(_beat())
    try:
        if use_stream:
            parts: list[str] = []
            pushed_src = ""  # "" | reasoning | content（来源切换立即推帧）
            last_push_t = 0.0
            try:
                async for chunk in provider.chat_stream(messages, tools=tools, ctx=ctx):
                    if chunk.reasoning:
                        state["reasoning_tail"] = (state["reasoning_tail"] + chunk.reasoning)[-400:]
                    if chunk.delta:
                        parts.append(chunk.delta)
                        state["content_tail"] = (state["content_tail"] + chunk.delta)[-400:]
                    elif chunk.content:
                        parts.append(chunk.content)
                        state["content_tail"] = (state["content_tail"] + chunk.content)[-400:]
                        break  # 非流式回退 chunk（mock 等）：content 即全文
                    if count_detail:
                        n = _count_json_items("".join(parts))
                        if n > 0 and n != state["n"]:
                            state["n"] = n
                            _report(time.monotonic() - _t0)
                    # 尾巴推帧：来源切换（思考→正文）立即推 + 0.5s 节流（chunk 级别太密，手机渲染扛不住）
                    tail_now = state["content_tail"] or state["reasoning_tail"]
                    src = "content" if state["content_tail"] else "reasoning"
                    now = time.monotonic()
                    if tail_now and (src != pushed_src or now - last_push_t >= 0.5):
                        pushed_src = src
                        last_push_t = now
                        _report(now - _t0)
            except TypeError:
                # provider 不支持流式签名 → 回退非流式
                resp = await provider.chat(messages, tools=tools, ctx=ctx)
                logger.info("[kb.llm] conn=%s 调用完成 stage=%s step=%s（非流式回退）耗时 %.1fs",
                            conn_id, stage, step, time.monotonic() - _t0)
                return resp
            logger.info("[kb.llm] conn=%s 调用完成 stage=%s step=%s 耗时 %.1fs（流式 %d 字）",
                        conn_id, stage, step, time.monotonic() - _t0, len("".join(parts)))
            return gw.ChatResponse(content="".join(parts))
        resp = await provider.chat(messages, tools=tools, ctx=ctx)
        logger.info("[kb.llm] conn=%s 调用完成 stage=%s step=%s 耗时 %.1fs",
                    conn_id, stage, step, time.monotonic() - _t0)
        return resp
    finally:
        stop.set()
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass


# ---------- 精简 DDL 画像（阶段二/三统一上下文：DDL 结构 + 描述 + 取值示例 + FK 内联） ----------

_TRUE_LONG_TYPES = ("CLOB", "BLOB", "LONGTEXT", "MEDIUMTEXT", "BYTEA", "JSON")

# TEXT 型 JSON/大文本/二进制列：type 不带 JSON（如 TEXT），靠列名后缀识别；
# LONGTEXT/BLOB/JSON 类型列已由 _TRUE_LONG_TYPES 类型层兜住。
_PORTRAIT_BLOB_NAMES = ("_json", "_jsonb", "_blob", "_clob", "_longtext", "_long_text",
                        "long_text", "jsondata", "payload")


def _portrait_noise(name: str, ctype: str) -> bool:
    """画像噪音列：列名级语义噪音（时间戳/审计/软删）+ JSON/大文本/二进制列 + 真长文本类型。
    绝不用类型前缀宽判——`status TEXT` 是业务枚举，不是长文本。"""
    if is_noise_column(name):
        return True
    n = (name or "").lower()
    if any(s in n for s in _PORTRAIT_BLOB_NAMES):
        return True
    return (ctype or "").upper().startswith(_TRUE_LONG_TYPES)


def _portrait_clean_type(t: str) -> str:
    """类型去长度/精度噪声：NUMERIC(10,2)→NUMERIC；VARCHAR(255)→VARCHAR。"""
    u = (t or "TEXT").upper()
    if any(u.startswith(k) for k in ("VARCHAR", "CHAR", "INT", "DECIMAL", "NUMERIC",
                                     "FLOAT", "DOUBLE", "REAL", "BIGINT", "SMALLINT", "TINYINT")):
        u = re.sub(r"\s*\(.*?\)\s*$", "", u)
    return u


def _single_table_ddl_portrait(
    schema: dict[str, Any], table: str,
    table_desc: str, col_desc: dict[str, str],
    samples: dict[str, Any] | None,
) -> str:
    """单表精简 DDL 画像：表描述头 + CREATE TABLE（类型去噪、噪音列滤除、FK 内联 [FK->]）。

    - 描述回填：表级库注释（无则阶段一表描述）+ 列级库注释/AI 注释；
    - 取值示例：有授权样本才附加（低基数离散列给取值集，高基数给单值，FK 列不给）；
    - FK 内联：有 FK 才出现 [FK->表.列]，无 FK 自然不写（LLM 为主，FK 可选增强）。
    """
    cols = [c for c in schema.get("columns", []) if c["table"] == table]
    fks = [f for f in schema.get("foreign_keys", []) if f["table"] == table]
    fk_refs: dict[str, list[str]] = {}
    for fk in fks:
        fk_refs.setdefault(fk["column"], []).append(f"{fk['ref_table']}.{fk['ref_column']}")

    lines = [f"CREATE TABLE {table} ("]
    col_lines = []
    for c in cols:
        name = c["name"]
        is_pk = bool(c.get("pk"))
        if not is_pk and not fk_refs.get(name) and _portrait_noise(name, c.get("type", "")):
            continue
        t = _portrait_clean_type(c.get("type", ""))
        body = f"  {name:<24} {t:<10}"
        if is_pk:
            body += " PRIMARY KEY"
        if fk_refs.get(name):
            body += f"  [FK->{';'.join(fk_refs[name])}]"
        # 描述 + 取值示例熔一句（画像只留"真枚举"：非主键/非高基数、值短≤16、上限5个）
        # id/主键/日期/数值等一次性或高基数列不给示例——对归类/判关系是噪音
        desc_extra = ""
        if samples is not None and not fk_refs.get(name):
            tu = (t or "").upper()
            is_high = is_pk or tu.startswith(
                ("INT", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE", "DATE", "TIME")
            )
            if not is_high:
                short_vals = [
                    str(v)[:16] for v in _distinct_values(samples, name)
                    if len(str(v)) <= 16
                ][:5]
                if len(short_vals) >= 2:
                    desc_extra = "取值示例：" + "，".join(short_vals)
        parts = []
        if col_desc.get(name):
            parts.append("描述：" + col_desc[name])
        if desc_extra:
            parts.append(desc_extra)
        if parts:
            body += "  -- " + "。".join(parts)
        col_lines.append(body)
    for fk in fks:
        col_lines.append(
            f"  CONSTRAINT fk_{table}_{fk['column']} FOREIGN KEY ({fk['column']}) "
            f"REFERENCES {fk['ref_table']}({fk['ref_column']})"
        )
    lines.append(",\n".join(col_lines))
    lines.append(");")
    ddl = "\n".join(lines)
    return f"-- {table}：{table_desc}\n{ddl}" if table_desc else ddl


def _knowledge_descs(
    state: "AppState", conn_id: str, schema: dict[str, Any],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """从知识库读已产出的阶段一/库注释，供画像回填：
    返回 (表描述表, 列描述表[表][列])。库注释优先，缺则 AI 注释。"""
    table_desc: dict[str, str] = {}
    col_desc: dict[str, dict[str, str]] = {}
    try:
        tabs = state.knowledge._tables.get(conn_id, {})
        for t in schema.get("tables", []):
            name = t["name"]
            tk = tabs.get(name)
            # 表级：库注释 > 当前 AI 注释 > 本轮提案（2026-09：提案即待确认内容，画像阶段消费）
            tdesc = t.get("comment", "") or (
                tk.comment if tk is not None and tk.status == "confirmed" else ""
            ) or (tk.proposed_comment if tk is not None else "")
            table_desc[name] = tdesc
            # 列级：库注释 > 当前 AI 注释 > 本轮提案
            cdesc: dict[str, str] = {}
            for c in schema.get("columns", []):
                if c["table"] != name:
                    continue
                cname = c["name"]
                dbc = c.get("comment", "")
                ci = tk.columns.get(cname) if tk is not None else None
                ai = (ci.comment if ci is not None and ci.status == "confirmed" else "")
                prop = (ci.proposed_comment or "") if ci is not None else ""
                cdesc[cname] = dbc or ai or prop
            col_desc[name] = cdesc
    except Exception:  # noqa: BLE001 - 读知识库失败不影响画像构造
        pass
    return table_desc, col_desc


def _build_ddl_portraits(
    state: "AppState", conn_id: str, schema: dict[str, Any],
    samples: dict[str, dict[str, Any]] | None = None,
) -> str:
    """全库精简 DDL 画像：每表一条 CREATE TABLE（FK/类型/取值内联）。
    仅供阶段三（图谱关系抽取）作上下文——它需要引用方向/主键/取值判断边。"""
    table_desc, col_desc = _knowledge_descs(state, conn_id, schema)
    parts = [
        _single_table_ddl_portrait(
            schema, t["name"], table_desc.get(t["name"], ""),
            col_desc.get(t["name"], {}),
            (samples or {}).get(t["name"]),
        )
        for t in schema.get("tables", [])
    ]
    return "\n\n".join(parts)


def _deterministic_candidates_block(schema: dict[str, Any], cap: int = 120) -> str:
    """代码已发现的关系候选块（喂给阶段三 prompt 做参考）。

    来源 = build_draft_edges（FK confidence 1.0 + 命名推断 0.6），纯函数零成本。
    表对级紧凑清单，去重；超出 cap 截断（FK 优先于 naming 排前）。
    LLM 职责引导：不必复述无争议候选，注意力放到候选之外（语义关联/多态/无命名规律）。
    """
    from app.knowledge.graph.builder import build_draft_edges  # noqa: PLC0415 - 延迟导入避免循环

    edges = build_draft_edges(schema)
    if not edges:
        return ""
    lines: list[str] = []
    for e in edges[:cap]:
        src = "FK" if e.get("source") == "fk" else "命名"
        lines.append(
            f"- {e['from_table']}.{e['from_col']} → {e['to_table']}.{e['to_col']}"
            f"（{src}，{e.get('cardinality', 'n:1')}）"
        )
    more = len(edges) - min(len(edges), cap)
    if more > 0:
        lines.append(f"- ……另有 {more} 条候选略")
    return "\n".join(lines)


# 阶段二领域概述：每表最多列出的主要业务列数（超出省略）
_DOMAIN_OVERVIEW_MAX_COLS = 8


def _build_domain_overview(
    state: "AppState", conn_id: str, schema: dict[str, Any],
) -> str:
    """阶段二领域概述：每表一行中文业务描述（表名：描述，主要列：…）。

    领域划分只关心"这张表是什么、跟谁业务相近"→ 一行高度浓缩即可，
    不喂完整 DDL（FK/类型/取值在此是噪音）。列取库注释/AI 注释优先，
    无注释回退英文列名，滤除时间戳/审计等噪音列，截前 _DOMAIN_OVERVIEW_MAX_COLS 个。
    """
    table_desc, col_desc = _knowledge_descs(state, conn_id, schema)
    lines: list[str] = []
    for t in schema.get("tables", []):
        name = t["name"]
        tdesc = table_desc.get(name) or t.get("comment") or ""
        cols: list[str] = []
        for c in schema.get("columns", []):
            if c["table"] != name:
                continue
            if _portrait_noise(c["name"], c.get("type", "")):
                continue
            cd = col_desc.get(name, {}).get(c["name"]) or c.get("comment") or c["name"]
            cols.append(cd)
        body = f"{name}：{tdesc}" if tdesc else name
        if cols:
            shown = "，".join(cols[:_DOMAIN_OVERVIEW_MAX_COLS])
            if len(cols) > _DOMAIN_OVERVIEW_MAX_COLS:
                shown += "，…"
            body += f"，主要列：{shown}"
        lines.append(body)
    return "\n".join(lines)


def _parse_domain_payload(text: str) -> tuple[bool, list[dict[str, Any]]]:
    """解析领域划分响应：JSON 数组 → (False, 领域条目)；其余 → (True, [])。

    条目 {name, description, tables[], reason}。表名校验/兜底在 _normalize_domains。
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return False, []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return False, []
    if isinstance(data, dict) and data.get("unchanged"):
        return True, []
    if not isinstance(data, list):
        return False, []
    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict) or not str(it.get("name") or "").strip():
            continue
        tbls = it.get("tables") or it.get("members") or []
        if isinstance(tbls, str):
            tbls = [tbls]
        if not isinstance(tbls, list):
            continue
        out.append({
            "name": str(it["name"]).strip(),
            "description": str(it.get("description") or "").strip(),
            "tables": [str(x).strip() for x in tbls if str(x).strip()],
            "reason": str(it.get("reason") or "").strip(),
            # 锚点模式：AI 提议的改名/合并映射（审核层展示"新名（原：旧名）"并批准）
            **({"renamed_from": str(it["renamed_from"]).strip()}
               if str(it.get("renamed_from") or "").strip() else {}),
            **({"merged_from": [str(x).strip() for x in it["merged_from"] if str(x).strip()]}
               if isinstance(it.get("merged_from"), list) else {}),
        })
    return False, out


def _domain_match_score(table: str, domain: dict[str, Any]) -> int:
    """孤表与领域的贴合度：域名/成员表名的词根重合越多越高。"""
    t = table.lower()
    dname = str(domain.get("name", "")).lower()
    score = 0
    if dname and (dname in t or t in dname):
        score += 2
    t_root = t.split("_")[0]
    for m in domain.get("tables", []):
        ml = str(m).lower()
        if ml and (t.startswith(ml) or ml.startswith(t) or ml.split("_")[0] == t_root):
            score += 1
    return score


def _normalize_domains(
    domains: list[dict[str, Any]], table_names: list[str],
) -> list[dict[str, Any]]:
    """校验 + 程序兜底：表名真实、域数上界、无孤表。

    兜底规则：域数 > 表数 → 留下成员最多的前 N 个，溢出表并入最大域；
    无任何域 → 兜底单「业务」域；孤表 → 挂到贴合度最高的域。
    """
    names = set(table_names)
    valid: list[dict[str, Any]] = []
    for d in domains:
        members = [t for t in d.get("tables", []) if t in names]
        if members:
            valid.append({**d, "tables": members})
    n_tab = len(names)
    # 域数越界程序兜底
    if len(valid) > n_tab:
        valid.sort(key=lambda d: len(d["tables"]), reverse=True)
        kept, rest = valid[:n_tab], valid[n_tab:]
        spill = [t for d in rest for t in d["tables"]]
        if kept and spill:
            kept[0]["tables"] = list(dict.fromkeys(kept[0]["tables"] + spill))
        valid = kept
    if not names:
        return valid
    if not valid:
        return [{"name": "业务", "description": "通用业务域（程序兜底）",
                 "tables": sorted(names), "reason": "程序兜底"}]
    # 孤表兜底：逐表挂到贴合度最高的域（排序保证确定性）
    assigned = {t for d in valid for t in d["tables"]}
    for t in sorted(names):
        if t in assigned:
            continue
        cand = max(valid, key=lambda d: _domain_match_score(t, d))
        cand.setdefault("tables", []).append(t)
    return valid


def _mock_domains(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """mock：按表名关键词确定性归域（一域多表，能体现多表复用标签）。"""
    doms: dict[str, dict[str, Any]] = {}
    for t in schema.get("tables", []):
        name = t["name"].lower()
        tag = next((zh for kw, zh in _MOCK_TAG_HINTS.items() if kw in name), "业务")
        d = doms.setdefault(tag, {"name": tag, "description": f"{tag}相关表",
                                  "tables": [], "reason": "mock 关键词归域"})
        d["tables"].append(t["name"])
    return list(doms.values())


def _domain_partition_prompt(
    portraits: str, lo: int, hi: int, n_tables: int,
    existing_tags: str = "", allow_rename: bool = False,
) -> str:
    from app.ai.prompts import render
    return render(
        "annotator_domain_partition",
        portraits=portraits, lo=str(lo), hi=str(hi), n_tables=str(n_tables),
        existing_tags=existing_tags or "", rename_rule=(
            "\n【改名/合并提议】\n"
            "若你判断某既有领域划分不合理，可提议改名或合并，条目中用 "
            "renamed_from / merged_from 字段标明原域名并说明理由；未提议的既有域请原样沿用其名称。\n"
        ) if allow_rename else "",
    )


def _incremental_tag_absorb_prompt(
    targets_portraits: str, existing_tags: str,
) -> str:
    """增量标签吸收（D1 收紧）：把新增表归入已有 confirmed 域，或提议新域（新域 draft）。

    关键约束（prompt 内明示）：
    - 只能决定【新表】挂到哪些域；不得改动已有域的成员表；
    - 输出 JSON：每表挂到最多领域 → [{table, tags[], reason}]；
      命名：命中候选域用其名；无合适候选 → 提议新域名（一个或多个，自动成为 draft 候选）。
    """
    from app.ai.prompts import render
    return render("annotator_domain_incremental", existing_tags=existing_tags or "（暂无）", targets_portraits=targets_portraits)


async def annotate_domain(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any] | None = None,
    mode: str = "full",                  # full=全量空库划分；incremental=增量吸收新表（D1 收紧）
    target_tables: list[str] | None = None,  # incremental 模式：只处理这些表（新增/变化表）
    on_progress: Any | None = None,    # 构建进度（阶段二子步：划分）
    limiter: "_AdaptiveLimiter | None" = None,  # 共享全局限流（构建期各阶段共用一个实例）
    existing_tags: str | None = None,   # 既有 confirmed 域清单文本（锚点：参考而非服从）
    allow_rename: bool = False,         # 锚点模式下允许 AI 提议改名/合并（renamed_from/merged_from）
) -> dict[str, Any]:
    """AI 领域划分（KC2 v2）：单轮画像打包划分（深度思考），写入知识库。

    产物：领域列表 [{name, 描述, 成员表[], 依据}] → 落库为标签草案 + 表→域多打标。
    段6：不再写 desc_drafts（表描述由阶段一列注释 + 库注释承担，领域调用只产出标签）。
    existing_tags/allow_rename：标签锚点（tag_mode=keep/anchor）——把既有 confirmed 域
    作为参考注入 prompt，抑制跨轮命名漂移；allow_rename 时可提议改名/合并并在审核层批准。
    """
    if mode == "incremental":
        return await _annotate_domain_incremental(
            state, conn_id, schema, target_tables or [], on_progress,
        )
    if schema is None:
        schema = await get_schema(state, conn_id)
    table_names = [t["name"] for t in schema.get("tables", [])]
    if not table_names:
        return {"tables": 0, "domains": 0, "new_tags": 0,
                "library_size": len(state.knowledge.tags(conn_id)["library"])}
    # 阶段二领域概述（每表一行中文描述）：划分只看"表是什么/跟谁相近"，不喂完整 DDL
    overview = _build_domain_overview(state, conn_id, schema)
    n = len(table_names)
    target = max(3, round(math.sqrt(n)))
    lo, hi = max(2, target - 1), target + 1

    rt = state.runtime.get()
    logger.info("[kb.tags] conn=%s 领域划分：tables=%s target=%s~%s anchor=%s",
                conn_id, n, lo, hi, bool(existing_tags))
    _tag_cfg = _kb_reason_provider_cfg(rt, reasoning=True)
    _tag_cfg["reasoning"] = "high"  # 单轮深度思考
    new_tags = 0

    if gw.is_effective_mock(_tag_cfg):
        logger.debug("[kb.tags] conn=%s mock 领域划分", conn_id)
        if on_progress:
            on_progress("AI 标签提取", 0, "领域划分", phase="tags",
                        step="partition", step_index=1, step_total=1)
        domains = _normalize_domains(_mock_domains(schema), table_names)
    else:
        provider = gw.build_provider(_tag_cfg)
        # 第一轮：打包划分（N 条画像一次调用）
        _t_part = time.monotonic()
        if on_progress:
            on_progress("AI 标签提取", 0, "领域划分", phase="tags",
                        step="partition", step_index=1, step_total=1)
        resp = await _chat_with_beat(
            provider,
            [{"role": "user", "content": _domain_partition_prompt(
                overview, lo, hi, n,
                existing_tags=existing_tags or "", allow_rename=allow_rename)}],
            None,
            ctx={
                "conn_id": conn_id, "connection": conn_id, "skill": "kb-tags",
                "source": "kb_build", "status": "egress-tags",
                "context_meta": {"candidate_tables": table_names},
            },
            on_progress=on_progress, stage="AI 标签提取", phase="tags",
            step="partition", step_index=1, step_total=1, percent=0,
            p_to=49,
            limiter=limiter,
            climb_seconds=_KB_REASONING_TIMEOUT * 0.8,
            stream=True, count_detail="已划分出 {n} 个领域",
        )
        _unchanged, raw_domains = _parse_domain_payload(resp.content or "")
        logger.info("[kb.tags] conn=%s 划分耗时 %.1fs（raw domains=%s）",
                    conn_id, time.monotonic() - _t_part, len(raw_domains))
        if not raw_domains:
            logger.warning("[kb.tags] conn=%s LLM 划分返回解析为空（兜底单「业务」域）", conn_id)
        # 兜底空输入 → 单「业务」域
        domains = _normalize_domains(raw_domains, table_names)
    # 阶段二完成：tags 条打满（窗口 45→60），真实收尾由 graph 条尾段承接（见 store.build）
    if on_progress:
        on_progress("AI 标签提取", 100, None, phase="tags")
    logger.info("[kb.tags] conn=%s 领域落库完成：domains=%s new_tags=%s",
                conn_id, len(domains), new_tags)
    return {
        "tables": len(table_names),
        "domains": len(domains),
        "new_tags": new_tags,
        "library_size": len(state.knowledge.tags(conn_id)["library"]),
        # 标签全集（审核页"新版标签集合"数据源；mock/全量划分路径均产出；版本制不落库）
        "domain_list": [{"name": d.get("name", ""), "description": d.get("description", ""),
                         "tables": list(d.get("tables") or []),
                         **({"renamed_from": d["renamed_from"]} if d.get("renamed_from") else {}),
                         **({"merged_from": list(d["merged_from"])} if d.get("merged_from") else {})}
                        for d in domains],
    }


async def _annotate_domain_incremental(
    state: "AppState", conn_id: str,
    schema: dict[str, Any], target_tables: list[str],
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """增量标签吸收（D1 收紧）：把新增表归入既有 confirmed 域，或提议新域（draft）。

    - 只处理 target_tables（新增/变化表）；不改已绑定表、不清标签库；
    - 候选域 = 既有 confirmed 标签（名称+描述）；
    - 新域名自动 upsert 为 draft（待人工确认）。
    """
    targets = [t for t in target_tables if t]
    if not targets or not schema.get("tables"):
        return {"tables": 0, "domains": 0, "new_tags": 0, "library_size": 0}
    # 只保留 target 表的画像（局部上下文，不重扫全库）
    sub_schema = dict(schema)
    sub_schema["tables"] = [t for t in schema["tables"] if t["name"] in targets]
    sub_schema["columns"] = [c for c in schema["columns"] if c["table"] in targets]
    sub_schema["foreign_keys"] = [
        f for f in schema["foreign_keys"] if f["table"] in targets or f["ref_table"] in targets
    ]
    # 增量吸收也走每表一行概述（只判断新表归哪个既有域，无需完整 DDL）
    overview = _build_domain_overview(state, conn_id, sub_schema)

    lib = {t["name"]: t for t in state.knowledge.tags(conn_id)["library"]}
    confirmed = {n: v for n, v in lib.items() if v.get("status") == "confirmed"}
    existing_desc = "\n".join(
        f"- {nm}（{confirmed[nm].get('description','')}）" for nm in confirmed
    ) or "（暂无）"

    rt = state.runtime.get()
    _tag_cfg = _kb_reason_provider_cfg(rt, reasoning=False)  # 阶段2 归类任务：关思考，秒级
    links: list[dict[str, Any]] = []
    if on_progress:
        on_progress("AI 标签提取", 0, "增量吸收", phase="tags",
                    step="absorb", step_index=1, step_total=1)

    if gw.is_effective_mock(_tag_cfg):
        # mock：无 LLM，程序兜底——按表名关键词命中候选域，否则归 default
        for t in targets:
            name = t.lower()
            hint = next((zh for kw, zh in _MOCK_TAG_HINTS.items() if kw in name), None)
            tags = ([hint] if hint and hint in confirmed else [])
            if not tags:
                tags = ["通用"] if "通用" in confirmed else [hint or "新增表"]
            links.append({"table": t, "tags": tags, "reason": "mock 关键词吸收"})
    else:
        provider = gw.build_provider(_tag_cfg)
        resp = await provider.chat(
            [{"role": "user", "content": _incremental_tag_absorb_prompt(overview, existing_desc)}],
            tools=None,
            ctx={
                "conn_id": conn_id, "connection": conn_id, "skill": "kb-tags",
                "source": "kb_sync", "status": "egress-tags-absorb",
                "context_meta": {"candidate_tables": targets},
            },
        )
        try:
            data = json.loads((resp.content or "").strip().lstrip("`").rstrip("`"))
        except Exception:
            data = []
        for item in (data or []) if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("table") in targets:
                tags = [x for x in (item.get("tags") or []) if isinstance(x, str) and x.strip()]
                if tags:
                    links.append({"table": item["table"], "tags": tags, "reason": item.get("reason", "")})

    # 落库：只 upsert 新域名（draft）+ 只 assign target 表
    new_tags: list[str] = []
    for ln in links:
        for tg in ln["tags"]:
            if tg not in lib and tg not in new_tags:
                new_tags.append(tg)
    tag_props = [{"name": n, "description": f"{n} 领域"} for n in new_tags]
    state.knowledge.upsert_tags(conn_id, tag_props)
    for ln in links:
        state.knowledge.assign_table_tags(conn_id, ln["table"], ln["tags"], merge=True)

    logger.info("[kb.tags] conn=%s 增量吸收完成：tables=%s existing=%s new=%s",
                conn_id, len(targets), len(confirmed), len(new_tags))
    return {
        "tables": len(targets),
        "domains": len({tg for ln in links for tg in ln["tags"]}),
        "new_tags": len(new_tags),
        "library_size": len(state.knowledge.tags(conn_id)["library"]),
    }


# ---------- LLM 图谱识别（两轮：全局扫描 + 候选验证） ----------


def _parse_graph_edges(text: str, schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """解析 LLM 返回的关系 JSON 数组（边 v2：四元组 + cardinality + reason）。

    校验规则：
    - 缺 from_col/to_col/cardinality（四元组/基数不齐）→ 丢弃；
    - schema 提供时：字段必须真实存在，自环丢弃；
    - 多侧校验：from_col 为 from_table 主键却声明 n:1 → 矛盾丢弃
      （主键列不可能为多侧；1:1 声明保留）。
    """
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        data = json.loads(t)
    except Exception:
        m = re.search(r"\[.*\]", t, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    cols = {
        (c["table"], c["name"]): bool(c.get("pk"))
        for c in (schema or {}).get("columns", [])
    }
    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        from_t = str(it.get("from_table") or it.get("from") or "").strip()
        to_t = str(it.get("to_table") or it.get("to") or "").strip()
        from_c = str(it.get("from_col") or "").strip()
        to_c = str(it.get("to_col") or "").strip()
        cardinality = str(it.get("cardinality") or "").strip()
        if not from_t or not to_t or not from_c or not to_c:
            continue  # 缺四元组 → 丢弃
        if cardinality not in ("n:1", "1:1"):
            continue  # 缺/错基数 → 丢弃
        if from_t == to_t and from_c == to_c:
            continue  # 自环 → 丢弃
        if schema is not None:
            if (from_t, from_c) not in cols or (to_t, to_c) not in cols:
                continue  # 字段不存在 → 丢弃
            if cardinality == "n:1" and cols[(from_t, from_c)]:
                continue  # from 为主键却 n:1 → 矛盾丢弃（主键不可能为多侧）
        out.append({
            "from_table": from_t, "from_col": from_c,
            "to_table": to_t, "to_col": to_c,
            "cardinality": cardinality,
            "reason": str(it.get("reason") or ""),
            "confidence": str(it.get("confidence") or ""),
            "status": str(it.get("status") or ""),
            "guard": str(it.get("guard") or "").strip() or None,  # S2-4：守卫谓词（多态关联）
        })
    return out


def _mock_graph_edges(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """mock：基于外键生成确定性图谱边（边 v2：FK 方向 + 基数推导）。"""
    edges: list[dict[str, Any]] = []
    col_pk = {(c["table"], c["name"]): bool(c.get("pk")) for c in schema.get("columns", [])}
    for fk in schema.get("foreign_keys", []):
        edges.append({
            "from_table": fk["table"], "from_col": fk["column"],
            "to_table": fk["ref_table"], "to_col": fk["ref_column"],
            "cardinality": "1:1" if col_pk.get((fk["table"], fk["column"])) else "n:1",
            "reason": f"FK 约束：{fk['table']}.{fk['column']} → {fk['ref_table']}.{fk['ref_column']}",
        })
    return edges


async def annotate_graph(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any],
    mode: str = "full",                    # full=全库扫描；incremental=局部补边（D3）
    target_tables: list[str] | None = None,  # incremental：只补这些表关联的边
    on_progress: Any | None = None,
    limiter: "_AdaptiveLimiter | None" = None,  # 共享全局限流（构建期各阶段共用一个实例）
) -> list[dict[str, Any]]:
    """LLM 图谱识别：单轮全局扫描（深度思考），返回 draft 边列表。

    全局扫描上下文 = 精简 DDL 画像（_build_ddl_portraits，描述回填 + FK 内联）。
    schema: 完整 schema，用于生成候选对与画像。
    on_progress: 阶段三「AI 关系识别」进度回调（全局扫描 0→100）。
    即低置信边就地审校（confirmed/rejected/修正字段），不再重跑全局生成；关时池内只含程序候选。
    返回值未落库，由调用方统一写入图谱 draft。

    mode="incremental"（增量局部补边，D3）：只对 target_tables 及其 FK 邻表做一次
    局部全局扫描，产出涉及 target_tables 的 draft 边；不触碰其它表的 LLM 边。
    """
    if mode == "incremental":
        return await _annotate_graph_incremental(
            state, conn_id, schema, target_tables or [], on_progress,
        )
    rt = state.runtime.get()
    # 阶段3（图谱）：单轮深度思考
    provider_cfg = _kb_reason_provider_cfg(rt)
    provider_cfg["reasoning"] = "high"
    logger.info(
        "[kb.graph] conn=%s 开始关系识别：tables=%s fks=%s",
        conn_id, len(schema.get("tables", [])), len(schema.get("foreign_keys", [])),
    )
    if gw.is_effective_mock(provider_cfg):
        logger.debug("[kb.graph] conn=%s mock FK 边", conn_id)
        if on_progress:
            on_progress("AI 关系识别", 74, None, phase="graph",
                        step="global", step_index=1, step_total=1)
        return _mock_graph_edges(schema)

    provider = gw.build_provider(provider_cfg)
    _t_g = time.monotonic()  # 全局扫描耗时打点

    # 上下文：精简 DDL 画像（描述回填 + 取值示例 + FK 内联），与阶段二统一
    _samples = state.knowledge._samples.get(conn_id, {}) if state.knowledge._samples.get(conn_id) else {}
    graph_overview = _build_ddl_portraits(state, conn_id, schema, _samples)
    # 确定性候选参考（FK+命名）：引导 LLM 注意力放到候选之外，不逐条复述已知事实
    candidates = _deterministic_candidates_block(schema)
    candidates_block = f"\n【代码已发现的候选（确定性规则产出）】\n{candidates}\n" if candidates else ""

    # ---- 第一轮：全局扫描（广度优先） ----
    from app.ai.prompts import render
    global_prompt = render("annotator_relation_global", graph_overview=graph_overview,
                           candidates_block=candidates_block)
    _graph_ctx = {
        "conn_id": conn_id, "connection": conn_id, "skill": "kb-graph",
        "source": "kb_build", "status": "egress-graph-global",
        "context_meta": {"candidate_tables": [t["name"] for t in schema.get("tables", [])]},
    }
    async def _global_scan(provider_: Any) -> Any:
        return await _chat_with_beat(
            provider_,
            [{"role": "user", "content": global_prompt}],
            None,
            ctx=_graph_ctx,
            on_progress=on_progress, stage="AI 关系识别", phase="graph",
            step="global", step_index=1, step_total=1, percent=0,
            p_to=49,
            limiter=limiter,
            climb_seconds=_KB_REASONING_TIMEOUT * 0.8,
            stream=True, count_detail="已识别 {n} 条关系",
        )

    global_resp = None
    try:
        global_resp = await _global_scan(provider)
    except httpx.ReadTimeout:
        # 推理模型超时 → 降级关思考重试一次（图谱关系识别结构化强，普通生成兜底够用；
        # FK+命名确定性边已在 builder 产出，LLM 只是补充非 FK 关系）
        # 流式下 ReadTimeout 语义 = 连续 480s 无任何字节（卡顿），非总时长
        logger.warning("[kb.graph] conn=%s 全局扫描推理超时，降级关思考重试", conn_id)
        if on_progress:
            on_progress("AI 关系识别", 5, "推理超时·降级普通生成", phase="graph",
                        step="global", step_index=1, step_total=1)
        provider = gw.build_provider(_kb_reason_provider_cfg(rt, reasoning=False))
        global_resp = await _global_scan(provider)

    global_edges = _parse_graph_edges(global_resp.content or "", schema)
    if not global_edges:
        # 修复（2026-09）：解析为空（空内容/散文/坏 JSON）此前只告警即放弃——
        # 现复用超时同款"降级关思考重试"兜底路径，仍空才认输（确定性通道兜底）
        logger.warning("[kb.graph] conn=%s LLM 返回解析为空（关系识别·全局扫描），降级关思考重试", conn_id)
        if on_progress:
            on_progress("AI 关系识别", 5, "解析为空·降级普通生成重试", phase="graph",
                        step="global", step_index=1, step_total=1)
        retry_resp = await _global_scan(gw.build_provider(_kb_reason_provider_cfg(rt, reasoning=False)))
        global_edges = _parse_graph_edges(retry_resp.content or "", schema)
        if not global_edges:
            logger.warning("[kb.graph] conn=%s 降级重试后仍解析为空（关系识别·全局扫描）", conn_id)
    logger.debug("[kb.graph] conn=%s 全局扫描边数=%s", conn_id, len(global_edges))
    logger.info("[kb.graph] conn=%s 全局扫描耗时 %.1fs（LLM 边 %s）",
                conn_id, time.monotonic() - _t_g, len(global_edges))
    # 标记来源
    for e in global_edges:
        e["source"] = "llm_global"
    if on_progress:
        on_progress("AI 关系识别", 50, None, phase="graph",
                    step="global", step_index=1, step_total=1)

    # 单轮直达：global 全量边直用（原合并逻辑见上方注释）
    all_edges = list(global_edges)

    # 最终去重（同一四元组只留一条；方向键视作同一对）
    seen_final: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for e in all_edges:
        key = (e["from_table"], e.get("from_col"), e["to_table"], e.get("to_col"))
        rev = (e["to_table"], e.get("to_col"), e["from_table"], e.get("from_col"))
        if key in seen_final or rev in seen_final:
            continue
        seen_final.add(key)
        deduped.append(e)

    logger.info(
        "[kb.graph] conn=%s 关系识别完成：llm_edges=%s total=%s",
        conn_id, len(global_edges), len(deduped),
    )
    return deduped


async def _annotate_graph_incremental(
    state: "AppState", conn_id: str,
    schema: dict[str, Any], target_tables: list[str],
    on_progress: Any | None = None,
) -> list[dict[str, Any]]:
    """增量局部补边（D3）：只对目标表及其 FK 邻表做一次局部全局扫描，产出涉及
    target_tables 的 draft 边。不触碰其它表的 LLM 边（store 层负责替换/保留）。

    局部子图 = 目标表 ∪ 与目标表有 FK 关系的已有表（作为邻居上下文）。
    单轮、高/低置信都返回（store 层校验墓碑后并入 draft）。
    """
    targets = [t for t in target_tables if t]
    if not targets:
        return []
    # 目标表 + FK 邻表（子图）
    fk_adj: dict[str, set[str]] = {}
    for f in schema.get("foreign_keys", []):
        fk_adj.setdefault(f["table"], set()).add(f["ref_table"])
        fk_adj.setdefault(f["ref_table"], set()).add(f["table"])
    subgraph = set(targets)
    for t in targets:
        subgraph |= fk_adj.get(t, set())
    sub_tables = sorted(subgraph)
    sub_schema = dict(schema)
    sub_schema["tables"] = [t for t in schema["tables"] if t["name"] in subgraph]
    sub_schema["columns"] = [c for c in schema["columns"] if c["table"] in subgraph]
    sub_schema["foreign_keys"] = [
        f for f in schema["foreign_keys"] if f["table"] in subgraph or f["ref_table"] in subgraph
    ]

    _samples = state.knowledge._samples.get(conn_id, {}) if state.knowledge._samples.get(conn_id) else {}
    overview = _build_ddl_portraits(state, conn_id, sub_schema, _samples)
    rt = state.runtime.get()
    provider_cfg = _kb_reason_provider_cfg(rt)
    if gw.is_effective_mock(provider_cfg):
        if on_progress:
            on_progress("AI 关系识别", 74, None, phase="graph",
                        step="incr_global", step_index=1, step_total=1)
        return _mock_graph_edges(sub_schema)  # FK 元数据边（mock 走 FK）

    provider = gw.build_provider(provider_cfg)
    if on_progress:
        on_progress("AI 关系识别", 0, None, phase="graph",
                    step="incr_global", step_index=1, step_total=1)
    from app.ai.prompts import render
    # 候选参考只保留涉及目标表的（增量颗粒 = 目标表关联）
    sub_candidates = [
        ln for ln in _deterministic_candidates_block(sub_schema).splitlines()
        if any(t in ln for t in targets)
    ]
    candidates_block = f"\n【代码已发现的候选（确定性规则产出）】\n" + "\n".join(sub_candidates) + "\n" if sub_candidates else ""
    prompt = render("annotator_relation_incremental", targets=", ".join(targets),
                    overview=overview, candidates_block=candidates_block)
    resp = await provider.chat(
        [{"role": "user", "content": prompt}], tools=None,
        ctx={
            "conn_id": conn_id, "connection": conn_id, "skill": "kb-graph",
            "source": "kb_sync", "status": "egress-graph-incr",
            "context_meta": {"candidate_tables": sub_tables},
        },
    )
    edges = _parse_graph_edges(resp.content or "", sub_schema)
    # 只保留涉及目标表的边（增量颗粒 = 目标表关联）
    edges = [
        e for e in edges
        if e["from_table"] in targets or e["to_table"] in targets
    ]
    for e in edges:
        e["source"] = "llm_incr"
    if on_progress:
        on_progress("AI 关系识别", 74, None, phase="graph",
                    step="incr_global", step_index=1, step_total=1)
    logger.info("[kb.graph] conn=%s 增量局部补边完成：subgraph=%s 目标边=%s",
                conn_id, len(sub_tables), len(edges))
    return edges


# ---------------------------------------------------------------------------
# S2-1：表级过滤器 AI 语义确认（启发式只做预标记，AI 裁决为权威知识）
# ---------------------------------------------------------------------------

_FILTER_VERDICTS = ("tenant", "soft_delete", "exempt", "none")


def _filters_candidates_prompt(schema: dict[str, Any], candidates: list[dict[str, Any]],
                               samples: dict[str, dict[str, list[Any]]]) -> str:
    """AI 过滤器裁决提示词（柔和措辞：很可能/大概率/一般，供 LLM 生成 SQL 时参考，
    不是强制规则——用户明确说明特殊情况时以用户为准）。"""
    lines = []
    for t in schema.get("tables", []):
        name = t["name"]
        lines.append(f"- {name}")
        for c in schema.get("columns", []):
            if c["table"] != name:
                continue
            mark = ""
            for cand in candidates:
                if cand["table"] == name and cand["column"] == c["name"]:
                    mark = f"  [预标记:{cand['hint']}]"
                    break
            vals = (samples.get(name) or {}).get(c["name"]) or []
            sample_txt = f"  样例: {vals[:5]}" if vals else ""
            lines.append(f"    {c['name']} ({c.get('type','')}){mark}{sample_txt}")
    tables_and_columns = "\n".join(lines)
    from app.ai.prompts import render
    return render("annotator_filters", tables_and_columns=tables_and_columns)


def _normalize_filter_verdicts(text: str, table_names: list[str]) -> list[dict[str, Any]]:
    """LLM 裁决 JSON → 规范化列表（非法/缺字段丢弃）。"""
    out: list[dict[str, Any]] = []
    try:
        data = json.loads(text)
    except Exception:
        return out
    for it in data.get("filters") or []:
        if not isinstance(it, dict):
            continue
        t = str(it.get("table") or "")
        vd = str(it.get("verdict") or "")
        if t not in table_names or vd not in _FILTER_VERDICTS:
            continue
        if vd in ("tenant", "soft_delete"):
            col = str(it.get("column") or "")
            pred = str(it.get("predicate") or "")
            conf = str(it.get("confidence") or "low")
            if not col or not pred:
                continue
            if conf not in ("high", "medium", "low"):
                conf = "low"
            out.append({"table": t, "verdict": vd, "column": col,
                        "predicate": pred, "confidence": conf,
                        "reason": str(it.get("reason") or "")[:120]})
        else:
            out.append({"table": t, "verdict": vd,
                        "confidence": str(it.get("confidence") or "low"),
                        "reason": str(it.get("reason") or "")[:120]})
    return out


def _mock_filter_verdicts(schema: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """mock 裁决：启发式预标记直接转裁决（演示/离线路径，确定性）。"""
    out: list[dict[str, Any]] = []
    for cand in candidates:
        if cand["hint"] == "soft_delete":
            out.append({"table": cand["table"], "verdict": "soft_delete",
                        "column": cand["column"], "predicate": cand["predicate"],
                        "confidence": "high", "reason": "启发式命中软删除列（mock）"})
        elif cand["hint"] == "tenant":
            out.append({"table": cand["table"], "verdict": "tenant",
                        "column": cand["column"], "predicate": cand["predicate"],
                        "confidence": "medium", "reason": "启发式命中租户列（mock）"})
    return out


def _soft_delete_values_ok(samples: dict[str, dict[str, list[Any]]],
                           table: str, column: str) -> bool:
    """值域校验：采样值全在 {0,1,None} 才算软删除（防误判普通列）。"""
    vals = (samples.get(table) or {}).get(column)
    if not vals:
        return False  # 无采样 → 不自动确认（保守）
    for v in vals:
        if v is None:
            continue
        try:
            if int(v) not in (0, 1):
                return False
        except Exception:
            return False
    return True


async def annotate_filters(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any],
    on_progress: Any | None = None,
    limiter: "_AdaptiveLimiter | None" = None,
) -> dict[str, Any]:
    """表级过滤器 AI 语义确认（S2-1）：逐表裁决租户/软删除/豁免。

    - 启发式检测（detect_candidates）只作预标记提示，AI 看全部表/列自行裁决（特例适配）
    - soft_delete（值域 0/1/NULL + high 置信）→ 自动 confirmed（低风险）
    - tenant / exempt → draft（ai_suggested）→ 人工确认（误判代价高）
    - 失败静默：异常不影响构建（filter 是参考知识，非构建关键路径）；
      AI 异常时启发式不再转正（零写入，待人工），仅 mock 路径产确定性裁决
    """
    from app.knowledge.filters import FilterStore

    kb = state.knowledge
    store: FilterStore = kb.filter_store
    table_names = [t["name"] for t in schema.get("tables", [])]
    if not table_names:
        return {"filtered": 0, "auto_confirmed": 0, "draft": 0}
    samples = kb.semantic_store._samples.get(conn_id, {})
    candidates = kb.build_service._detect_filter_candidates(schema)

    rt = state.runtime.get()
    cfg = _kb_reason_provider_cfg(rt, reasoning=False)
    if gw.is_effective_mock(cfg):
        verdicts = _mock_filter_verdicts(schema, candidates)
    else:
        try:
            provider = gw.build_provider(cfg)
            prompt = _filters_candidates_prompt(schema, candidates, samples)
            resp = await _chat_with_beat(
                provider,
                [{"role": "user", "content": prompt}],
                None,
                ctx={"conn_id": conn_id, "connection": conn_id, "skill": "kb-filters",
                     "source": "kb_build", "status": "egress-filters",
                     "context_meta": {"candidate_tables": table_names}},
                on_progress=on_progress, stage="filters", phase="filters",
                step="adjudicate", step_index=1, step_total=1, percent=0,
                limiter=limiter,
            )
            text = resp.content if hasattr(resp, "content") else str(resp)
            verdicts = _normalize_filter_verdicts(text or "", table_names)
        except Exception as e:
            # 兜底收紧（先审后动）：AI 裁决失败时启发式预标记不再转正为 verdict，
            # 保持零写入待人工处理（用户可在过滤器审查页手动确认/跳过）。
            logger.warning("[kb.filters] conn=%s AI 裁决异常，本轮跳过（启发式仅预标记不落盘）：%s", conn_id, e)
            return {"filtered": 0, "auto_confirmed": 0, "draft": 0, "error": str(e) or type(e).__name__}

    # FilterStore 一表一条：先按表聚合（软删+租户 AND 合并；存在 tenant 则整表 draft，
    # 只有 soft_delete 且值域/置信通过才自动 confirmed）
    by_table: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        t = v["table"]
        vd = v["verdict"]
        if vd == "exempt":
            by_table.setdefault(t, {"preds": [], "auto": True, "exempt": True})
            continue
        entry = by_table.setdefault(t, {"preds": [], "auto": True, "exempt": False})
        entry["preds"].append(v["predicate"])
        if vd == "tenant":
            entry["auto"] = False
        elif vd == "soft_delete":
            if not (v.get("confidence") == "high"
                    and _soft_delete_values_ok(samples, t, v.get("column", ""))):
                entry["auto"] = False
    auto_confirmed = 0
    draft = 0
    for t, entry in by_table.items():
        if entry.get("exempt"):
            store.add(conn_id, t, "", scope="exempt", status="draft")
            draft += 1
            continue
        pred = " AND ".join(entry["preds"])
        if not pred:
            continue
        status = "confirmed" if entry["auto"] else "draft"
        store.add(conn_id, t, pred, scope="table", status=status)
        if entry["auto"]:
            auto_confirmed += 1
        else:
            draft += 1
    if verdicts:
        logger.info("[kb.filters] conn=%s AI 裁决 %d 条：auto_confirmed=%s draft=%s",
                    conn_id, len(verdicts), auto_confirmed, draft)
    return {"filtered": len(verdicts), "auto_confirmed": auto_confirmed, "draft": draft}


# ---------------------------------------------------------------------------
# S2-2：静态业务常量 AI 识别（kind=constant 生产者，设计 §5/§8.2）
# ---------------------------------------------------------------------------

def _constants_prompt(tables_desc: str) -> str:
    from app.ai.prompts import render
    return render("annotator_constants", tables_desc=tables_desc)


def _tables_desc_for_constants(schema: dict[str, Any],
                               tables: dict[str, Any]) -> str:
    """表/列注释 + 采样值 → 紧凑描述（供常量识别）。"""
    lines: list[str] = []
    for t in schema.get("tables", []):
        tname = t["name"]
        tk = tables.get(tname)
        tcomment = (tk.comment or "").strip() if tk else ""
        lines.append(f"- {tname}：{tcomment or '（无注释）'}")
        for c in schema.get("columns", []):
            if c["table"] != tname:
                continue
            ccomment = ""
            if tk and c["name"] in tk.columns:
                ci = tk.columns[c["name"]]
                ccomment = (ci.comment or "").strip()
                if ci.values:
                    ccomment += f" 取值: {ci.values[:80]}"
            if ccomment:
                lines.append(f"    {c['name']}：{ccomment}")
    return "\n".join(lines)


def _normalize_constants(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        data = json.loads(text)
    except Exception:
        return out
    for it in data.get("constants") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        value = str(it.get("value") or "").strip()
        if not name or not value:
            continue
        out.append({
            "name": name, "value": value,
            "unit": str(it.get("unit") or "").strip(),
            "source_table": str(it.get("source_table") or "").strip(),
            "source_column": str(it.get("source_column") or "").strip(),
            "reason": str(it.get("reason") or "")[:120],
        })
    return out


async def annotate_constants(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any],
    on_progress: Any | None = None,
    limiter: "_AdaptiveLimiter | None" = None,  # 共享全局限流（构建期各阶段共用一个实例）
) -> int:
    """静态业务常量识别（S2-2）：LLM 从表/列注释 + 取值识别常量 → kind=constant 候选。

    - 产出 Concept(kind=constant, status=draft, source=ai) → 人工确认后注入 context
    - mock / 识别失败 → 0（常量是增强知识，非构建关键路径）
    """
    from app.knowledge.semantic.concepts import Concept

    kb = state.knowledge
    tables = kb.semantic_store._tables.get(conn_id, {})
    desc = _tables_desc_for_constants(schema, tables)
    if not desc:
        return 0
    rt = state.runtime.get()
    cfg = _kb_reason_provider_cfg(rt, reasoning=False)
    if gw.is_effective_mock(cfg):
        return 0
    try:
        provider = gw.build_provider(cfg)
        resp = await _chat_with_beat(
            provider,
            [{"role": "user", "content": _constants_prompt(desc)}],
            None,
            ctx={"conn_id": conn_id, "connection": conn_id, "skill": "kb-constants",
                 "source": "kb_build", "status": "egress-constants",
                 "context_meta": {"candidate_tables": [t["name"] for t in schema.get("tables", [])]}},
            on_progress=on_progress, stage="constants", phase="constants",
            step="identify", step_index=1, step_total=1, percent=0,
            limiter=limiter,
        )
        text = resp.content if hasattr(resp, "content") else str(resp)
        items = _normalize_constants(text or "")
    except Exception as e:
        logger.warning("[kb.constants] conn=%s 常量识别异常，跳过：%s", conn_id, e)
        return 0
    n = 0
    for it in items:
        c = Concept(
            name=it["name"],
            canonical_enum=[{"code": it["value"], "label": it["unit"] or ""}],
            members=[{"table": it["source_table"], "column": it["source_column"]}],
            status="draft", kind="constant", source="ai",
        )
        if kb.concept_store.upsert(conn_id, c, schema):
            n += 1
    if n:
        logger.info("[kb.constants] conn=%s 常量候选 %d 条（draft，待人工确认）", conn_id, n)
    return n
