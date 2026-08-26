"""端到端全量重构验证（生产代码全链路，真实模型，不污染线上库）。

流程：独立 conn_id + demo.db 副本 → KnowledgeBase.build(incluidng_samples=True)
  → 阶段一逐表（新模板，示例熔入 comment）
  → 阶段二领域（全量先 clear_tags + 空库划分 + 新画像）
  → 阶段三图谱（统一精简 DDL 画像）
断言：标签零残留（无 0 表孤儿）/ 图谱有边 / 无异常 / 全量完成。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.schema import get_schema, sample_values  # noqa: E402
import app.state as _app_state                          # noqa: E402


def main() -> None:
    state = _app_state.init_state()  # 全局单例——build() 内部用同一 state，勿另建实例
    # demo.db 副本 → 独立连接（不碰线上 knowledge-b9ee0e703c74）
    tmp_db = Path("/tmp/tt-kb-e2e-demo.db")
    if tmp_db.exists():
        tmp_db.unlink()
    home = Path.home()
    shutil.copy(home / ".tabletalk/demo.db", tmp_db)

    conn = state.connections.create({
        "name": "kb-e2e-verify", "dialect": "sqlite", "file": str(tmp_db),
        "read_only": True,
    })
    conn_id = conn.id
    print(f"验证连接: {conn_id} → {tmp_db}", flush=True)

    schema = asyncio.run(get_schema(state, conn_id, refresh=True))
    tables = [t["name"] for t in schema["tables"]]
    samples = {}
    for t in tables:
        samples[t] = asyncio.run(sample_values(state, conn_id, t, 10))

    def on_progress(stage, percent, detail=None, **kw):
        if percent is None:
            return
        print(f"  [{stage}] {percent}% {detail or ''}", flush=True)

    # 生产全链路 build（真实模型 + 授权采样）
    stats = asyncio.run(state.knowledge.build(
        conn_id, schema,
        samples=samples, include_samples=True,
        enable_ai_annotation=True, self_check=True,
        on_progress=on_progress,
    ))
    print(f"\nbuild 结果: {json.dumps(stats, ensure_ascii=False)}", flush=True)

    # ---- 断言 ----
    kb = state.knowledge
    # 1. 标签零残留：库内所有 tag 都必须挂表（无 0 表孤儿）
    tags = kb.tags(conn_id)
    lib = {t["name"]: t for t in tags["library"]}
    bound = set()
    for tbl, names in tags["tables"].items():
        bound.update(names if isinstance(names, list) else [names])
    orphans = [n for n in lib if n not in bound]
    print(f"标签数={len(lib)} 已绑定={len(bound)} 孤儿={orphans}", flush=True)
    assert not orphans, f"存在 0 表孤儿标签: {orphans}"

    # 2. 图谱有边（FK 图 + LLM draft 边至少其一）
    g = kb.graph(conn_id)
    edges = g.get("edges", [])
    llm_draft = g.get("llm_draft_edges", [])
    print(f"图谱: 正式边={len(edges)} LLM draft={len(llm_draft)}", flush=True)
    assert len(edges) + len(llm_draft) > 0, "图谱为空"

    # 3. 阶段一注释有示例（有采样授权 → comment 含「示例」）
    tabs = kb._tables.get(conn_id, {})
    sample_in_comment = 0
    ai_drafts = 0
    for tk in tabs.values():
        if tk.status in ("draft", "confirmed"):
            ai_drafts += 1
        for ci in tk.columns.values():
            if ci.comment and "示例" in ci.comment:
                sample_in_comment += 1
    print(f"表级 AI 注释数={ai_drafts} 含「示例」的列注释数={sample_in_comment}", flush=True)
    assert ai_drafts > 0, "阶段一无 AI 注释落库"
    assert sample_in_comment > 0, "有采样授权但列注释未带示例"

    print("\n✅ 端到端全量重构验证通过", flush=True)
    # 清理验证连接，避免污染线上连接列表
    state.connections.delete(conn_id)
    try:
        tmp_db.unlink()
    except OSError:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()