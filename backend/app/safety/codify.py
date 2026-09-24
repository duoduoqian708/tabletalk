"""敏感度路由 — B3 代号化（SQLite 持久化 tabletalk.db: codify）。"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from app.core.system_db import get_conn, init_system_db

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}

def _hash(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:4], 16)

def _load(data_dir: Path, conn_id: str) -> dict[str, Any]:
    key = f"{Path(data_dir)}:{conn_id}"
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        # 优先从 DB 读
        try:
            init_system_db(Path(data_dir))
            con = get_conn(Path(data_dir))
            cur = con.execute("SELECT kind, code, original FROM codify WHERE conn_id=?", (conn_id,))
            data: dict[str, Any] = {"tables": {}, "cols": {}, "rev_tables": {}, "rev_cols": {}}
            for kind, code, original in cur.fetchall():
                if kind == "table":
                    data["tables"][original] = code
                    data["rev_tables"][code] = original
                elif kind == "col":
                    data["cols"][original] = code
                    data["rev_cols"][code] = original
            con.close()
            if data["tables"] or data["cols"]:
                _CACHE[key] = data
                return data
        except Exception:
            pass
        # 回退：旧 JSON 迁移
        p = Path(data_dir) / f"codify-{conn_id}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                _CACHE[key] = data
                # 迁移到 DB
                try:
                    con = get_conn(Path(data_dir))
                    for tbl, code in data.get("tables", {}).items():
                        con.execute("INSERT OR IGNORE INTO codify (conn_id, kind, code, original) VALUES (?,?,?,?)",
                                    (conn_id, "table", code, tbl))
                    for col_key, code in data.get("cols", {}).items():
                        con.execute("INSERT OR IGNORE INTO codify (conn_id, kind, code, original) VALUES (?,?,?,?)",
                                    (conn_id, "col", code, col_key))
                    con.commit()
                    con.close()
                    bak = p.with_suffix(".json.bak")
                    if not bak.exists():
                        p.rename(bak)
                except Exception:
                    pass
                return data
            except Exception:
                pass
        data = {"tables": {}, "cols": {}, "rev_tables": {}, "rev_cols": {}}
        _CACHE[key] = data
        return data

def _save(data_dir: Path, conn_id: str, data: dict[str, Any]) -> None:
    key = f"{Path(data_dir)}:{conn_id}"
    with _LOCK:
        _CACHE[key] = data
        # 写 DB（增量）
        try:
            init_system_db(Path(data_dir))
            con = get_conn(Path(data_dir))
            # 全量同步：先删旧，再插新（映射量小，简单可靠）
            con.execute("DELETE FROM codify WHERE conn_id=?", (conn_id,))
            for tbl, code in data.get("tables", {}).items():
                con.execute("INSERT INTO codify (conn_id, kind, code, original) VALUES (?,?,?,?)",
                            (conn_id, "table", code, tbl))
            for col_key, code in data.get("cols", {}).items():
                con.execute("INSERT INTO codify (conn_id, kind, code, original) VALUES (?,?,?,?)",
                            (conn_id, "col", code, col_key))
            con.commit()
            con.close()
        except Exception:
            pass
        # 旧文件归档
        p = Path(data_dir) / f"codify-{conn_id}.json"
        if p.exists():
            try:
                bak = p.with_suffix(".json.bak")
                if not bak.exists():
                    p.rename(bak)
            except OSError:
                pass

def codify_table(data_dir: Path, conn_id: str, table: str) -> str:
    data = _load(data_dir, conn_id)
    if table in data["tables"]:
        return data["tables"][table]
    code = f"t_{_hash(table) % 100}"
    existing = set(data["tables"].values())
    i = 0
    while code in existing:
        rev = data["rev_tables"].get(code)
        if rev == table:
            break
        i += 1
        code = f"t_{( _hash(table) + i) % 100}"
        if i > 50:
            break
    data["tables"][table] = code
    data["rev_tables"][code] = table
    _save(data_dir, conn_id, data)
    return code

def codify_column(data_dir: Path, conn_id: str, table: str, col: str) -> str:
    key = f"{table}.{col}"
    data = _load(data_dir, conn_id)
    if key in data["cols"]:
        return data["cols"][key]
    code = f"c_{_hash(key) % 100}"
    existing = set(data["cols"].values())
    i = 0
    while code in existing:
        rev = data["rev_cols"].get(code)
        if rev == key:
            break
        i += 1
        code = f"c_{( _hash(key) + i) % 100}"
        if i > 50:
            break
    data["cols"][key] = code
    data["rev_cols"][code] = key
    _save(data_dir, conn_id, data)
    return code

def decodify_text(data_dir: Path, conn_id: str, text: str) -> str:
    data = _load(data_dir, conn_id)
    import re
    for code, orig in list(data["rev_cols"].items()):
        _, col = orig.split(".", 1) if "." in orig else ("", orig)
        text = re.sub(r"\b" + re.escape(code) + r"\b", col, text)
    for code, orig in list(data["rev_tables"].items()):
        text = re.sub(r"\b" + re.escape(code) + r"\b", orig, text)
    return text

def is_sensitive_table(state: Any, conn_id: str, table: str) -> bool:
    try:
        tags = state.knowledge.tags(conn_id)
        table_tags = tags.get("tables", {})
        tag_names = table_tags.get(table, [])
        lib = {t["name"]: t for t in tags.get("library", [])}
        for tn in tag_names:
            if tn.lower() in ("sensitive", "敏感") and lib.get(tn, {}).get("status") == "confirmed":
                return True
    except Exception:
        pass
    return False

def codify_schema(data_dir: Path, conn_id: str, schema: dict[str, Any], sensitive_tables: set[str]) -> dict[str, Any]:
    import copy
    out = copy.deepcopy(schema)
    table_map = {tbl: codify_table(data_dir, conn_id, tbl) for tbl in sensitive_tables}
    col_map = {}
    for col in out.get("columns", []):
        tbl = col.get("table", "")
        if tbl in sensitive_tables:
            col_map[(tbl, col["name"])] = codify_column(data_dir, conn_id, tbl, col["name"])
    for t in out.get("tables", []):
        orig = t.get("name", "")
        if orig in table_map:
            t["name"] = table_map[orig]
            t["comment"] = ""
    for c in out.get("columns", []):
        tbl = c.get("table", "")
        if tbl in sensitive_tables:
            c["table"] = table_map.get(tbl, tbl)
            key = (tbl, c["name"])
            if key in col_map:
                c["name"] = col_map[key]
            c["comment"] = ""
    for fk in out.get("foreign_keys", []):
        if fk.get("table") in table_map:
            orig_tbl = fk["table"]
            fk["table"] = table_map[orig_tbl]
            fk["column"] = col_map.get((orig_tbl, fk["column"]), fk["column"])
        if fk.get("ref_table") in table_map:
            orig_ref = fk["ref_table"]
            fk["ref_table"] = table_map[orig_ref]
            fk["ref_column"] = col_map.get((orig_ref, fk["ref_column"]), fk["ref_column"])
    return out
