"""T5 采样分列策略测试：枚举列 DISTINCT（历史值不丢）、度量列最近行、无主键回退。"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.dialects.base import ColumnRef
from app.core.schema import _looks_like_enum, sample_values


def _make_db(tmp_path, rows: int = 20) -> str:
    """id 1 为历史遗留状态，2..N 为 active——pk 倒序采样会漏掉历史值。"""
    p = tmp_path / "t.db"
    con = sqlite3.connect(str(p))
    con.executescript("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            status TEXT,
            amount REAL,
            created_at TEXT
        );
    """)
    con.executemany(
        "INSERT INTO orders (id, status, amount, created_at) VALUES (?,?,?,?)",
        [(i, "legacy_status" if i == 1 else "active", float(i * 10.0), f"2024-{i:02d}-01")
         for i in range(1, rows + 1)],
    )
    con.commit()
    con.close()
    return str(p)


def _make_no_pk_db(tmp_path) -> str:
    p = tmp_path / "t2.db"
    con = sqlite3.connect(str(p))
    con.executescript("""
        CREATE TABLE events (name TEXT, val INTEGER);
        INSERT INTO events VALUES ('a', 1), ('b', 2), ('c', 3);
    """)
    con.commit()
    con.close()
    return str(p)


@pytest.fixture
def sample_conn(app_state, tmp_path) -> str:
    c = app_state.connections.create({"name": "t", "dialect": "sqlite", "file": _make_db(tmp_path)})
    return c.id


# ---------- 判定规则 ----------

def test_looks_like_enum_rules():
    class C:
        def __init__(self, name, ctype):
            self.name = name
            self.data_type = ctype

    assert _looks_like_enum(C("status", "TEXT")) is True
    assert _looks_like_enum(C("type", "INT")) is True
    assert _looks_like_enum(C("is_deleted", "INT")) is True
    assert _looks_like_enum(C("flag", "INT")) is True
    assert _looks_like_enum(C("active", "BOOLEAN")) is True
    assert _looks_like_enum(C("amount", "REAL")) is False
    assert _looks_like_enum(C("created_at", "TEXT")) is False
    assert _looks_like_enum(C("updated_at", "TEXT")) is False
    assert _looks_like_enum(C("price", "REAL")) is False


def test_char_length_boundary():
    """char/varchar 长度判定：≤20 判枚举、>20 走最近行、裸类型保守判枚举（T5 §4#5）。"""

    class C:
        def __init__(self, name, ctype):
            self.name = name
            self.data_type = ctype

    assert _looks_like_enum(C("code", "VARCHAR(20)")) is True
    assert _looks_like_enum(C("code", "CHAR(2)")) is True
    assert _looks_like_enum(C("code", "VARCHAR(21)")) is False
    assert _looks_like_enum(C("code", "VARCHAR(255)")) is False
    assert _looks_like_enum(C("code", "VARCHAR")) is True  # 裸类型：无长度信息，保守判枚举


# ---------- 分列采样 ----------

async def test_enum_col_gets_historical_values(app_state, sample_conn):
    """status 枚举列：DISTINCT 路径采到历史遗留值（pk 倒序会漏）。"""
    out = await sample_values(app_state, sample_conn, "orders", per_column=5)
    statuses = out.get("status", [])
    assert "legacy_status" in statuses, "DISTINCT 路径必须采到历史值"
    assert "active" in statuses


async def test_metric_col_uses_recent(app_state, sample_conn):
    """R4：20 行小表全表 DISTINCT——amount 全量去重（含最新行值 200.0）。"""
    out = await sample_values(app_state, sample_conn, "orders", per_column=5)
    amounts = out.get("amount", [])
    assert 200.0 in amounts  # 小表全量 DISTINCT 覆盖最新值
    assert len(amounts) == 20  # 20 行全量去重


async def test_no_pk_fallback(app_state, tmp_path):
    c = app_state.connections.create({"name": "t2", "dialect": "sqlite", "file": _make_no_pk_db(tmp_path)})
    out = await sample_values(app_state, c.id, "events", per_column=5)
    assert "a" in out.get("name", [])
    assert set(out.keys()) == {"name", "val"}


async def test_enum_distinct_dedup(app_state, sample_conn):
    """DISTINCT 路径值不重复。"""
    out = await sample_values(app_state, sample_conn, "orders", per_column=100)
    statuses = out["status"]
    assert len(statuses) == len(set(statuses)) == 2  # legacy_status + active


async def test_sampling_failure_graceful(app_state, sample_conn, monkeypatch):
    """采样失败返回 {} + warning，不炸构建（T5 §4#6）。"""

    class BoomAdapter:
        def quote_ident(self, name):
            return name

        async def list_columns(self, conn, table):
            return [ColumnRef(table=table, name="amount", data_type="REAL", is_pk=False)]

        async def execute(self, conn, sql):
            raise RuntimeError("boom")

    async def fake_run(conn_id, fn):
        return await fn(BoomAdapter(), None)

    monkeypatch.setattr(app_state.pools, "run", fake_run)
    out = await sample_values(app_state, sample_conn, "orders", per_column=5)
    assert out == {}


# ---------- R2：多态判别器分组采样（§3.4，供 build_polymorphic_edges） ----------

def _make_poly_db(tmp_path) -> str:
    """comments 表：type + ref_id 判别器模式（Rails polymorphic）。"""
    p = tmp_path / "poly.db"
    con = sqlite3.connect(str(p))
    con.executescript("""
        CREATE TABLE comments (
            id INTEGER PRIMARY KEY,
            body TEXT,
            type INTEGER,        -- 判别器：1=文章 2=视频
            ref_id INTEGER       -- 引用列：指向 article.id / video.id
        );
        CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE videos (id INTEGER PRIMARY KEY, title TEXT);
    """)
    con.executemany(
        "INSERT INTO comments (id, body, type, ref_id) VALUES (?,?,?,?)",
        [(i, f"c{i}", 1 if i <= 10 else 2, i if i <= 10 else i - 10)
         for i in range(1, 21)],
    )
    con.executemany("INSERT INTO articles (id, title) VALUES (?,?)",
                    [(i, f"a{i}") for i in range(1, 8)])
    con.executemany("INSERT INTO videos (id, title) VALUES (?,?)",
                    [(i, f"v{i}") for i in range(1, 8)])
    con.commit()
    con.close()
    return str(p)


async def test_polymorphic_grouped_sampling(app_state, tmp_path):
    """判别器表（type + ref_id）→ sample_values 产出 _grouped 分组采样（§3.4 格式）。"""
    c = app_state.connections.create(
        {"name": "poly", "dialect": "sqlite", "file": _make_poly_db(tmp_path)})
    out = await sample_values(app_state, c.id, "comments", per_column=10)
    grouped = out.get("_grouped")
    assert grouped is not None, out.keys()
    assert "type" in grouped and grouped["type"].get("__ref__") == "ref_id"
    # 组内值：type=1 组 ref_id 1..10；type=2 组 ref_id 1..10
    g1 = grouped["type"].get(1)
    g2 = grouped["type"].get(2)
    assert g1 and len(g1) > 0
    assert g2 and len(g2) > 0
    assert all(isinstance(v, int) for v in g1)


async def test_non_polymorphic_no_grouped(app_state, sample_conn):
    """无判别器模式的表 → 不产出 _grouped（零额外开销）。"""
    out = await sample_values(app_state, sample_conn, "orders", per_column=5)
    assert "_grouped" not in out


# ---------- R4：T5 小项（60 字符截断 / NULL 处理） ----------

def test_serialize_value_truncates_long_text():
    """T5 §4#3：超长 TEXT/JSON 值截断到 60 字符。"""
    from app.core.query import serialize_value

    long = "x" * 500
    out = serialize_value(long)
    assert len(out) == 60 + 3  # 截断 + 省略号
    assert out.endswith("...")
    # 短值不截断
    assert serialize_value("short") == "short"
    # 非字符串（int/list/dict）不影响
    assert serialize_value(42) == 42
    assert isinstance(serialize_value([1, 2, 3]), str)


async def test_null_handled(app_state, tmp_path):
    """T5 §6：列含 NULL → DISTINCT 路径剔除 NULL 或保留（二选一，不炸）。"""
    p = tmp_path / "null.db"
    con = sqlite3.connect(str(p))
    con.executescript("""
        CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO t VALUES (1, 'A'), (2, NULL), (3, 'B');
    """)
    con.commit()
    con.close()
    c = app_state.connections.create({"name": "null", "dialect": "sqlite", "file": str(p)})
    out = await sample_values(app_state, c.id, "t", per_column=10)
    statuses = out.get("status", [])
    assert "A" in statuses and "B" in statuses  # 非 NULL 值完整
    assert None not in statuses  # NULL 被剔除（DISTINCT 路径）

