"""向量文本 AI 画像体系回归（2026-09）：零代码拼接，override > profile > 空。"""
from __future__ import annotations

from types import SimpleNamespace

from app.knowledge.semantic.store import SYNTH_VERSION, SemanticStore
from app.knowledge.store import TableKnowledge
from app.knowledge.annotator import _mock_table_comments_from_ddl, _parse_items


def _mk_store(*tables: TableKnowledge) -> SemanticStore:
    st = SemanticStore()
    st._tables["c1"] = {t.name: t for t in tables}
    return st


def test_table_vector_text_priority():
    """override > profile > 空；空 = 无向量文本（调用方跳过嵌入）。"""
    st = SemanticStore()
    tk = TableKnowledge(name="t1", vector_profile="画像文本")
    assert st.table_vector_text("c1", tk) == "画像文本"
    tk.vector_override = "人工文本"
    assert st.table_vector_text("c1", tk) == "人工文本"
    tk2 = TableKnowledge(name="t2")
    assert st.table_vector_text("c1", tk2) == ""


def test_profile_proposal_and_confirm():
    """表级 profile 走提案流：draft 不动当前值，confirm 提升 vector_profile。"""
    tk = TableKnowledge(name="t1")
    st = _mk_store(tk)
    n = st.annotate_drafts("c1", [
        {"table": "t1", "column": None, "comment": "表定位", "profile": "画像 150~300 字"},
    ])
    assert n == 1
    assert tk.proposed_comment == "表定位"
    assert tk.proposed_profile == "画像 150~300 字"
    assert tk.vector_profile == ""          # 提案未确认，当前值不动
    assert st.table_vector_text("c1", tk) == ""
    import asyncio
    asyncio.get_event_loop_policy()
    asyncio.run(st.confirm("c1"))
    assert tk.comment == "表定位"
    assert tk.vector_profile == "画像 150~300 字"
    assert st.table_vector_text("c1", tk) == "画像 150~300 字"
    assert not tk.has_proposal


def test_parse_items_profile():
    """_parse_items：表级项提取 profile；列级项的 profile 忽略；超长截 600。"""
    items = _parse_items(
        '[{"table":"t1","comment":"定位","profile":"' + "画" * 700 + '"},'
        '{"table":"t1","column":"a","comment":"列","profile":"不应出现"}]'
    )
    tbl = next(i for i in items if i["column"] is None)
    assert tbl["profile"] == "画" * 600
    col = next(i for i in items if i["column"] == "a")
    assert "profile" not in col


def test_mock_table_profile_excludes_noise_columns():
    """mock（扮演 LLM）产画像：噪音/系统列不进关键字段。"""
    cols = [{"name": "id", "type": "int"}, {"name": "biz_name", "type": "varchar"},
            {"name": "is_deleted", "type": "tinyint"}, {"name": "create_time", "type": "datetime"}]
    items = _mock_table_comments_from_ddl("t1", cols, None)
    tbl = items[0]
    assert tbl["column"] is None and tbl["profile"]
    assert "biz_name" in tbl["profile"]
    assert "is_deleted" not in tbl["profile"] and "create_time" not in tbl["profile"]


def test_edit_clear_override_returns_profile_state():
    """清空人工覆盖（''）→ 回落 AI 画像；返回体带 profile/提案状态。"""
    import asyncio
    tk = TableKnowledge(name="t1", vector_profile="画像", vector_override="手改")
    st = _mk_store(tk)
    r = asyncio.run(st.edit_table_knowledge("c1", "t1", vector_text=""))
    assert tk.vector_override == ""
    assert r["vector_text"] == "画像"
    assert r["vector_profile"] == "画像"
    assert r["vector_override"] is None


def test_synth_version_bumped():
    """SYNTH_VERSION v3：画像体系变更触发全库重嵌指纹更新。"""
    assert SYNTH_VERSION == "v3"


def test_embed_skips_empty_text_keeps_old_vector():
    """无向量文本的表跳过嵌入且保留旧向量；有画像的表正常重嵌。"""
    import asyncio

    from app.knowledge.build import BuildService

    has_profile = TableKnowledge(name="t_ok", vector_profile="画像文本")
    no_text = TableKnowledge(name="t_empty")
    store = SemanticStore()
    store._tables["c1"] = {"t_ok": has_profile, "t_empty": no_text}

    captured: dict[str, str] = {}

    class _Emb:
        async def embed(self, text: str) -> list[float]:
            captured["last"] = text
            return [0.1, 0.2]

    retrieval = SimpleNamespace(_table_vec={"c1": {"t_empty": [0.9, 0.9], "t_gone": [0.5]}})
    facade = SimpleNamespace(
        semantic_store=store, _emb=_Emb(), retrieval_service=retrieval,
    )
    asyncio.run(BuildService._embed_tables(None, facade, "c1"))
    vecs = retrieval._table_vec["c1"]
    assert vecs["t_ok"] == [0.1, 0.2]          # 画像文本已嵌入
    assert vecs["t_empty"] == [0.9, 0.9]       # 无文本 → 旧向量保留
    assert captured["last"] == "画像文本"
    assert "t_gone" not in vecs                # 全量档清掉已移除表的陈旧向量


def test_table_knowledge_persist_roundtrip():
    """新字段持久化往返 + 旧快照缺键容错。"""
    tk = TableKnowledge(name="t1", vector_profile="p", proposed_profile="pp", vector_override="o")
    d = tk.to_dict()
    assert TableKnowledge.from_dict(d).vector_profile == "p"
    legacy = TableKnowledge.from_dict({"name": "t2"})
    assert legacy.vector_profile == "" and legacy.proposed_profile == ""
