"""本地脱敏网关 — B2 确定性 tokenization（纯函数，离线可审计）。

三类规则：
1. 模式匹配：邮箱/手机号/身份证/银行卡/IP
2. 列名语义：phone/email/id_card/name/address 等
3. 列级显式：敏感名单（table.column glob）或知识库敏感标签

确定性：HMAC-SHA256(value, salt) 截断 -> [TYPE_a3f2]；同值同 token，GROUP BY 可对上。
盐存 data_dir/redact.key (chmod 600)，永不出网；映射表内存 + 惰性持久化（仅本地还原）。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
from pathlib import Path
from typing import Any

# 1) 正则库（模式匹配）
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"\b1[3-9]\d{9}\b")),
    ("ID", re.compile(r"\b\d{17}[\dXx]\b")),
    ("CARD", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

# 2) 列名语义
_COL_HINTS: list[tuple[str, list[str]]] = [
    ("PHONE", ["phone", "mobile", "tel", "手机号"]),
    ("EMAIL", ["email", "mail", "邮箱"]),
    ("ID", ["id_card", "idcard", "身份证", "ssn"]),
    ("NAME", ["name", "姓名", "customer_name", "user_name"]),
    ("ADDRESS", ["address", "addr", "地址"]),
]

_SALT_LOCK = threading.Lock()
_SALT_CACHE: dict[str, bytes] = {}
_MAP_LOCK = threading.Lock()
_TOKEN_MAP: dict[str, str] = {}  # token -> original（仅内存，惰性落盘可选）

def _salt_path(data_dir: Path) -> Path:
    return Path(data_dir) / "redact.key"

def get_salt(data_dir: Path) -> bytes:
    key = str(data_dir)
    with _SALT_LOCK:
        if key in _SALT_CACHE:
            return _SALT_CACHE[key]
        p = _salt_path(data_dir)
        if p.exists():
            try:
                salt = p.read_bytes()
                if len(salt) >= 16:
                    _SALT_CACHE[key] = salt
                    return salt
            except OSError:
                pass
        # 生成 32 字节
        import os
        salt = os.urandom(32)
        try:
            p.write_bytes(salt)
            p.chmod(0o600)
        except OSError:
            pass
        _SALT_CACHE[key] = salt
        return salt

def _token(value: str, salt: bytes, prefix: str) -> str:
    # HMAC-SHA256 截断 8 hex = 32 bits，碰撞率极低（5k 值 <0.1%）
    h = hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:8]
    tok = f"[{prefix}_{h}]"
    with _MAP_LOCK:
        _TOKEN_MAP[tok] = value
    return tok

def _is_sensitive_col(table: str, col: str, sensitive: list[str | dict[str, Any]] | None) -> bool:
    """敏感列判定：兼容双结构——str=glob 模式；dict{table, columns[]}=精确名单（columns 空/缺=整表）。"""
    if not sensitive:
        return False
    import fnmatch
    tl = table.lower()
    cl = col.lower()
    for entry in sensitive:
        if isinstance(entry, dict):
            et = str(entry.get("table", "")).strip().lower()
            if not et:
                continue
            if tl != et and not fnmatch.fnmatch(tl, et):
                continue
            cols = entry.get("columns")
            # 未给 columns（或空数组）= 整表视为敏感
            if cols is None or (isinstance(cols, list) and not cols):
                return True
            if isinstance(cols, list):
                if any(cl == str(c).strip().lower() for c in cols if str(c).strip()):
                    return True
            continue
        if not isinstance(entry, str):
            continue
        pat = entry.strip().lower()
        if not pat:
            continue
        if fnmatch.fnmatch(f"{tl}.{cl}", pat) or fnmatch.fnmatch(cl, pat) or fnmatch.fnmatch(tl, pat):
            return True
    return False

def _col_prefix(table: str, col: str) -> str | None:
    lc = f"{table}.{col}".lower()
    for prefix, hints in _COL_HINTS:
        for h in hints:
            if h in lc:
                return prefix
    return None

def redact_text(text: str, salt: bytes, sensitive: list[str] | None = None) -> tuple[str, dict[str, str]]:
    """脱敏对话文本（含用户问题里贴的敏感串）。返回 (redacted, map)。"""
    if not text:
        return text, {}
    out = text
    local_map: dict[str, str] = {}
    # 1) 正则
    for prefix, pat in _PATTERNS:
        def repl(m: re.Match[str]) -> str:
            val = m.group(0)
            tok = _token(val, salt, prefix)
            local_map[tok] = val
            return tok
        out = pat.sub(repl, out)
    return out, local_map

def redact_rows(
    rows: list[list[Any]],
    columns: list[str],
    table: str,
    salt: bytes,
    sensitive: list[str | dict[str, Any]] | None = None,
    auto_rules: bool = True,
) -> tuple[list[list[Any]], dict[str, str]]:
    """脱敏聚合行（标准档下 include_data 的行）。仅列级判定，不扫值。

    auto_rules=True（标准档）：显式名单 + 列名语义（整列 token 化）；未命中列原样透传。
    auto_rules=False（自定义档）：只 token 化显式名单命中的列，其余原样。
    不做值正则逐格扫描——自由文本里的敏感串属 redact_text 的职责（对话/审计文本）。
    """
    if not rows or not columns:
        return rows, {}
    local_map: dict[str, str] = {}
    # 预判哪些列需整列 token 化（列级显式 + 列名语义）
    col_flags: list[str | None] = []
    for col in columns:
        flag = None
        if _is_sensitive_col(table, col, sensitive):
            # 显式敏感列：整列 token 化，前缀用列名 hint 或 SENSITIVE
            flag = _col_prefix(table, col) or "SENSITIVE"
        elif auto_rules:
            # 列名语义：若列名含敏感 hint，则整列视为敏感（即使值不匹配正则）
            flag = _col_prefix(table, col)
        col_flags.append(flag)

    # 无任何整列命中 → 原样直通
    if not any(col_flags):
        return rows, {}

    redacted: list[list[Any]] = []
    for row in rows:
        new_row: list[Any] = []
        for idx, val in enumerate(row):
            if val is None:
                new_row.append(None)
                continue
            flag = col_flags[idx] if idx < len(col_flags) else None
            if flag:
                # 整列敏感：直接 token 化
                sval = str(val)
                tok = _token(sval, salt, flag)
                local_map[tok] = sval
                new_row.append(tok)
            else:
                # 未命中列：原样透传（不做值正则扫描）
                new_row.append(val)
        redacted.append(new_row)
    return redacted, local_map
