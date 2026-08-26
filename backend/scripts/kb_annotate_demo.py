"""KB 逐表标注最小验证 demo v2（不改生产代码，直连配置的 LLM 接口）。

用户三轮迭代后的目标格式：
1. 画像字段命名：不再叫 common（易与 DDL 的 COMMENT 混淆），改叫 description（描述）。
2. 画像要保留 DDL 结构：剔除噪音列 + 剔除类型长度/必填等噪声，回填我们的 description，
   字段描述在有采样时追加「取值示例：xxx」。
3. 完整跑三步并输出文档：
   ① 新画像（DDL 结构 + description + 取值示例）
   ② 有采样标注（取数据）
   ③ 无采样标注（不取数据）

用法：cd backend && .venv/bin/python scripts/kb_annotate_demo.py [表名]
默认表名 = orders。走真实/已配置 provider（deepseek-v4-flash），每次运行产生 2 次真实 LLM 调用。
"""
from __future__ import annotations

import asyncio
import json
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
)
from app.knowledge.ddl_context import is_noise_column   # noqa: E402


def _pick_conn(state) -> tuple[str, str, str]:
    conns = list(state.connections.list())
    if not conns:
        raise SystemExit("没有可用连接")
    pick = conns[0]
    return pick.id, pick.name, pick.dialect


def _table_ddl_snapshot(schema, table: str) -> str:
    """从结构快照合成单表 CREATE TABLE（demo 用；真构建走 generate_ddls_all 的实时 DDL）。
    仅用于标注 prompt 的 DDL 段，画像是另一套精简结构（见 _build_ddl_portrait）。"""
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


# ---- 类型噪声裁剪：去掉长度/精度/必填等对语义无用的细节，保留可读类型 ----
_TYPE_KEEP = ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "REAL", "DOUBLE",
              "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "DATETIME",
              "VARCHAR", "CHAR", "TEXT", "JSON", "BLOB", "DECIMAL", "NUMERIC")
_TYPE_STRIP = re.compile(r"\(\s*\d+(?:\s*,\s*\d+)?\s*\)")


def _clean_type(t: str) -> str:
    """类型去噪声：VARCHAR(255) → VARCHAR；NUMERIC(10,2) → NUMERIC；NOT NULL 等约束不归属类型。"""
    u = (t or "TEXT").upper()
    if any(u.startswith(k) for k in ("VARCHAR", "CHAR", "INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL", "BIGINT", "SMALLINT", "TINYINT")):
        u = re.sub(r"\s*\(.*?\)\s*$", "", u)
    return u


# ---- 真·长文本/二进制类型（无解析价值，画像里滤掉；TEXT 不算——status/type 常为 TEXT）----
_TRUE_LONG = ("CLOB", "BLOB", "LONGTEXT", "MEDIUMTEXT", "BYTEA", "JSON")


def _is_portrait_noise(name: str, ctype: str) -> bool:
    """画像层面的噪音列：只按「噪音列名」剔除（时间戳/审计人/软删标记），
    并额外滤掉真·长文本/二进制类型。绝不用类型前缀宽判——`status TEXT` 是核心业务列，
    不能因为 TEXT 前缀就被当长文本滤掉。"""
    if is_noise_column(name):  # 仅列名判据（不传类型）
        return True
    t = (ctype or "").upper()
    return t.startswith(_TRUE_LONG)


# ---- 新画像：DDL 结构 + description + 取值示例（common → description 命名） ----
def _build_ddl_portrait(
    schema,
    table: str,
    table_desc: str,                       # 表级描述（已确认/库注释/AI 草案回填）
    col_desc: dict[str, str],              # 列名 → 描述（AI 注释/库注释回填）
    samples: dict[str, list] | None,       # 授权样本（None=未授权 → 无取值示例）
) -> str:
    """单表 DDL 画像：保留 DDL 结构骨架，剔除噪音列 + 类型噪声，回填我们的 description，
    字段在有采样时追加『取值示例：…』。column 级 description 优先列级 AI 注释/库注释。"""
    cols = [c for c in schema["columns"] if c["table"] == table]
    fks = [f for f in schema["foreign_keys"] if f["table"] == table]
    # FK 引用映射：列名 → [被引用的表.列]
    fk_refs: dict[str, list[str]] = {}
    for fk in fks:
        fk_refs.setdefault(fk["column"], []).append(f"{fk['ref_table']}.{fk['ref_column']}")

    lines = [f"CREATE TABLE {table} ("]
    col_lines = []
    for c in cols:
        name = c["name"]
        # 噪音剔除（列名级）；主/外键永远保留
        if not c.get("pk") and not fk_refs.get(name) and _is_portrait_noise(name, c.get("type", "")):
            continue
        # 类型去噪声：只裁长度/精度，保留可读类型
        t = _clean_type(c.get("type", ""))
        if fk_refs.get(name):
            t = t or "INT"
        # 回填 description
        d = col_desc.get(name, "").strip()
        body = f"  {name:<22} {t:<12}"
        if c.get("pk"):
            body = f"  {name:<22} {t:<12}  PRIMARY KEY"
        if d:
            body += f"  -- 描述：{d}"
        # FK 尾注（引用另一张表，语义在其他表——不给取值示例，避免诱导编造）
        if fk_refs.get(name):
            body += f"  [FK->{';'.join(fk_refs[name])}]"
        # 取值示例：仅授权采样时；FK 列不给（语义在其他表，避免诱导编造）
        if samples is not None and not fk_refs.get(name):
            distinct = _distinct_values(samples, name)
            ex = _first_example(samples, name)
            tu = (t or "").upper()
            # 高基数列（数值/时间/主键）→ 单个示例值；TEXT/VARCHAR 低基数离散列 → 取值集
            is_high_card = bool(c.get("pk")) or tu.startswith(
                ("INT", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE", "DATE", "TIME")
            )
            if not is_high_card and 2 <= len(distinct) <= 50:
                ex_txt = "，".join(str(v)[:24] for v in distinct[:8])
                body += f"  取值示例：{ex_txt}"
            elif ex:
                body += f"  取值示例：{ex[:30]}"
        col_lines.append(body.rstrip())
    # FK 约束行（保留真实的 REFERENCES 关系）
    for fk in fks:
        col_lines.append(
            f"  CONSTRAINT fk_{table}_{fk['column']} FOREIGN KEY ({fk['column']}) "
            f"REFERENCES {fk['ref_table']}({fk['ref_column']})"
        )
    lines.append(",\n".join(col_lines))
    lines.append(");")
    ddl = "\n".join(lines)
    if table_desc:
        return f"-- {table}：{table_desc}\n{ddl}"
    return ddl


def _col_desc_from_db(schema, table: str) -> dict[str, str]:
    """库注释回填（demo 里演示；真构建还叠加 AI 草案/已确认）。"""
    return {
        c["name"]: c.get("comment", "") for c in schema["columns"] if c["table"] == table
    }


def main() -> None:
    table = sys.argv[1] if len(sys.argv) > 1 else "orders"
    state = _build_state()
    conn_id, conn_name, dialect = _pick_conn(state)
    rt = state.runtime.get()
    cfg = rt.provider_config()
    print(f"连接: {conn_name} ({conn_id}) dialect={dialect}")
    print(f"provider: {cfg.get('model')} @ {cfg.get('base_url')} (mode={'REAL' if not gw.is_effective_mock(cfg) else 'MOCK'})")
    mode = "REAL" if not gw.is_effective_mock(cfg) else "MOCK"

    schema = asyncio.run(get_schema(state, conn_id, refresh=True))
    tables = [t["name"] for t in schema["tables"]]
    if table not in tables:
        raise SystemExit(f"表 {table} 不存在。可选: {tables}")

    # 抽样（授权演示）+ 无采样对照
    samples_rows = rt.kb_sample_rows
    samples_db = asyncio.run(sample_values(state, conn_id, table, samples_rows))
    samples = {table: samples_db} if samples_db else None
    samples_for = samples[table] if samples else None

    ddl = _table_ddl_snapshot(schema, table)
    col_desc = _col_desc_from_db(schema, table)
    tbl_comment = next((t.get("comment", "") for t in schema["tables"] if t["name"] == table), "")

    # ---------- ① 新画像（DDL 结构 + description + 取值示例） ----------
    portrait_sampled = _build_ddl_portrait(schema, table, tbl_comment, col_desc, samples_for)
    portrait_plain = _build_ddl_portrait(schema, table, tbl_comment, col_desc, None)

    # ---------- ② 有采样标注 ----------
    sample_rows_text = "\n".join(
        f"- {c}: {v}" for c, v in (samples_for or {}).items()
    )
    smp_ref = f"【样本取值（真实数据；用于辅助理解字段含义）】\n{sample_rows_text}" if sample_rows_text else ""
    prompt_sampled = _annotation_prompt_sampled(ddl, smp_ref)
    # ---------- ③ 无采样标注 ----------
    prompt_unsampled = _annotation_prompt_unsampled(ddl)

    out: list[str] = []
    out.append(f"# KB 逐表标注最小验证 demo v2\n")
    out.append(f"- 日期: 2026-08-25")
    out.append(f"- 连接: {conn_name} (`{conn_id}`), dialect={dialect}, mode={mode}")
    out.append(f"- 表: `{table}`  ({len([c for c in schema['columns'] if c['table']==table])} 列)")
    out.append(f"- provider: {cfg.get('model')} @ {cfg.get('base_url')}")
    out.append(f"- kb_sample_rows: {samples_rows}")

    out.append(f"\n## ① 新画像（DDL 结构 + description + 取值示例，common→description）\n")
    out.append("**有采样（取数据）版本：**\n")
    out.append("```sql")
    out.append(portrait_sampled)
    out.append("```\n")
    out.append("**无采样（不取数据）版本：**\n")
    out.append("```sql")
    out.append(portrait_plain)
    out.append("```\n")

    out.append(f"\n## ② 有采样标注\n")
    out.append("**发送 prompt（逐字）：**\n```text\n" + prompt_sampled + "\n```\n")
    r1 = asyncio.run(_call(cfg, conn_id, prompt_sampled))
    _dump_response(out, r1)

    out.append(f"\n## ③ 无采样标注\n")
    out.append("**发送 prompt（逐字）：**\n```text\n" + prompt_unsampled + "\n```\n")
    r2 = asyncio.run(_call(cfg, conn_id, prompt_unsampled))
    _dump_response(out, r2)

    doc_path = Path(__file__).resolve().parent.parent.parent / "docs" / "kb-annotate-demo-output.md"
    doc_path.write_text("\n".join(out), encoding="utf-8")
    print(f"\n✅ 已写入 {doc_path}")


async def _call(cfg, conn_id, prompt):
    provider = gw.build_provider(cfg)
    return await provider.chat(
        [{"role": "user", "content": prompt}], tools=None,
        ctx={"conn_id": conn_id, "connection": conn_id, "skill": "kb-demo",
             "source": "kb_demo", "status": "egress-demo"},
    )


def _dump_response(out, resp):
    content = resp.content or ""
    if content.startswith("```"):
        out.append("**返回 content：**\n\n" + content + "\n")
    else:
        out.append("**返回 content：**\n\n```json\n" + content + "\n```\n")
    out.append(f"**usage:** `{json.dumps(resp.usage, ensure_ascii=False)}`\n")


if __name__ == "__main__":
    main()
