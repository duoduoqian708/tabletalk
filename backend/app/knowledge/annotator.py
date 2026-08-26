"""AI 自动注释：DDL 上下文 + 样本值 → 模型 → 中文注释/标签草案（draft）。

构建管线（store.build）调用 annotate_tables() + annotate_domain()，
独立 API 保留 annotate_knowledge() / annotate_domain() 原入口。

隐私：样本值发送由构建前弹窗的 include_samples 控制；mock 网关时生成
确定性伪注释，保证无 key 也能跑通管线。
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any

from app.ai import gateway as gw
from app.core.schema import get_schema, sample_values
from app.knowledge.ddl_context import is_noise_column

if TYPE_CHECKING:
    from app.state import AppState

logger = logging.getLogger(__name__)


def _kb_reason_provider_cfg(rt) -> dict[str, Any]:
    """KB 阶段2（领域划分）/阶段3（图谱）专用 provider cfg：
    模型配置里探测到支持推理（capabilities.reasoning）→ 开启最大深度档位。
    无 capabilities（旧数据/mock）→ 保持原全局 reasoning 设置。"""
    cfg = rt.provider_config()
    try:
        default_id = getattr(rt, "default_ai_model", "")
        pool = rt.ai_models if hasattr(rt, "ai_models") else []
        default = next((m for m in pool if m.id == default_id), None) or (pool[0] if pool else None)
        cap = getattr(default, "capabilities", None) if default else None
        if cap and cap.get("reasoning"):
            effort = cap.get("reasoning_effort")
            # 有档位 → 开最大深度；无档位（只接受 thinking）→ 仅启用思考
            cfg["reasoning"] = effort if effort in ("low", "medium", "high") else "thinking"
    except Exception:  # noqa: BLE001
        pass
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
    table_ddl: str, samples_ref: str
) -> str:
    """有采样版 prompt：样本取值段 + values 对照指令 + 含 values 的 JSON 契约。

    已有注释内嵌在 DDL 的 /* comment */ 里，随 DDL 发给模型；准则里声明其仅供参考。
    """
    return (
        "你是数据库语义分析专家。请为下表每一列生成准确的中文业务注释"
        "（一句话说清业务用途，不要复述列名或类型）。\n"
        "【建表 DDL】\n"
        f"{table_ddl}\n\n"
        f"{samples_ref}\n"
        "【准则】\n"
        "1. DDL 中的 COMMENT 注释可能过时或不准确，仅供参考，以你的独立复核为准。\n"
        "2. 每列产出一条注释。有样本值时：若该列值形态可辨识（编号/代码/标识/枚举/状态等，"
        "如订单编号形如 o_123），请在注释中附真实取值示例（如「订单编号，示例 o_123」），"
        "便于今后识别同类值；示例一律取自样本，不要编造。\n"
        "3. 低基数离散取值（状态/类型/标志位等）另附加 values=「代码=含义」分号分隔"
        "（如 P=待付款；S=已发货）。\n"
        "4. 额外为整张表写一条表级描述。\n"
        "【输出】\n"
        "返回 JSON 数组，字段级 {\"table\":\"表名\",\"column\":\"列名\","
        "\"comment\":\"一句话（可含取值示例）\",\"values\":\"可选\"}，"
        "表级 {\"table\":\"表名\",\"comment\":\"整表定位一句话\"}。\n"
        "只返回 JSON，不要多余文字。"
    )


def _annotation_prompt_unsampled(table_ddl: str) -> str:
    """无采样版 prompt：纯结构，不提 values（不诱导编造取值含义）。"""
    return (
        "你是数据库语义分析专家。请为下表每一列生成准确的中文业务注释"
        "（一句话说清业务用途，不要复述列名或类型）。\n"
        "【建表 DDL】\n"
        f"{table_ddl}\n\n"
        "【准则】\n"
        "1. DDL 中的 COMMENT 注释可能过时或不准确，仅供参考，以你的独立复核为准。\n"
        "2. 每列产出一条注释；对低基数离散字段（状态/标志位等）仅当能从字段名/类型/"
        "注释可靠推断时才简述取值含义，否则保守描述，不编造具体取值。\n"
        "3. 额外为整张表写一条表级描述。\n"
        "【输出】\n"
        "返回 JSON 数组，字段级 {\"table\":\"表名\",\"column\":\"列名\",\"comment\":\"一句话\"}，"
        "表级 {\"table\":\"表名\",\"comment\":\"整表定位一句话\"}。\n"
        "只返回 JSON，不要多余文字。"
    )


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
    if not (2 <= len(distinct) <= ENUM_MAX_VALUES):
        return ""
    return "；".join(f"{v}={v}（业务含义待确认）" for v in distinct)


def _mock_table_comments_from_ddl(
    table_name: str,
    columns: list[dict[str, Any]],
    samples: dict[str, list[Any]] | None,
    table_comment: str = "",
) -> list[dict[str, Any]]:
    """mock：为单表生成伪注释（无 LLM 时的 fallback）；低基数列附 values/example。

    表级描述草案：仅当无库注释时产出（库注释视为权威，不产竞争草案）。
    """
    items: list[dict[str, Any]] = []
    if not table_comment:
        items.append({
            "table": table_name, "column": None,
            "comment": f"{table_name} 表：{len(columns)} 列的业务实体。",
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
        if values:
            item["values"] = values
        example = _first_example(samples, c["name"])
        if example:
            item["example"] = example
        items.append(item)
    return items


async def annotate_table(
    state: "AppState",
    conn_id: str,
    table_name: str,
    table_ddl: str,
    schema: dict[str, Any],
    samples: dict[str, dict[str, list[Any]]] | None = None,
) -> list[dict[str, Any]]:
    """为单张表生成列级注释（DDL 做上下文，可选样本值）。

    无论表已有注释与否，都让 AI 生成语义注释（有注释时 AI 参考但不盲信）。
    返回 items 列表（未落库，由调用方统一 annotate_drafts）：
    [{table, column, comment, values?, example?}]。
    values/example 仅在授权样本（include_samples）时产生——天然门控。
    """
    rt = state.runtime.get()
    columns = [c for c in schema.get("columns", []) if c["table"] == table_name]
    table_comment = next(
        (t.get("comment", "") for t in schema.get("tables", []) if t["name"] == table_name), ""
    )
    table_samples = samples.get(table_name) if samples else None

    provider_cfg = rt.provider_config()
    if gw.is_effective_mock(provider_cfg):
        logger.debug("[kb.annotate] conn=%s mock 伪注释 table=%s", conn_id, table_name)
        return _mock_table_comments_from_ddl(table_name, columns, table_samples, table_comment)

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
    prompt = (
        _annotation_prompt_sampled(table_ddl, samples_ref)
        if table_samples
        else _annotation_prompt_unsampled(table_ddl)
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
    # 表级项（column 省略）只保留无库注释的表：库注释视为权威，不产竞争草案
    if table_comment:
        items = [it for it in items if it.get("column") is not None]
    # 无采样硬闸（段4）：无授权样本 → 丢弃 LLM 可能硬凑的 values/example
    # （未经实际数据校验，且"未授权不落盘"红线）；有样本 → example 后端规范化提取。
    if not table_samples:
        for it in items:
            it.pop("values", None)
            it.pop("example", None)
    else:
        # example 从样本提取（后端规范化，不依赖 LLM）：首个非空值截断 60
        for it in items:
            if it.get("column"):
                example = _first_example(table_samples, it["column"])
                if example:
                    it["example"] = example
    return items


async def annotate_tables(
    state: "AppState",
    conn_id: str,
    ddl_map: dict[str, str],
    schema: dict[str, Any],
    samples: dict[str, dict[str, list[Any]]] | None = None,
    on_progress: Any | None = None,
    p0: int = 15,
    p1: int = 40,
) -> int:
    """逐表 DDL annotation 批量入口：返回新增 draft 数量。

    ddl_map: {table_name: ddl_string}，由 ddl_context.generate_ddls_all 生成。
    on_progress(stage, percent, detail, phase) 用于构建进度回调（阶段一「AI 正在处理」）。
    """
    items_all: list[dict[str, Any]] = []
    table_names = [t["name"] for t in schema.get("tables", []) if t["name"] in ddl_map]
    n = max(1, len(table_names))
    logger.info("[kb.annotate] conn=%s 开始逐表注释：tables=%s", conn_id, len(table_names))
    for i, tbl in enumerate(table_names):
        if on_progress:
            on_progress(
                "AI 正在处理", p0 + (p1 - p0) * (i + 1) // n,
                f"表 {tbl}（{i + 1}/{n}）", phase="annotate",
                step="per_table", step_index=i + 1, step_total=n,
            )
        try:
            tbl_items = await annotate_table(
                state, conn_id, tbl, ddl_map[tbl], schema, samples,
            )
            if not tbl_items:
                logger.debug("[kb.annotate] conn=%s 单表注释为空 table=%s", conn_id, tbl)
            items_all.extend(tbl_items)
        except Exception as e:
            logger.warning("[kb.annotate] conn=%s 单表注释失败 table=%s：%s", conn_id, tbl, e)
            continue
    if not items_all and table_names:
        logger.warning("[kb.annotate] conn=%s LLM 返回解析为空：处理了 %s 张表但零产出", conn_id, len(table_names))
    added = state.knowledge.annotate_drafts(conn_id, items_all)
    logger.info("[kb.annotate] conn=%s 逐表注释完成：items=%s added=%s", conn_id, len(items_all), added)
    return added


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

    provider_cfg = rt.provider_config()
    if gw.is_effective_mock(provider_cfg):
        items = _mock_comments(schema, samples)
    else:
        # 独立 API 时无 DDL，使用旧 schema 格式
        prompt = (
            "你是数据库知识构建助手。为下面 schema 中的表和列生成简短中文注释。\n"
            "已有注释仅供参考，可能过时或不准确，请结合字段名和上下文独立判断。\n"
            "返回 JSON 数组，元素形如 {\"table\": \"表名\", \"column\": \"列名，表级注释则省略\", \"comment\": \"一句话中文注释\"}。\n"
            "只返回 JSON，不要多余文字。\n" + _prompt_schema(schema, samples)
        )
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

    added = state.knowledge.annotate_drafts(conn_id, items)
    return {"items": len(items), "added": added, "samples_used": include_samples}


# ---------- 领域划分（KC2·段6：画像打包 → 一轮划分 → 一轮自检） ----------

_MOCK_TAG_HINTS = {
    "order": "订单", "product": "商品", "customer": "客户", "return": "退货",
    "pay": "支付", "ship": "物流", "inventory": "库存", "review": "评价",
    "categor": "分类", "supplier": "供应商", "campaign": "营销", "address": "地址",
}

# ---------- 精简 DDL 画像（阶段二/三统一上下文：DDL 结构 + 描述 + 取值示例 + FK 内联） ----------

_TRUE_LONG_TYPES = ("CLOB", "BLOB", "LONGTEXT", "MEDIUMTEXT", "BYTEA", "JSON")


def _portrait_noise(name: str, ctype: str) -> bool:
    """画像噪音列：列名级语义噪音（时间戳/审计/软删）+ 真长文本类型。
    绝不用类型前缀宽判——`status TEXT` 是业务枚举，不是长文本。"""
    if is_noise_column(name):
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
        # 描述 + 取值示例熔一句
        desc_extra = ""
        if samples is not None and not fk_refs.get(name):
            distinct = _distinct_values(samples, name)
            ex = _first_example(samples, name)
            tu = (t or "").upper()
            is_high = is_pk or tu.startswith(
                ("INT", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE", "DATE", "TIME")
            )
            if not is_high and 2 <= len(distinct) <= 50:
                desc_extra = "取值示例：" + "，".join(str(v)[:24] for v in distinct[:8])
            elif ex:
                desc_extra = "取值示例：" + str(ex)[:30]
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
            # 表级：库注释 > AI 注释（draft/confirmed）
            tdesc = t.get("comment", "") or (
                tk.comment if tk is not None and tk.status in ("draft", "confirmed") else ""
            )
            table_desc[name] = tdesc
            # 列级：库注释 > AI 注释
            cdesc: dict[str, str] = {}
            for c in schema.get("columns", []):
                if c["table"] != name:
                    continue
                cname = c["name"]
                dbc = c.get("comment", "")
                ci = tk.columns.get(cname) if tk is not None else None
                ai = (ci.comment if ci is not None and ci.status in ("draft", "confirmed") else "")
                cdesc[cname] = dbc or ai
            col_desc[name] = cdesc
    except Exception:  # noqa: BLE001 - 读知识库失败不影响画像构造
        pass
    return table_desc, col_desc


def _build_ddl_portraits(
    state: "AppState", conn_id: str, schema: dict[str, Any],
    samples: dict[str, dict[str, Any]] | None = None,
) -> str:
    """全库精简 DDL 画像：每表一条，供阶段二（领域标签）/阶段三（图谱）统一作上下文。"""
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


def _parse_domain_payload(text: str) -> tuple[bool, list[dict[str, Any]]]:
    """解析领域/自检响应：{"unchanged": true} → (True, [])；JSON 数组 → (False, 领域条目)。

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
) -> str:
    return (
        "你是数据库领域分析专家。请将下列表按业务语义划分为若干领域。\n"
        "【表画像】\n" + portraits + "\n\n"
        "【准则】\n"
        f"1. 领域数量目标约 {lo}~{hi} 个（库共 {n_tables} 张表；目标可小幅浮动，不要为凑数硬拆）。\n"
        "2. 每张表至少归入 1 个领域；可挂多个（复合实体同时属于多个域）。\n"
        "3. 领域名简短、语义清晰、互不重叠；领域内表业务高度相关。\n"
        "4. 每个领域给一句话「依据」（为什么这些表同属一域）。\n"
        "【输出】\n"
        "返回 JSON 数组，元素形如 "
        '{"name":"领域名","description":"一句话描述","tables":["表1","表2"],"reason":"依据"}。\n'
        "只返回 JSON，不要多余文字。"
    )


def _domain_selfcheck_prompt(
    portraits: str, domains: list[dict[str, Any]], lo: int, hi: int,
) -> str:
    domain_lines = "\n".join(
        f"- {d['name']}（{d.get('description','')}）: {', '.join(d['tables'])}"
        for d in domains
    )
    return (
        "你是数据库领域划分审校专家。请审查以下初版划分，指出需修正之处并给出修正版。\n"
        "【表画像】\n" + portraits + "\n\n【初版划分】\n" + domain_lines + "\n\n"
        "【准则】\n"
        "1. 孤表：任何表都必须至少属于一个域（否则补齐归属）。\n"
        "2. 单表成域：某域只有 1 张表且与相邻域语义可合并 → 合并。\n"
        "3. 语义重叠：两域边界不清 → 理清边界或合并。\n"
        "4. 命名：域名简短、语义清晰、互不重叠。\n"
        f"5. 领域数量保持约 {lo}~{hi} 个（可小幅浮动）。\n"
        "【输出】\n"
        "若初版已合理，返回 {\"unchanged\": true}；\n"
        "若有改进，返回完整修正后的领域 JSON 数组（格式、字段同第一轮）。\n"
        "只返回 JSON，不要多余文字。"
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
    return (
        "你是数据库领域分析专家。以下数据库已有一部分『既有领域』（已确认标签）。\n"
        "请把【新增表】归入最合适的既有领域；若无合适领域，则提议新的领域。\n"
        "【既有领域（已确认标签）】\n"
        f"{existing_tags or '（暂无）'}\n\n"
        "【新增表画像】\n"
        f"{targets_portraits}\n\n"
        "【准则】\n"
        "1. 只能为新增表指定归属；不得改动既有领域已有的成员表。\n"
        "2. 一张新增表可归多个领域；每项给出一句「依据」。\n"
        "3. 领域名复用既有领域名；确实无法归入时才提议新名。\n"
        "【输出】\n"
        "返回 JSON 数组，元素形如 "
        '{"table":"表名","tags":["领域名","可选新域名"],"reason":"依据"}。\n'
        "只返回 JSON，不要多余文字。"
    )


async def annotate_domain(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any] | None = None,
    mode: str = "full",                  # full=全量空库划分；incremental=增量吸收新表（D1 收紧）
    target_tables: list[str] | None = None,  # incremental 模式：只处理这些表（新增/变化表）
    self_check: bool | None = None,    # 构建期覆盖（None=运行时 kb_build_self_check）
    on_progress: Any | None = None,    # 构建进度（阶段二子步：划分 → 自检）
) -> dict[str, Any]:
    """AI 领域划分（KC2 v2）：一轮画像打包划分 + 一轮审校式自检。写入知识库。

    产物：领域列表 [{name, 描述, 成员表[], 依据}] → 落库为标签草案 + 表→域多打标。
    段6：不再写 desc_drafts（表描述由阶段一列注释 + 库注释承担，领域调用只产出标签）。
    自检（kb_build_self_check 开时）：拿初版划分回去审校，仅在有改进时输出修正版，
    否则 {"unchanged":true} 保留初版——不重跑生成。

    mode="incremental"（增量标签吸收，D1 收紧）：
      只把 target_tables 归入既有 confirmed 域或提议新域；不改已有绑定表、不清标签库。
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
    # 新画像（DDL 结构 + 描述 + 取值示例 + FK 内联）；样本仅授权才带（否则取值示例为空）
    _samples = state.knowledge._samples.get(conn_id, {}) if state.knowledge._samples.get(conn_id) else {}
    portraits = _build_ddl_portraits(state, conn_id, schema, _samples)
    n = len(table_names)
    target = max(3, round(math.sqrt(n)))
    lo, hi = max(2, target - 1), target + 1

    rt = state.runtime.get()
    if self_check is None:
        self_check = bool(getattr(rt, "kb_build_self_check", True))
    logger.info("[kb.tags] conn=%s 领域划分：tables=%s target=%s~%s self_check=%s",
                conn_id, n, lo, hi, self_check)
    _tag_cfg = _kb_reason_provider_cfg(rt)
    domains: list[dict[str, Any]] = []

    if gw.is_effective_mock(_tag_cfg):
        logger.debug("[kb.tags] conn=%s mock 领域划分", conn_id)
        if on_progress:
            on_progress("AI 标签提取", 0, "领域划分", phase="tags",
                        step="partition", step_index=1, step_total=1)
        domains = _normalize_domains(_mock_domains(schema), table_names)
    else:
        provider = gw.build_provider(_tag_cfg)
        # 第一轮：打包划分（N 条画像一次调用）
        if on_progress:
            on_progress("AI 标签提取", 0, "领域划分", phase="tags",
                        step="partition", step_index=1,
                        step_total=2 if self_check else 1)
        resp = await provider.chat(
            [{"role": "user", "content": _domain_partition_prompt(portraits, lo, hi, n)}],
            tools=None,
            ctx={
                "conn_id": conn_id, "connection": conn_id, "skill": "kb-tags",
                "source": "kb_build", "status": "egress-tags",
                "context_meta": {"candidate_tables": table_names},
            },
        )
        _unchanged, domains = _parse_domain_payload(resp.content or "")
        if domains:
            domains = _normalize_domains(domains, table_names)
            logger.debug("[kb.tags] conn=%s 划分产出：domains=%s", conn_id, len(domains))
        else:
            logger.warning("[kb.tags] conn=%s LLM 划分返回解析为空", conn_id)
        # 第二轮：审校式自检（不重跑生成；unchanged 保留初版）
        if self_check and domains:
            if on_progress:
                on_progress("AI 标签提取", 50, "审校自检", phase="tags",
                            step="selfcheck", step_index=2, step_total=2)
            chk = await provider.chat(
                [{"role": "user", "content": _domain_selfcheck_prompt(portraits, domains, lo, hi)}],
                tools=None,
                ctx={
                    "conn_id": conn_id, "connection": conn_id, "skill": "kb-tags",
                    "source": "kb_build", "status": "egress-tags-selfcheck",
                    "context_meta": {"candidate_tables": table_names},
                },
            )
            unchanged, revised = _parse_domain_payload(chk.content or "")
            if not unchanged and revised:
                nd = _normalize_domains(revised, table_names)
                if nd:
                    domains = nd
                    logger.debug("[kb.tags] conn=%s 自检采纳修正版：domains=%s", conn_id, len(domains))
            else:
                logger.debug("[kb.tags] conn=%s 自检 unchanged，保留初版", conn_id)

    # 兜底：任何路径产出都必须无孤表、域数合法
    domains = _normalize_domains(domains, table_names)

    # 落库：域 → 标签草案 + 表→域多打标；不再写 desc_drafts
    tag_props: list[dict[str, str]] = []
    seen: set[str] = set()
    for d in domains:
        tg = d["name"]
        if tg and tg not in seen:
            seen.add(tg)
            tag_props.append({"name": tg, "description": d.get("description") or f"{tg} 领域"})
    state.knowledge.upsert_tags(conn_id, tag_props)
    table_tags: dict[str, list[str]] = {}
    for d in domains:
        for t in d["tables"]:
            table_tags.setdefault(t, []).append(d["name"])
    for t, tags in table_tags.items():
        state.knowledge.assign_table_tags(conn_id, t, tags)

    logger.info("[kb.tags] conn=%s 领域落库完成：domains=%s new_tags=%s assigned表=%s",
                conn_id, len(domains), len(tag_props), len(table_tags))
    return {
        "tables": len(table_names),
        "domains": len(domains),
        "new_tags": len(tag_props),
        "library_size": len(state.knowledge.tags(conn_id)["library"]),
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
    _samples = state.knowledge._samples.get(conn_id, {}) if state.knowledge._samples.get(conn_id) else {}
    portraits = _build_ddl_portraits(state, conn_id, sub_schema, _samples)

    lib = {t["name"]: t for t in state.knowledge.tags(conn_id)["library"]}
    confirmed = {n: v for n, v in lib.items() if v.get("status") == "confirmed"}
    existing_desc = "\n".join(
        f"- {nm}（{confirmed[nm].get('description','')}）" for nm in confirmed
    ) or "（暂无）"

    rt = state.runtime.get()
    _tag_cfg = _kb_reason_provider_cfg(rt)
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
            [{"role": "user", "content": _incremental_tag_absorb_prompt(portraits, existing_desc)}],
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
        state.knowledge.assign_table_tags(conn_id, ln["table"], ln["tags"])

    logger.info("[kb.tags] conn=%s 增量吸收完成：tables=%s existing=%s new=%s",
                conn_id, len(targets), len(confirmed), len(new_tags))
    return {
        "tables": len(targets),
        "domains": len({tg for ln in links for tg in ln["tags"]}),
        "new_tags": len(new_tags),
        "library_size": len(state.knowledge.tags(conn_id)["library"]),
    }


# ---------- LLM 图谱识别（两轮：全局扫描 + 候选验证） ----------


def _generate_candidate_pairs(schema: dict[str, Any]) -> list[dict[str, str]]:
    """程序启发式：根据列名匹配 + 类型兼容，生成可能有关联的表对候选。

    策略：A 表某列名（去掉 _id 后缀）≈ B 表名，或两表有同名非通用列且类型兼容。
    边 v2：from 恒为持有引用列的表（多侧）；列兼主键 → 1:1，否则 n:1。
    """
    candidates: list[dict[str, str]] = []
    tables = schema.get("tables", [])
    columns = schema.get("columns", [])
    cols_by_table: dict[str, list[dict]] = {}
    for c in columns:
        cols_by_table.setdefault(c["table"], []).append(c)
    table_names = {t["name"] for t in tables}
    # 通用名（不做匹配锚点）
    generic_names = {"id", "uuid", "guid", "status", "type", "name", "code",
                     "created_at", "updated_at", "deleted_at", "created_by", "updated_by"}
    seen_pairs: set[tuple[str, str, str, str]] = set()

    for t in tables:
        tname = t["name"]
        for c in cols_by_table.get(tname, []):
            cname = c["name"].lower()
            if cname in generic_names:
                continue
            # 去掉 _id 后缀，尝试匹配另一张表名
            base = cname.removesuffix("_id")
            if base != cname and base in table_names and base != tname:
                pair = tuple(sorted([tname, base]))
                ref_col = next(
                    (cc["name"] for cc in cols_by_table.get(base, [])
                     if cc["name"].lower() == "id"),
                    "id",
                )
                cardinality = "1:1" if c.get("pk") else "n:1"
                key = (pair[0], pair[1], c["name"], ref_col)
                if key not in seen_pairs:
                    candidates.append({
                        "from_table": tname, "from_col": c["name"],
                        "to_table": base, "to_col": ref_col,
                        "cardinality": cardinality,
                        "reason": f"列名匹配：{tname}.{c['name']} → {base}.{ref_col}",
                    })
                    seen_pairs.add(key)

    return candidates


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
    self_check: bool | None = None,   # 构建期覆盖（None=运行时 kb_build_self_check）
) -> list[dict[str, Any]]:
    """LLM 图谱识别：两轮调用（全局扫描 → 候选裁决+自检），返回 draft 边列表。

    全局扫描上下文 = 精简 DDL 画像（_build_ddl_portraits，描述回填 + FK 内联）。
    schema: 完整 schema，用于生成候选对与画像。
    on_progress: 阶段三「AI 关系识别」进度回调（第一轮全局扫描 0→50，第二轮候选裁决 50→100）。
    kb_build_self_check 开时：第二轮待裁决池 = 程序候选 ∪ 首轮低置信（confidence≠high）LLM 边，
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
    # 阶段3（图谱）：支持推理则开最大深度（能力探测落库 → _kb_reason_provider_cfg）
    provider_cfg = _kb_reason_provider_cfg(rt)
    logger.info(
        "[kb.graph] conn=%s 开始关系识别：tables=%s fks=%s",
        conn_id, len(schema.get("tables", [])), len(schema.get("foreign_keys", [])),
    )
    if gw.is_effective_mock(provider_cfg):
        logger.debug("[kb.graph] conn=%s mock FK 边", conn_id)
        if on_progress:
            on_progress("AI 关系识别", 100, None, phase="graph",
                        step="global", step_index=1, step_total=1)
        return _mock_graph_edges(schema)

    provider = gw.build_provider(provider_cfg)

    # 上下文：精简 DDL 画像（描述回填 + 取值示例 + FK 内联），与阶段二统一
    _samples = state.knowledge._samples.get(conn_id, {}) if state.knowledge._samples.get(conn_id) else {}
    graph_overview = _build_ddl_portraits(state, conn_id, schema, _samples)

    # ---- 第一轮：全局扫描（广度优先） ----
    global_prompt = (
        "你是数据库关系分析专家。请判断下表结构之间存在哪些业务关联。\n"
        "【表结构】\n" + graph_overview + "\n\n"
        "【准则】\n"
        "1. 关联不限同名列；结合字段名与语义判断（如 status 枚举、xxx_id 引用）。\n"
        "2. 区分真实业务关联与单纯同名字段；中间表（junction）拆成两条边，禁止 n:m 直连。\n"
        "3. 方向：from_table 是多侧（明细/子表，持有引用字段），to_table 是一侧（主表/父表）。\n"
        "4. 基数：n:1（多对一）或 1:1（外键兼主键）；from_col 是 from_table 主键时只能 1:1；"
        "不确定默认 n:1。\n"
        "5. 每条边给 confidence（high/medium/low）与一句 reason 依据。\n"
        "【输出】\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id",'
        '"cardinality":"n:1","confidence":"high/medium/low","reason":"依据"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    global_resp = await provider.chat(
        [{"role": "user", "content": global_prompt}], tools=None,
        ctx={
            "conn_id": conn_id, "connection": conn_id, "skill": "kb-graph",
            "source": "kb_build", "status": "egress-graph-global",
            "context_meta": {"candidate_tables": [t["name"] for t in schema.get("tables", [])]},
        },
    )
    global_edges = _parse_graph_edges(global_resp.content or "", schema)
    if not global_edges:
        logger.warning("[kb.graph] conn=%s LLM 返回解析为空（关系识别·全局扫描）", conn_id)
    logger.debug("[kb.graph] conn=%s 全局扫描边数=%s", conn_id, len(global_edges))
    # 标记来源
    for e in global_edges:
        e["source"] = "llm_global"
    if on_progress:
        on_progress("AI 关系识别", 50, None, phase="graph",
                    step="global", step_index=1, step_total=2)

    # ---- 第二轮：候选验证 + 自检（程序候选 ∪ 首轮低置信 LLM 边；只裁决不新增） ----
    if self_check is None:
        self_check = bool(getattr(rt, "kb_build_self_check", True))
    candidates = _generate_candidate_pairs(schema)
    global_keys = {
        (e["from_table"], e["to_table"], e.get("from_col"), e.get("to_col"))
        for e in global_edges
    }
    rev_global_keys = {
        (e["to_table"], e["from_table"], e.get("to_col"), e.get("from_col"))
        for e in global_edges
    }

    # 待裁决池 = 未被全局扫描覆盖的程序候选 ∪ 首轮 confidence≠high 的 LLM 边（自检开时）
    pool: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()

    def _push(item: dict[str, Any]) -> None:
        key = (item["from_table"], item.get("from_col"), item["to_table"], item.get("to_col"))
        if key not in seen_keys:
            seen_keys.add(key)
            pool.append(item)

    for c in candidates:
        key = (c["from_table"], c["to_table"], c["from_col"], c["to_col"])
        if key not in global_keys and key not in rev_global_keys:
            _push(c)
    if self_check:
        for e in global_edges:
            if e.get("confidence") != "high":
                _push(e)

    if not pool:
        logger.info(
            "[kb.graph] conn=%s 无可复验候选（程序候选均已覆盖 + 无低置信 LLM 边），边 %s 条",
            conn_id, len(global_edges),
        )
        return global_edges

    # 构建待裁决候选的上下文（只给相关表的结构）
    related_tables = set()
    for c in pool:
        related_tables.add(c["from_table"])
        related_tables.add(c["to_table"])

    verify_lines: list[str] = []
    for c in pool:
        src = "全局扫描低置信" if c.get("source") == "llm_global" else "程序列名匹配"
        verify_lines.append(
            f"- {c['from_table']}.{c['from_col']} → {c['to_table']}.{c['to_col']}"
            f"  （{c.get('cardinality', 'n:1')}，来源：{src}，依据：{c.get('reason', '')}）"
        )

    # 相关表的精简结构（供 LLM 判断字段语义）
    rel_schema_lines: list[str] = []
    for t in schema.get("tables", []):
        if t["name"] not in related_tables:
            continue
        cols = [cc for cc in schema.get("columns", []) if cc["table"] == t["name"]]
        col_txt = ", ".join(
            f"{cc['name']}({cc.get('type', '')}{' PK' if cc.get('pk') else ''})"
            for cc in cols
        )
        rel_schema_lines.append(f"- {t['name']}: {col_txt}")

    verify_prompt = (
        "你是数据库关系分析专家。请对下列候选关系逐项裁决其是否成立。\n"
        "【准则】\n"
        "1. 只裁决候选项，不得新增列表之外的关系。\n"
        "2. 成立 → status=confirmed（可修正关联字段/基数）；不成立 → status=rejected。\n"
        "3. 拒绝无实际业务关联的候选（如仅仅命名巧合）。\n"
        "4. 方向：from_table 是多侧（明细/子表），to_table 是一侧（主表/父表）。\n"
        "5. 基数：n:1 或 1:1（from_col 是 from_table 主键时只能 1:1）。\n"
        "【相关表结构】\n" + "\n".join(rel_schema_lines) + "\n\n"
        "【待裁决候选关系】\n" + "\n".join(verify_lines) + "\n\n"
        "【输出】\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id",'
        '"cardinality":"n:1","confidence":"high/medium/low","reason":"说明","status":"confirmed/rejected"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    verify_resp = await provider.chat(
        [{"role": "user", "content": verify_prompt}], tools=None,
        ctx={
            "conn_id": conn_id, "connection": conn_id, "skill": "kb-graph",
            "source": "kb_build", "status": "egress-graph-verify",
            "context_meta": {"candidate_tables": list(related_tables)},
        },
    )
    verified_edges = _parse_graph_edges(verify_resp.content or "", schema)
    if not verified_edges:
        logger.warning("[kb.graph] conn=%s LLM 返回解析为空（关系识别·候选验证 %s 条）", conn_id, len(pool))
    for e in verified_edges:
        e["source"] = "llm_verify"
    confirmed_verified = [e for e in verified_edges if e.get("status") != "rejected"]
    if on_progress:
        on_progress("AI 关系识别", 100, None, phase="graph",
                    step="verify", step_index=2, step_total=2)

    # 合并：全局边中高置信的保留直用（自检关时全部保留）；第二轮通过项并入
    base_global = (
        [e for e in global_edges if e.get("confidence") == "high"] if self_check
        else list(global_edges)
    )
    all_edges = base_global + confirmed_verified

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
        "[kb.graph] conn=%s 关系识别完成：global=%s(高置信%s) verify=%s total=%s",
        conn_id, len(global_edges), len(base_global), len(confirmed_verified), len(deduped),
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
            on_progress("AI 关系识别", 100, None, phase="graph",
                        step="incr_global", step_index=1, step_total=1)
        return _mock_graph_edges(sub_schema)  # FK 元数据边（mock 走 FK）

    provider = gw.build_provider(provider_cfg)
    if on_progress:
        on_progress("AI 关系识别", 0, None, phase="graph",
                    step="incr_global", step_index=1, step_total=1)
    prompt = (
        "你是数据库关系分析专家。以下是一个数据库的局部表结构（含本次新增/变化的表）。\n"
        "请判断这些表之间存在哪些业务关联，**尤其标出【新增/变化表】与其它们的关联**。\n"
        "【新增/变化表】: " + ", ".join(targets) + "\n"
        "【表结构】\n" + overview + "\n\n"
        "【准则】\n"
        "1. 关联不限同名列；结合字段名与语义判断（如 xxx_id 引用、status 枚举）。\n"
        "2. 区分真实关联与单纯同名字段；禁止 n:m 直连（junction 拆两条 n:1）。\n"
        "3. 方向：from_table 是多侧（明细/子表，持有引用字段），to_table 是一侧。\n"
        "4. 基数：n:1 或 1:1（from_col 为 from_table 主键时只能 1:1）；不确定默认 n:1。\n"
        "5. 每条边给 confidence（high/medium/low）与一句 reason。\n"
        "【输出】\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id",'
        '"cardinality":"n:1","confidence":"high/medium/low","reason":"依据"}。\n'
        "只返回 JSON，不要多余文字。"
    )
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
        on_progress("AI 关系识别", 100, None, phase="graph",
                    step="incr_global", step_index=1, step_total=1)
    logger.info("[kb.graph] conn=%s 增量局部补边完成：subgraph=%s 目标边=%s",
                conn_id, len(sub_tables), len(edges))
    return edges
