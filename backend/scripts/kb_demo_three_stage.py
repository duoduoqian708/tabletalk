"""KB 构建三阶段示例测试（直连配置的 LLM，不改生产代码）。

按用户要求跑「三阶段」完整示例：
  阶段一 · 逐表注释（有采样/无采样两版对照）
  阶段二 · 领域标签（批量划分 + 审校式自检）
  阶段三 · 图谱（全局扫描 + 候选验证）

新画像格式（本次验证的核心）：
  - 保留 DDL 结构（剔除噪音列 + 类型长度噪声）
  - 回填 description（库注释 / 阶段一 AI 注释）
  - 取值示例熔进描述文本（不是独立批注）：`描述：订单状态。取值示例：paid，pending...`
  - 不再叫 common，统一 description。

用法：cd backend && .venv/bin/python scripts/kb_demo_three_stage.py
输出：docs/kb-three-stage-demo.md
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import gateway as gw                      # noqa: E402
from app.core.schema import get_schema, sample_values  # noqa: E402
from app.state import _build_state                      # noqa: E402

from app.knowledge.annotator import (                  # noqa: E402
    _annotation_prompt_sampled,
    _annotation_prompt_unsampled,
    _distinct_values,
    _first_example,
    _generate_candidate_pairs,
    _domain_partition_prompt,
    _domain_selfcheck_prompt,
    _parse_domain_payload,
    _normalize_domains,
    _parse_graph_edges,
)
from app.knowledge.ddl_context import is_noise_column   # noqa: E402

# 示例测试库：订单/明细/商品/客户/退货——覆盖 FK 引用、枚举列、噪音列
TABLES = ["orders", "order_items", "products", "customers", "returns"]


# ---- 生产同款：表级 DDL 快照合成（标注 prompt 的 DDL 段） ----
def _table_ddl(schema, table: str) -> str:
    cols = [c for c in schema["columns"] if c["table"] == table]
    fks = [f for f in schema["foreign_keys"] if f["table"] == table]
    lines = [f"CREATE TABLE {table} ("]
    defs = []
    for c in cols:
        parts = [c["name"], c.get("type") or "TEXT"]
        if c.get("pk"):
            parts.append("PRIMARY KEY")
        if c.get("comment"):
            parts.append(f"/* {c['comment']} */")
        defs.append("  " + " ".join(parts))
    for fk in fks:
        defs.append(
            f"  CONSTRAINT fk_{table}_{fk['column']} FOREIGN KEY ({fk['column']}) "
            f"REFERENCES {fk['ref_table']} ({fk['ref_column']})"
        )
    lines.append(",\n".join(defs))
    lines.append(");")
    return "\n".join(lines)


# ---- 类型噪声裁剪：去长度/精度，留可读类型 ----
def _clean_type(t: str) -> str:
    u = (t or "TEXT").upper()
    if any(u.startswith(k) for k in ("VARCHAR", "CHAR", "INT", "DECIMAL", "NUMERIC",
                                     "FLOAT", "DOUBLE", "REAL", "BIGINT", "SMALLINT", "TINYINT")):
        u = re.sub(r"\s*\(.*?\)\s*$", "", u)
    return u


_TRUE_LONG = ("CLOB", "BLOB", "LONGTEXT", "MEDIUMTEXT", "BYTEA", "JSON")


def _is_portrait_noise(name: str, ctype: str) -> bool:
    """噪音列判据：列名级语义噪音（时间戳/审计/软删）+ 真长文本类型。
    绝不用类型前缀宽判——`status TEXT` 是业务枚举，不是长文本。"""
    if is_noise_column(name):
        return True
    return (ctype or "").upper().startswith(_TRUE_LONG)


# ---- 新画像：DDL 结构 + 描述回填 + 取值示例熔入描述 ----
def _build_ddl_portrait(
    schema,
    table: str,
    table_desc: str,
    col_desc: dict[str, str],
    samples: dict[str, list] | None,
) -> str:
    """单表 DDL 画像。取值示例是描述文本的一部分，不是独立批注；
    FK 列不给取值示例（语义在其他表）；高基数列给单个示例；TEXT 低基数给取值集。"""
    cols = [c for c in schema["columns"] if c["table"] == table]
    fks = [f for f in schema["foreign_keys"] if f["table"] == table]
    fk_refs: dict[str, list[str]] = {}
    for fk in fks:
        fk_refs.setdefault(fk["column"], []).append(f"{fk['ref_table']}.{fk['ref_column']}")

    lines = [f"CREATE TABLE {table} ("]
    col_lines = []
    for c in cols:
        name = c["name"]
        is_pk = bool(c.get("pk"))
        if not is_pk and not fk_refs.get(name) and _is_portrait_noise(name, c.get("type", "")):
            continue
        t = _clean_type(c.get("type", ""))
        if fk_refs.get(name) and not t:
            t = "INT"
        body = f"  {name:<24} {t:<10}"
        if is_pk:
            body += " PRIMARY KEY"
        if fk_refs.get(name):
            body += f"  [FK->{';'.join(fk_refs[name])}]"
        # 描述 + 取值示例熔一句
        d = col_desc.get(name, "").strip()
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
        if d:
            parts.append("描述：" + d)
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


def _library_portraits(schema, tables, table_desc, col_desc, samples) -> str:
    return "\n\n".join(
        _build_ddl_portrait(schema, t, table_desc.get(t, ""), col_desc.get(t, {}), samples.get(t))
        for t in tables
    )


# ---- LLM 直呼 + 完整记录 ----
async def _call(cfg, conn_id, prompt, tag: str) -> tuple[dict, str]:
    provider = gw.build_provider(cfg)
    resp = await provider.chat(
        [{"role": "user", "content": prompt}], tools=None,
        ctx={"conn_id": conn_id, "connection": conn_id, "skill": "kb-demo3",
             "source": "kb_demo3", "status": tag},
    )
    return resp.usage or {}, resp.content or ""


def _req_body(prompt: str) -> str:
    """完整入参（用户消息 + 发送参数：temperature/reasoning/model）打到文档。"""
    return f"```text\n{prompt}\n```"


def _resp_block(content: str) -> str:
    if content.startswith("```"):
        return content + "\n"
    try:
        json.loads(content)
        fence = "json"
    except Exception:
        fence = "text"
    return f"```{fence}\n{content}\n```"


def main() -> None:
    nofk = "--nofk" in sys.argv  # LLM 为主路径验证：清空 FK，上下文不写 FK，看标签/图谱裸奔
    state = _build_state()
    conn_id, conn_name, dialect = _pick_conn(state)
    rt = state.runtime.get()
    cfg = rt.provider_config()
    mode = "REAL" if not gw.is_effective_mock(cfg) else "MOCK"

    schema = asyncio.run(get_schema(state, conn_id, refresh=True))
    if nofk:
        schema["foreign_keys"] = []  # 模拟无 FK 产出：FK 为可选上下文，没有就不写
    all_tables = [t["name"] for t in schema["tables"]]
    missing = [t for t in TABLES if t not in all_tables]
    if missing:
        raise SystemExit(f"表不存在: {missing}。全库: {all_tables}")

    # 授权抽样（阶段一/二/三的取值示例与 values 来源）
    samples: dict[str, dict[str, list]] = {}
    for t in TABLES:
        samples[t] = asyncio.run(sample_values(state, conn_id, t, rt.kb_sample_rows))

    out: list[str] = []
    out.append(f"# KB 构建三阶段示例测试（新画像 + {'无 FK' if nofk else '有 FK'}）\n")
    out.append(f"- 日期: 2026-08-25")
    out.append(f"- 连接: {conn_name} (`{conn_id}`), dialect={dialect}, mode={mode}")
    out.append(f"- 表集: {', '.join(TABLES)}")
    out.append(f"- model: {cfg.get('model')}")
    out.append(f"- FK: {'已清空（模拟无外键库，上下文不写 FK）' if nofk else '真实外键注入上下文'}")
    out.append(f"- kb_sample_rows: {rt.kb_sample_rows}（授权 → 描述含取值示例）\n")

    # ============================================================
    # 阶段一 · 逐表注释（生产标注模板：DDL + 样本取值）
    # ============================================================
    out.append(f"\n## 阶段一 · 逐表注释\n")
    stage1_col_desc: dict[str, dict[str, str]] = {}
    stage1_table_desc: dict[str, str] = {}
    for i, t in enumerate(TABLES, 1):
        ddl = _table_ddl(schema, t)
        samp = samples[t]
        db_tdesc = next((x.get("comment", "") for x in schema["tables"] if x["name"] == t), "")

        # 有采样版
        srows = "\n".join(f"- {c}: {v}" for c, v in samp.items())
        p_sampled = _annotation_prompt_sampled(
            ddl, f"【样本取值（真实数据；用于辅助理解字段含义）】\n{srows}"
        )
        usage, content = asyncio.run(_call(cfg, conn_id, p_sampled, "demo3-annotate-sampled"))
        out.append(f"\n### {t} · 有采样（取数据）\n")
        out.append(f"**入参（完整 prompt）：**\n{_req_body(p_sampled)}\n")
        out.append(f"**返回：**\n{_resp_block(content)}\n")
        out.append(f"**usage:** `{json.dumps(usage, ensure_ascii=False)}`\n")

        # 解析：回填描述（给阶段二三的画像）
        try:
            items = json.loads(content.strip().lstrip("`"))
        except Exception:
            items = []
        if isinstance(items, list):
            for it in items:
                if it.get("column"):
                    stage1_col_desc.setdefault(t, {})[it["column"]] = it.get("comment", "")
                elif it.get("comment"):
                    stage1_table_desc[t] = it.get("comment", "")
        else:
            stage1_table_desc[t] = db_tdesc

        # 无采样版（对照）
        p_unsamp = _annotation_prompt_unsampled(ddl)
        usage, content = asyncio.run(_call(cfg, conn_id, p_unsamp, "demo3-annotate-unsampled"))
        out.append(f"\n### {t} · 无采样（不取数据）\n")
        out.append(f"**入参（完整 prompt）：**\n{_req_body(p_unsamp)}\n")
        out.append(f"**返回：**\n{_resp_block(content)}\n")
        out.append(f"**usage:** `{json.dumps(usage, ensure_ascii=False)}`\n")

    # ============================================================
    # 阶段二 · 领域标签（新画像上下文 + 批量划分 + 自检）
    # ============================================================
    out.append(f"\n## 阶段二 · 领域标签\n")
    lib_desc = {t: stage1_table_desc.get(t, "") for t in TABLES}
    lib_cd = {t: stage1_col_desc.get(t, {}) for t in TABLES}
    portraits = _library_portraits(schema, TABLES, lib_desc, lib_cd, samples)

    n = len(TABLES)
    target = max(3, round(__import__("math").sqrt(n)))
    lo, hi = max(2, target - 1), target + 1

    existing_desc = "（暂无）"
    p_part = _domain_partition_prompt(portraits, existing_desc, lo, hi, n)
    out.append(f"\n### 画像（新格式，阶段二上下文）\n")
    out.append(f"```sql\n{portraits}\n```\n")
    out.append(f"\n### 第一轮 · 批量划分\n")
    out.append(f"**入参（完整 prompt）：**\n{_req_body(p_part)}\n")
    usage, content = asyncio.run(_call(cfg, conn_id, p_part, "demo3-tags-partition"))
    out.append(f"**返回：**\n{_resp_block(content)}\n")
    out.append(f"**usage:** `{json.dumps(usage, ensure_ascii=False)}`\n")

    _, domains = _parse_domain_payload(content)
    domains = _normalize_domains(domains, TABLES) if domains else []

    p_chk = _domain_selfcheck_prompt(portraits, domains, lo, hi)
    out.append(f"\n### 第二轮 · 审校式自检\n")
    out.append(f"**入参（完整 prompt）：**\n{_req_body(p_chk)}\n")
    usage, content = asyncio.run(_call(cfg, conn_id, p_chk, "demo3-tags-selfcheck"))
    out.append(f"**返回：**\n{_resp_block(content)}\n")
    out.append(f"**usage:** `{json.dumps(usage, ensure_ascii=False)}`\n")
    unchanged, revised = _parse_domain_payload(content)
    final_domains = _normalize_domains(revised, TABLES) if (not unchanged and revised) else domains
    final_domains = _normalize_domains(final_domains, TABLES)
    out.append(f"\n**最终领域（{'保留初版' if unchanged else '采纳修正版'}）：**\n")
    out.append("```json\n" + json.dumps(final_domains, ensure_ascii=False, indent=1) + "\n```\n")

    # ============================================================
    # 阶段三 · 图谱（新画像上下文 + 全局扫描 + 候选验证）
    # ============================================================
    out.append(f"\n## 阶段三 · 图谱\n")
    # 全局扫描：用新画像作表结构上下文
    g_graph_overview = portraits
    g_prompt = (
        "你是数据库关系分析专家。以下是某个数据库的全部表结构，请判断哪些表之间存在业务关联。\n"
        "注意：\n"
        "- 不限于同名列匹配，注意语义关联\n"
        "- 可能存在复合关联（如 order_items 同时关联 orders 和 products）\n"
        "- 注意区分：真正的业务关联 vs 仅仅是同名字段\n"
        "- 返回 confident（高确信）和 uncertain（低确信）两类\n"
        "方向规则：from_table 必须是多侧（明细/子表一侧，持有引用字段），to_table 是一侧（主表/父表）；"
        "from_col 为 from_table 中引用 to_table 的字段。\n"
        "基数规则：cardinality 只能取 n:1 或 1:1（不确定取 n:1；from_col 为 from_table 主键时只能 1:1）。\n"
        "禁止 n:m 直连边：多对多必须通过中间表拆成两条 n:1 边。\n\n"
        "【表结构】\n" + g_graph_overview + "\n\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id",'
        '"cardinality":"n:1","confidence":"high/medium/low","reason":"一句话说明"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    out.append(f"\n### 第一轮 · 全局扫描\n")
    out.append(f"**入参（完整 prompt）：**\n{_req_body(g_prompt)}\n")
    usage, content = asyncio.run(_call(cfg, conn_id, g_prompt, "demo3-graph-global"))
    out.append(f"**返回：**\n{_resp_block(content)}\n")
    out.append(f"**usage:** `{json.dumps(usage, ensure_ascii=False)}`\n")
    global_edges = _parse_graph_edges(content, schema)
    for e in global_edges:
        e["source"] = "llm_global"

    # 候选验证：程序候选 ∪ 首轮低置信 LLM 边
    candidates = _generate_candidate_pairs(schema)
    cand_in_lib = [c for c in candidates if c["from_table"] in TABLES and c["to_table"] in TABLES]
    global_keys = {(e["from_table"], e["to_table"], e.get("from_col"), e.get("to_col")) for e in global_edges}
    rev_keys = {(e["to_table"], e["from_table"], e.get("to_col"), e.get("from_col")) for e in global_edges}
    pool, seen = [], set()
    for c in cand_in_lib:
        k = (c["from_table"], c["to_table"], c["from_col"], c["to_col"])
        if k not in global_keys and k not in rev_keys:
            key = (c["from_table"], c.get("from_col"), c["to_table"], c.get("to_col"))
            if key not in seen:
                seen.add(key)
                pool.append(c)
    for e in global_edges:
        if e.get("confidence") != "high":
            key = (e["from_table"], e.get("from_col"), e["to_table"], e.get("to_col"))
            if key not in seen:
                seen.add(key)
                pool.append(e)

    verify_lines = [
        f"- {c['from_table']}.{c.get('from_col')} → {c['to_table']}.{c.get('to_col')}"
        f"  （{c.get('cardinality','n:1')}，来源：{'全局扫描低置信' if c.get('source')=='llm_global' else '程序列名匹配'}"
        f"，依据：{c.get('reason','')}）"
        for c in pool
    ]
    rel_lines = []
    for t in TABLES:
        cols = [c for c in schema["columns"] if c["table"] == t]
        col_txt = ", ".join(
            f"{c['name']}({c.get('type','')}{' PK' if c.get('pk') else ''})" for c in cols
        )
        rel_lines.append(f"- {t}: {col_txt}")

    v_prompt = (
        "你是数据库关系分析专家。以下是候选关系（来自程序启发式列名匹配，以及全局扫描中置信度较低的发现）。\n"
        "请仅对列出的每一项裁决：成立 → status=confirmed（可修正关联字段/基数）；不成立 → status=rejected。\n"
        "不得新增列表之外的关系；拒绝无实际业务关联的候选（如仅仅命名巧合）。\n"
        "方向规则：from_table 必须是多侧，to_table 是一侧。\n"
        "基数规则：cardinality 只能取 n:1 或 1:1（from_col 为 from_table 主键时只能 1:1）。\n\n"
        "【相关表结构】\n" + "\n".join(rel_lines) + "\n\n"
        "【待裁决候选关系】\n" + "\n".join(verify_lines) + "\n\n"
        "返回 JSON 数组，元素形如 "
        '{"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id",'
        '"cardinality":"n:1","confidence":"high/medium/low","reason":"说明","status":"confirmed/rejected"}。\n'
        "只返回 JSON，不要多余文字。"
    )
    out.append(f"\n### 第二轮 · 候选验证（待裁决 {len(pool)} 条）\n")
    out.append(f"**入参（完整 prompt）：**\n{_req_body(v_prompt)}\n")
    usage, content = asyncio.run(_call(cfg, conn_id, v_prompt, "demo3-graph-verify"))
    out.append(f"**返回：**\n{_resp_block(content)}\n")
    out.append(f"**usage:** `{json.dumps(usage, ensure_ascii=False)}`\n")
    verified = [e for e in _parse_graph_edges(content, schema) if e.get("status") != "rejected"]
    out.append(f"\n**阶段三最终边（全局高置信 ∪ 验证通过）：**\n")
    base_global = [e for e in global_edges if e.get("confidence") == "high"]
    all_edges = base_global + verified
    out.append("```json\n" + json.dumps(all_edges, ensure_ascii=False, indent=1) + "\n```\n")

    doc_path = Path(__file__).resolve().parent.parent.parent / "docs" / "kb-three-stage-demo.md"
    doc_path.write_text("\n".join(out), encoding="utf-8")
    print(f"✅ 已写入 {doc_path}")
    # SQLite 池线程挂起 → 强制退出
    os._exit(0)


def _pick_conn(state):
    conns = list(state.connections.list())
    if not conns:
        raise SystemExit("没有可用连接")
    return conns[0].id, conns[0].name, conns[0].dialect


if __name__ == "__main__":
    main()