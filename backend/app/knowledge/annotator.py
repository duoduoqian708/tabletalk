"""AI 自动注释：读 schema（+可选样本）→ 模型 → 中文注释草案（draft）。

隐私：默认不把样本值发给模型（kb_ai_annotation_samples 门控）；mock 网关时生成
确定性伪注释，保证无 key 也能跑通管线。
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from app.ai import gateway as gw
from app.core.schema import get_schema, sample_values

if TYPE_CHECKING:
    from app.state import AppState


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


async def annotate_knowledge(
    state: "AppState",
    conn_id: str,
    include_samples: bool | None = None,
) -> dict[str, Any]:
    schema = await get_schema(state, conn_id)
    rt = state.runtime.get()
    if include_samples is None:
        include_samples = rt.kb_ai_annotation_samples

    samples: dict[str, dict[str, list[Any]]] = {}
    if include_samples and rt.kb_sample_rows > 0:
        for t in schema["tables"]:
            try:
                samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
            except Exception:
                samples[t["name"]] = {}

    provider_cfg = rt.provider_config()
    if gw.is_effective_mock(provider_cfg):
        items = _mock_comments(schema, samples)
    else:
        prompt = (
            "你是数据库知识构建助手。为下面 schema 中【没有注释】的表和列生成简短中文注释。\n"
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


async def annotate_domain(state: "AppState", conn_id: str) -> dict[str, Any]:
    """AI 生成逐表描述 + 领域标签（约束在现有标签库内复用，新标签提草案）。写入知识库。"""
    schema = await get_schema(state, conn_id)
    lib = state.knowledge.tags(conn_id)["library"]
    existing = {t["name"]: t for t in lib}
    existing_names = sorted(existing.keys())
    existing_desc = "\n".join(
        f"- {n}（{existing[n].get('description','')}，{existing[n].get('status','draft')}）" for n in existing_names
    ) or "（暂无）"

    rt = state.runtime.get()
    if gw.is_effective_mock(rt.provider_config()):
        items = _mock_domain_tags(schema)
    else:
        table_desc = "\n".join(
            f"- {t['name']}（{t.get('comment','')}，{t.get('column_count',0)} 列）"
            for t in schema["tables"]
        )
        prompt = (
            "你是数据库知识构建助手。为下面的每张表生成：①一句话中文描述；②1~3 个中文领域标签。\n"
            "标签规则：优先从【已有标签】中复用，确有新领域才提新标签（新标签会走人工确认）。\n"
            "已有标签：\n" + existing_desc + "\n表清单：\n" + table_desc + "\n"
            "返回 JSON 数组，元素形如 {\"table\":\"表名\",\"description\":\"中文描述\",\"tags\":[\"标签1\",\"标签2\"]}。\n"
            "只返回 JSON，不要多余文字。"
        )
        provider = gw.build_provider(rt.provider_config())
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        items = _parse_domain_items(resp.content or "")

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

    return {
        "tables": len(items),
        "descriptions": len(desc_drafts),
        "new_tags": len(tag_props),
        "library_size": len(state.knowledge.tags(conn_id)["library"]),
    }
