"""敏感度路由 — B3 代号化（确定性，本地可逆）。

敏感表：结构出网时表名/列名替换为代号（t_17/c_3），注释剥离；回复回来按映射还原展示。
映射：每连接一套，存 data_dir/codify-{conn_id}.json（chmod 600），永不出网。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}

def _path(data_dir: Path, conn_id: str) -> Path:
    return Path(data_dir) / f"codify-{conn_id}.json"

def _hash(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:4], 16)

def _load(data_dir: Path, conn_id: str) -> dict[str, Any]:
    key = f"{data_dir}:{conn_id}"
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        p = _path(data_dir, conn_id)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                _CACHE[key] = data
                return data
            except Exception:
                pass
        data = {"tables": {}, "cols": {}, "rev_tables": {}, "rev_cols": {}}
        _CACHE[key] = data
        return data

def _save(data_dir: Path, conn_id: str, data: dict[str, Any]) -> None:
    key = f"{data_dir}:{conn_id}"
    with _LOCK:
        _CACHE[key] = data
        p = _path(data_dir, conn_id)
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            p.chmod(0o600)
        except OSError:
            pass

def codify_table(data_dir: Path, conn_id: str, table: str) -> str:
    data = _load(data_dir, conn_id)
    if table in data["tables"]:
        return data["tables"][table]
    # 生成 t_17 形式，哈希后取 0-99
    code = f"t_{_hash(table) % 100}"
    # 避免冲突：若已存在相同 code 指向不同表，则 +1 递增
    existing = set(data["tables"].values())
    base = code
    i = 0
    while code in existing:
        # 若已存在且指向同一表则复用，否则递增
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
    base = code
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
        # table_tags: table -> [tag names]
        table_tags = tags.get("tables", {})
        tag_names = table_tags.get(table, [])
        # 若表被打上 sensitive 标签（且已确认），则视为敏感
        lib = {t["name"]: t for t in tags.get("library", [])}
        for tn in tag_names:
            if tn.lower() == "sensitive" and lib.get(tn, {}).get("status") == "confirmed":
                return True
        # 兼容：若库中存在名为 sensitive 的标签且状态为 draft，但表已打上该标签，也视为敏感（待确认时也代号化，出网清单中无明文）
        # 但按 07 §7 不变式，路由只认 confirmed，此处代号化也应只认 confirmed，故不处理 draft
    except Exception:
        pass
    return False

def codify_schema(data_dir: Path, conn_id: str, schema: dict[str, Any], sensitive_tables: set[str]) -> dict[str, Any]:
    """返回代号化后的 schema 副本（表名/列名替换，注释剥离）。"""
    import copy
    out = copy.deepcopy(schema)
    # 映射表
    table_map = {tbl: codify_table(data_dir, conn_id, tbl) for tbl in sensitive_tables}
    col_map = {}
    for col in out.get("columns", []):
        tbl = col.get("table", "")
        if tbl in sensitive_tables:
            col_map[(tbl, col["name"])] = codify_column(data_dir, conn_id, tbl, col["name"])
    # 替换 tables
    for t in out.get("tables", []):
        orig = t.get("name", "")
        if orig in table_map:
            t["name"] = table_map[orig]
            t["comment"] = ""  # 剥离注释
    # 替换 columns
    for c in out.get("columns", []):
        tbl = c.get("table", "")
        if tbl in sensitive_tables:
            # 表名代号化
            c["table"] = table_map.get(tbl, tbl)
            # 列名代号化
            key = (tbl, c["name"])
            if key in col_map:
                c["name"] = col_map[key]
            c["comment"] = ""
    # 替换 foreign_keys
    for fk in out.get("foreign_keys", []):
        if fk.get("table") in table_map:
            # 列名也需代号化
            orig_tbl = fk["table"]
            fk["table"] = table_map[orig_tbl]
            fk["column"] = col_map.get((orig_tbl, fk["column"]), fk["column"])
        if fk.get("ref_table") in table_map:
            orig_ref = fk["ref_table"]
            fk["ref_table"] = table_map[orig_ref]
            fk["ref_column"] = col_map.get((orig_ref, fk["ref_column"]), fk["ref_column"])
    return out
