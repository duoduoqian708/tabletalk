"""标签颜色（v2）：颜色后端持久化，改一处全端引用同步；无颜色回退哈希色板。"""
from __future__ import annotations

import sqlite3

from app.knowledge.store import KnowledgeBase
from app.knowledge.storage import SqliteStorage, KB_SNAPSHOT_VERSION

from tests.knowledge.test_store import _schema  # noqa: PLC2701 - 复用测试 schema


async def test_tag_color_lifecycle(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema())
    # 新建带色
    assert kb.create_tag("c1", "订单", "订单领域", "#e25050") is True
    lib = kb.tags("c1")["library"]
    tag = next(t for t in lib if t["name"] == "订单")
    assert tag["color"] == "#e25050"
    # 改色
    assert kb.update_tag("c1", "订单", color="#50e27a") is True
    tag = next(t for t in kb.tags("c1")["library"] if t["name"] == "订单")
    assert tag["color"] == "#50e27a"
    # 改名 → 色保留（表绑定同步）
    assert kb.update_tag("c1", "订单", new_name="订单中心") is True
    lib = kb.tags("c1")["library"]
    assert next(t for t in lib if t["name"] == "订单中心")["color"] == "#50e27a"
    # overview 引用带色
    kb.assign_table_tags("c1", "orders", ["订单中心"])
    ov = kb.overview("c1")
    tbl = next(t for t in ov["tables"] if t["name"] == "orders")
    assert tbl["tags"] == [{"name": "订单中心", "status": "confirmed", "color": "#50e27a"}]
    # 无色标签 → color 空串（前端回退哈希）
    assert kb.create_tag("c1", "无色", "") is True
    assert next(t for t in kb.tags("c1")["library"] if t["name"] == "无色")["color"] == ""


def test_storage_tag_color_roundtrip(tmp_path):
    st = SqliteStorage(tmp_path, "c1")
    snap = _snap()
    snap.tags["订单"]["color"] = "#e25050"
    st.save(snap)
    snap2 = st.load()
    assert snap2.tags["订单"]["color"] == "#e25050"


def test_storage_tag_color_column_migration(tmp_path):
    """旧库 tags 表无 color 列 → load 时迁移 ALTER 补齐，数据不丢。"""
    st = SqliteStorage(tmp_path, "c1")
    snap = _snap()
    st.save(snap)
    # 模拟旧 schema：删掉 color 列
    conn = sqlite3.connect(str(tmp_path / "knowledge-c1.db"))
    conn.execute("ALTER TABLE tags DROP COLUMN color")
    conn.commit()
    conn.close()
    st2 = SqliteStorage(tmp_path, "c1")
    snap2 = st2.load()
    assert snap2.tags["订单"]["status"] == "confirmed"
    assert snap2.tags["订单"].get("color", "") == ""
    assert len(snap2.tables) == 1  # 其余数据不丢
    st2.save(snap2)  # 迁移后写入不炸


def _snap():
    from tests.knowledge.test_storage import _snap as _src_snap

    return _src_snap()