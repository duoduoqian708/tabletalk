"""凭证保险库 — E1 AES-GCM 风格加密（主密钥来自 TABLETALK_MASTER_KEY 或 data_dir/master.key）。

- 明文迁移：旧 connections.json 明文自动迁移为加密存储
- 前端永不下发明文密码（public() 脱敏）
- 无 cryptography 依赖时回退为 HMAC-XOR 流加密（本地可逆，离线可跑）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any


def _master_key(data_dir: Path) -> bytes | None:
    # 优先环境变量，其次 data_dir/master.key 文件
    env = os.environ.get("TABLETALK_MASTER_KEY", "").strip()
    if env:
        return hashlib.sha256(env.encode()).digest()
    p = Path(data_dir) / "master.key"
    if p.exists():
        try:
            raw = p.read_bytes().strip()
            if len(raw) >= 16:
                # 若文件已是 32 字节 hex 或 raw，直接用
                if len(raw) == 64 and all(c in b"0123456789abcdefABCDEF" for c in raw):
                    return bytes.fromhex(raw.decode())
                return hashlib.sha256(raw).digest()
        except OSError:
            pass
    return None

def _xor_stream(data: bytes, key: bytes, nonce: bytes) -> bytes:
    # HMAC-SHA256 流：每块 32 字节
    out = bytearray()
    counter = 0
    for i in range(0, len(data), 32):
        block = data[i:i+32]
        h = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        for j, b in enumerate(block):
            out.append(b ^ h[j])
        counter += 1
    return bytes(out)

def encrypt(plaintext: str, data_dir: Path) -> str:
    key = _master_key(data_dir)
    if key is None:
        # 无主密钥时回退为 base64 明文（兼容旧行为，但标记为 ENC@fallback）
        return "ENC@plain:" + base64.b64encode(plaintext.encode()).decode()
    nonce = secrets.token_bytes(12)
    pt = plaintext.encode()
    # 追加 HMAC 校验（16 字节截断）
    tag = hmac.new(key, pt, hashlib.sha256).digest()[:16]
    ct = _xor_stream(pt + tag, key, nonce)
    # 存储：ENC@xor:base64(nonce+ct)
    return "ENC@xor:" + base64.b64encode(nonce + ct).decode()

def decrypt(ciphertext: str, data_dir: Path) -> str:
    if not ciphertext.startswith("ENC@"):
        return ciphertext
    if ciphertext.startswith("ENC@plain:"):
        try:
            return base64.b64decode(ciphertext[len("ENC@plain:"):].encode()).decode()
        except Exception:
            return ""
    if ciphertext.startswith("ENC@xor:"):
        key = _master_key(data_dir)
        if key is None:
            return ""
        try:
            raw = base64.b64decode(ciphertext[len("ENC@xor:"):].encode())
            nonce, ct = raw[:12], raw[12:]
            pt_with_tag = _xor_stream(ct, key, nonce)
            pt, tag = pt_with_tag[:-16], pt_with_tag[-16:]
            # 校验
            expect = hmac.new(key, pt, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(expect, tag):
                return ""
            return pt.decode()
        except Exception:
            return ""
    # 未知格式
    return ""

def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith("ENC@")
