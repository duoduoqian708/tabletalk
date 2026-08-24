"""AI 自动注释：DDL 上下文 + 样本值 → 模型 → 中文注释/标签草案（draft）。

构建管线（store.build）调用 annotate_tables() + annotate_domain()，
独立 API 保留 annotate_knowledge() / annotate_domain() 原入口。

隐私：样本值发送由构建前弹窗的 include_samples 控制；mock 网关时生成
确定性伪注释，保证无 key 也能跑通管线。
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from app.ai import gateway as gw
from app.core.schema import get_schema, sample_values

if TYPE_CHECKING:
    from app.state import AppState

logger = logging.getLogger(__name__)


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
        out.append({"table": it["table"], "column": it.get("column"), "comment": comment})
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


def _mock_table_comments_from_ddl(
    table_name: str,
    columns: list[dict[str, Any]],
    samples: dict[str, list[Any]] | None,
) -> list[dict[str, Any]]:
    """mock：为单表生成伪注释（无 LLM 时的 fallback）。"""
    items: list[dict[str, Any]] = []
    for c in columns:
        extra = ""
        if samples:
            vals = [str(v) for v in (samples or {}).get(c["name"], []) if v is not None][:3]
            if vals:
                extra = f"，示例取值如 {vals}"
        items.append({
            "table": table_name, "column": c["name"],
            "comment": f"列 {c['name']}，类型 {c.get('type', '')}{extra}。",
        })
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
    返回 items 列表（未落库，由调用方统一 annotate_drafts）。
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
        return _mock_table_comments_from_ddl(table_name, columns, table_samples)

    # 构建已有注释参考段（仅标注哪些列有注释，AI 参考但不盲信）
    existing_lines: list[str] = []
    if table_comment:
        existing_lines.append(f"表注释（仅供参考）：{table_comment}")
    for c in columns:
        if c.get("comment"):
            existing_lines.append(f"- {c['name']}: {c['comment']}")
    existing_ref = ""
    if existing_lines:
        existing_ref = "\n【已有注释（仅供参考，可能过时或不准确，请结合字段名和上下文独立判断）】\n" + "\n".join(existing_lines)

    # 样本值段
    samples_ref = ""
    if table_samples:
        sample_lines = []
        for c in columns:
            vals = table_samples.get(c["name"])
            if vals:
                val_strs = [str(v) for v in vals if v is not None][:5]
                if val_strs:
                    sample_lines.append(f"- {c['name']}: {val_strs}")
        if sample_lines:
            samples_ref = "\n【样本取值（用于辅助理解字段含义）】\n" + "\n".join(sample_lines)

    prompt = (
        "你是数据库语义分析专家。请根据建表 DDL 为每列生成准确的中文业务语义注释。\n"
        "注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。\n"
        f"{existing_ref}"
        f"{samples_ref}\n"
        "【建表 DDL】\n"
        f"{table_ddl}\n\n"
        "请为每列生成注释。返回 JSON 数组，元素形如 "
        '{"table": "表名", "column": "列名", "comment": "一句话中文注释"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    provider = gw.build_provider(provider_cfg)
    resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
    return _parse_items(resp.content or "")


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
            on_progress("AI 正在处理", p0 + (p1 - p0) * i // n, tbl, phase="annotate")
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
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        items = _parse_items(resp.content or "")

    added = state.knowledge.annotate_drafts(conn_id, items)
    return {"items": len(items), "added": added, "samples_used": include_samples}


# ---------- 领域标签生成（KC2） ----------

_MOCK_TAG_HINTS = {
    "order": "订单", "product": "商品", "customer": "客户", "return": "退货",
    "pay": "支付", "ship": "物流", "inventory": "库存", "review": "评价",
    "categor": "分类", "supplier": "供应商", "campaign": "营销", "address": "地址",
}


def _mock_domain_tags(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """mock：按表名关键词给确定性标签，能体现"多表复用同一标签"（如 order_items / order_status_history → 订单）。"""
    items: list[dict[str, Any]] = []
    for t in schema["tables"]:
        name = t["name"].lower()
        tag = next((zh for kw, zh in _MOCK_TAG_HINTS.items() if kw in name), "业务")
        items.append({
            "table": t["name"],
            "description": f"{t['name']} 表：{t.get('column_count', 0)} 列的业务实体。",
            "tags": [tag],
        })
    return items


def _parse_domain_items(text: str) -> list[dict[str, Any]]:
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
        tags = it.get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        out.append({
            "table": it["table"],
            "description": str(it.get("description", "")).strip(),
            "tags": [str(x).strip() for x in tags if str(x).strip()],
        })
    return out


async def annotate_domain(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any] | None = None,
    ddl_overview: str | None = None,
) -> dict[str, Any]:
    """AI 生成逐表描述 + 领域标签（约束在现有标签库内复用，新标签提草案）。写入知识库。

    ddl_overview: 精简版表结构概览（由 ddl_context.build_ddl_overview 生成），
    传入时替代旧的 table_desc 格式，提供更丰富的上下文给 LLM。
    """
    if schema is None:
        schema = await get_schema(state, conn_id)
    lib = state.knowledge.tags(conn_id)["library"]
    existing = {t["name"]: t for t in lib}
    existing_names = sorted(existing.keys())
    existing_desc = "\n".join(
        f"- {n}（{existing[n].get('description','')}，{existing[n].get('status','draft')}）" for n in existing_names
    ) or "（暂无）"

    rt = state.runtime.get()
    logger.info("[kb.tags] conn=%s 开始领域标签提取：tables=%s", conn_id, len(schema.get("tables", [])))
    if gw.is_effective_mock(rt.provider_config()):
        logger.debug("[kb.tags] conn=%s mock 伪标签", conn_id)
        items = _mock_domain_tags(schema)
    else:
        if ddl_overview:
            table_section = ddl_overview
        else:
            table_section = "\n".join(
                f"- {t['name']}（{t.get('comment','')}，{t.get('column_count',0)} 列）"
                for t in schema["tables"]
            )
        prompt = (
            "你是数据库领域分析专家。为下面的每张表生成：①一句话中文描述；②1~3 个中文领域标签。\n"
            "描述应说明该表的核心业务用途，不要复述列数。\n"
            "标签规则：优先从【已有标签】中复用，确有新领域才提新标签（新标签会走人工确认）。\n"
            "【已有标签】\n" + existing_desc + "\n\n【表结构】\n" + table_section + "\n\n"
            "返回 JSON 数组，元素形如 {\"table\":\"表名\",\"description\":\"中文描述\",\"tags\":[\"标签1\",\"标签2\"]}。\n"
            "只返回 JSON，不要多余文字。"
        )
        provider = gw.build_provider(rt.provider_config())
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        items = _parse_domain_items(resp.content or "")
        if not items:
            logger.warning("[kb.tags] conn=%s LLM 返回解析为空（领域标签）", conn_id)

    # 落库：表描述草案 + 标签草案 + 打标
    desc_drafts = [
        {"table": it["table"], "column": None, "comment": it["description"]}
        for it in items if it["description"]
    ]
    tag_props = []
    seen: set[str] = set()
    for it in items:
        for tg in it["tags"]:
            if tg in seen or tg in existing_names:
                continue
            seen.add(tg)
            tag_props.append({"name": tg, "description": f"{tg} 领域"})
    state.knowledge.annotate_drafts(conn_id, desc_drafts)
    state.knowledge.upsert_tags(conn_id, tag_props)
    for it in items:
        state.knowledge.assign_table_tags(conn_id, it["table"], it["tags"])

    logger.info(
        "[kb.tags] conn=%s 标签提取完成：tables=%s desc=%s new_tags=%s",
        conn_id, len(items), len(desc_drafts), len(tag_props),
    )
    return {
        "tables": len(items),
        "descriptions": len(desc_drafts),
        "new_tags": len(tag_props),
        "library_size": len(state.knowledge.tags(conn_id)["library"]),
    }


# ---------- 枚举取值字典生成（KC3） ----------

ENUM_MAX_VALUES = 50  # 单列去重取值超过此数视为非枚举（长文本/主键），不抽


def _distinct_enum_values(samples: dict[str, list[Any]], table: str, column: str) -> list[str]:
    vals = samples.get(table, {}).get(column) if samples else None
    if not vals:
        return []
    seen: list[str] = []
    for v in vals:
        if v is None:
            continue
        s = str(v)
        if s not in seen:
            seen.append(s)
        if len(seen) >= ENUM_MAX_VALUES:
            break
    return seen


def _mock_enums(schema: dict[str, Any], samples: dict[str, dict[str, list[Any]]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for c in schema["columns"]:
        table, column = c["table"], c["name"]
        distinct = _distinct_enum_values(samples, table, column)
        if not (2 <= len(distinct) <= ENUM_MAX_VALUES):
            continue
        items.append({
            "table": table, "column": column,
            "entries": [{"value": v, "meaning": f"{v}（{column} 的枚举取值，业务含义待确认）"} for v in distinct],
        })
    return items


def _parse_enum_items(text: str) -> list[dict[str, Any]]:
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
        if not isinstance(it, dict) or not it.get("table") or not it.get("column"):
            continue
        entries = it.get("entries") or []
        if not isinstance(entries, list):
            continue
        clean = [{"value": str(e.get("value", "")), "meaning": str(e.get("meaning", "")).strip()}
                 for e in entries if isinstance(e, dict) and e.get("value") not in (None, "")]
        if clean:
            out.append({"table": it["table"], "column": it["column"], "entries": clean})
    return out


async def annotate_enums_core(
    state: "AppState",
    conn_id: str,
    schema: dict[str, Any],
    samples: dict[str, dict[str, list[Any]]],
) -> dict[str, Any]:
    """枚举抽取核心：调用方已备好 schema/samples。

    授权门控在调用方（build 流水线按 include_samples 决定是否调用本函数）。
    枚举解释必须发送去重取值才有意义；按用户决策不做列级过滤，出网唯一防护是
    truncate_samples 值级截断（60 字符）。截断副本贯穿候选判定/mock/prompt/入库
    四路，保证字典键与发送内容一致。mock 网关时生成确定性占位含义。
    """
    from app.knowledge.ddl_context import truncate_samples  # noqa: PLC0415
    safe = truncate_samples(samples)

    enum_cols = [
        c for c in schema.get("columns", [])
        if 2 <= len(_distinct_enum_values(safe, c["table"], c["name"])) <= ENUM_MAX_VALUES
    ]
    if not enum_cols:
        logger.info("[kb.enums] conn=%s 无符合枚举特征的列（取值需 2~%s 个），零产出", conn_id, ENUM_MAX_VALUES)
        return {"columns": 0, "entries": 0, "added": 0}
    logger.info("[kb.enums] conn=%s 开始枚举抽取：候选列=%s", conn_id, len(enum_cols))

    rt = state.runtime.get()
    provider_cfg = rt.provider_config()
    if gw.is_effective_mock(provider_cfg):
        logger.debug("[kb.enums] conn=%s mock 占位枚举", conn_id)
        items = _mock_enums(schema, safe)
    else:
        col_lines = []
        for c in enum_cols:
            distinct = _distinct_enum_values(safe, c["table"], c["name"])
            col_lines.append(f"- {c['table']}.{c['name']}（类型 {c.get('type','')}）取值样本: {distinct}")
        prompt = (
            "你是数据库知识构建助手。下面给出若干列及其出现的取值样本。请为【每个取值】给出简短中文业务含义。\n"
            "返回 JSON 数组，元素形如 "
            "{\"table\":\"表名\",\"column\":\"列名\",\"entries\":[{\"value\":\"取值\",\"meaning\":\"中文含义\"}]}。\n"
            "只返回 JSON，不要多余文字。\n" + "\n".join(col_lines)
        )
        provider = gw.build_provider(provider_cfg)
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        items = _parse_enum_items(resp.content or "")
        if not items:
            logger.warning("[kb.enums] conn=%s LLM 返回解析为空（枚举字典，候选列=%s）", conn_id, len(enum_cols))

    added = state.knowledge.annotate_enums(conn_id, items)
    logger.info("[kb.enums] conn=%s 枚举抽取完成：columns=%s added=%s", conn_id, len(items), added)
    return {"columns": len(items), "entries": added, "added": added}


async def annotate_enums(state: "AppState", conn_id: str) -> dict[str, Any]:
    """兼容包装：自行取 schema + 采样后调核心函数（供既有独立 API 端点使用）。"""
    schema = await get_schema(state, conn_id)
    rt = state.runtime.get()
    samples: dict[str, dict[str, list[Any]]] = {}
    if rt.kb_sample_rows > 0:
        for t in schema["tables"]:
            try:
                samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
            except Exception:
                samples[t["name"]] = {}
    return await annotate_enums_core(state, conn_id, schema, samples)


# ---------- LLM 图谱识别（两轮：全局扫描 + 候选验证） ----------


def _generate_candidate_pairs(schema: dict[str, Any]) -> list[dict[str, str]]:
    """程序启发式：根据列名匹配 + 类型兼容，生成可能有关联的表对候选。

    策略：A 表某列名（去掉 _id 后缀）≈ B 表名，或两表有同名非通用列且类型兼容。
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
                key = (pair[0], pair[1], c["name"], ref_col)
                if key not in seen_pairs:
                    candidates.append({
                        "from_table": tname, "from_col": c["name"],
                        "to_table": base, "to_col": ref_col,
                        "reason": f"列名匹配：{tname}.{c['name']} → {base}.id",
                    })
                    seen_pairs.add(key)

    return candidates


def _parse_graph_edges(text: str) -> list[dict[str, Any]]:
    """解析 LLM 返回的关系 JSON 数组。"""
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
        if not isinstance(it, dict):
            continue
        from_t = str(it.get("from_table") or it.get("from") or "").strip()
        to_t = str(it.get("to_table") or it.get("to") or "").strip()
        from_c = str(it.get("from_col") or "").strip()
        to_c = str(it.get("to_col") or "").strip()
        if not from_t or not to_t:
            continue
        out.append({
            "from_table": from_t, "from_col": from_c or None,
            "to_table": to_t, "to_col": to_c or None,
            "reason": str(it.get("reason") or ""),
        })
    return out


def _mock_graph_edges(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """mock：基于外键生成确定性图谱边。"""
    edges: list[dict[str, Any]] = []
    for fk in schema.get("foreign_keys", []):
        edges.append({
            "from_table": fk["table"], "from_col": fk["column"],
            "to_table": fk["ref_table"], "to_col": fk["ref_column"],
            "reason": f"FK 约束：{fk['table']}.{fk['column']} → {fk['ref_table']}.{fk['ref_column']}",
        })
    return edges


async def annotate_graph(
    state: "AppState",
    conn_id: str,
    graph_overview: str,
    schema: dict[str, Any],
    on_progress: Any | None = None,
) -> list[dict[str, Any]]:
    """LLM 图谱识别：两轮调用（全局扫描 + 候选验证），返回 draft 边列表。

    graph_overview: 噪音列已过滤的精简表结构概览（build_graph_overview 生成）。
    schema: 完整 schema，用于生成候选对。
    on_progress: 阶段三「AI 关系识别」进度回调（第一轮全局扫描 0→50，第二轮候选验证 50→100）。
    返回值未落库，由调用方统一写入图谱 draft。
    """
    rt = state.runtime.get()
    provider_cfg = rt.provider_config()
    logger.info(
        "[kb.graph] conn=%s 开始关系识别：tables=%s fks=%s",
        conn_id, len(schema.get("tables", [])), len(schema.get("foreign_keys", [])),
    )
    if gw.is_effective_mock(provider_cfg):
        logger.debug("[kb.graph] conn=%s mock FK 边", conn_id)
        if on_progress:
            on_progress("AI 关系识别", 100, None, phase="graph")
        return _mock_graph_edges(schema)

    provider = gw.build_provider(provider_cfg)

    # ---- 第一轮：全局扫描（广度优先） ----
    global_prompt = (
        "你是数据库关系分析专家。以下是某个数据库的全部表结构，请判断哪些表之间存在业务关联。\n"
        "注意：\n"
        "- 不限于同名列匹配，注意语义关联（如 status 字段可能有枚举含义）\n"
        "- 可能存在复合关联（如 order_items 同时关联 orders 和 products）\n"
        "- 注意区分：真正的业务关联 vs 仅仅是同名字段\n"
        "- 返回 confident（高确信）和 uncertain（低确信）两类\n\n"
        "【表结构】\n" + graph_overview + "\n\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A", "from_col":"a_id", "to_table":"B", "to_col":"id", '
        '"confidence":"high/medium/low", "reason":"一句话说明关联依据"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    global_resp = await provider.chat(
        [{"role": "user", "content": global_prompt}], tools=None,
    )
    global_edges = _parse_graph_edges(global_resp.content or "")
    if not global_edges:
        logger.warning("[kb.graph] conn=%s LLM 返回解析为空（关系识别·全局扫描）", conn_id)
    logger.debug("[kb.graph] conn=%s 全局扫描边数=%s", conn_id, len(global_edges))
    # 标记来源
    for e in global_edges:
        e["source"] = "llm_global"
    if on_progress:
        on_progress("AI 关系识别", 50, None, phase="graph")

    # ---- 第二轮：候选验证（精确度优先） ----
    candidates = _generate_candidate_pairs(schema)
    if not candidates:
        logger.info("[kb.graph] conn=%s 无程序候选对，仅用全局扫描边 %s 条", conn_id, len(global_edges))
        return global_edges

    # 标记已被全局扫描覆盖的候选对
    global_keys = {
        (e["from_table"], e["to_table"], e.get("from_col"), e.get("to_col"))
        for e in global_edges
    }
    confirmed_refs = []
    unverified = []
    for c in candidates:
        pair_key = (c["from_table"], c["to_table"], c["from_col"], c["to_col"])
        rev_key = (c["to_table"], c["from_table"], c["to_col"], c["from_col"])
        if pair_key in global_keys or rev_key in global_keys:
            confirmed_refs.append(c)
        else:
            unverified.append(c)

    if not unverified:
        logger.info("[kb.graph] conn=%s 候选对均已被全局扫描覆盖，边 %s 条", conn_id, len(global_edges))
        return global_edges

    # 构建待验证候选的上下文（只给相关表的结构）
    related_tables = set()
    for c in unverified:
        related_tables.add(c["from_table"])
        related_tables.add(c["to_table"])

    verify_lines: list[str] = []
    for c in unverified:
        verify_lines.append(
            f"- {c['from_table']}.{c['from_col']} → {c['to_table']}.{c['to_col']}"
            f"  （依据：{c['reason']}）"
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
        "你是数据库关系分析专家。以下是程序通过列名匹配发现的候选关系。\n"
        "请判断每个候选是否成立。若关联字段不正确，请给出修正后的字段。\n"
        "拒绝无实际业务关联的候选（如仅仅是命名巧合）。\n\n"
        "【相关表结构】\n" + "\n".join(rel_schema_lines) + "\n\n"
        "【待验证候选关系】\n" + "\n".join(verify_lines) + "\n\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A", "from_col":"a_id", "to_table":"B", "to_col":"id", '
        '"confidence":"high/medium/low", "reason":"说明", "status":"confirmed/rejected"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    verify_resp = await provider.chat(
        [{"role": "user", "content": verify_prompt}], tools=None,
    )
    verified_edges = _parse_graph_edges(verify_resp.content or "")
    if not verified_edges:
        logger.warning("[kb.graph] conn=%s LLM 返回解析为空（关系识别·候选验证 %s 条）", conn_id, len(unverified))
    for e in verified_edges:
        e["source"] = "llm_verify"
        # 如果 LLM 返回了 status=rejected，过滤掉
        if isinstance(e, dict) and e.get("status") == "rejected":
            continue
    if on_progress:
        on_progress("AI 关系识别", 100, None, phase="graph")

    # 合并两轮结果（全局扫描 + 候选验证，去重）
    all_edges = global_edges + [e for e in verified_edges if e.get("status") != "rejected"]
    logger.info("[kb.graph] conn=%s 关系识别完成：global=%s verified=%s total=%s",
                conn_id, len(global_edges), len(verified_edges), len(all_edges))
    return all_edges
