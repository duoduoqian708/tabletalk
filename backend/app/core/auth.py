"""本地账号与会话（E4 最小闭环，单机零配置保持）。

- 单机模式（默认）：无 users.json 时，沿用 X-TableTalk-Token 单共享密钥，/bootstrap 免鉴权。
- 团队模式：users.json 存在或 TABLETALK_AUTH_MODE=team 时，启用本地账号：
  POST /api/v1/auth/login -> {token, user}，后续请求带 X-TableTalk-Token: <jwt-like>（兼容旧头）或 Authorization: Bearer。
  审计与权限全部 per-user。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import hmac
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    role: str = "member"  # admin | member
    created_at: str = ""

def _hash_pwd(pwd: str, salt: str = "tabletalk-salt") -> str:
    return hashlib.sha256((salt + pwd).encode()).hexdigest()

def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _sign(payload: dict[str, Any], secret: str) -> str:
    import base64, json as _json
    header = _b64url(_json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    body = _b64url(_json.dumps(payload, ensure_ascii=False).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"

def _verify(token: str, secret: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, body, sig = parts
        expect = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        import base64
        # pad
        def _pad(s: str) -> str:
            return s + "=" * (-len(s) % 4)
        expect_b64 = _b64url(expect)
        if not hmac.compare_digest(expect_b64, sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(_pad(body)).decode())
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None

class AuthStore:
    def __init__(self, data_dir: Path, secret: str | None = None):
        self._path = Path(data_dir) / "users.json"
        self._secret = secret or self._load_secret(data_dir)
        self._users: dict[str, User] = {}
        self._load()

    def _load_secret(self, data_dir: Path) -> str:
        # 复用 sidecar token 作为 JWT 密钥（若无则生成）
        p = Path(data_dir) / "tabletalk.token"
        if p.exists():
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        # 兜底：生成
        import secrets
        s = secrets.token_hex(32)
        try:
            p.write_text(s, encoding="utf-8")
            p.chmod(0o600)
        except OSError:
            pass
        return s

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for u in data:
                self._users[u["username"]] = User(**u)
        except Exception:
            pass

    def _save(self):
        try:
            self._path.write_text(json.dumps([asdict(u) for u in self._users.values()], ensure_ascii=False, indent=2), encoding="utf-8")
            self._path.chmod(0o600)
        except OSError:
            pass

    def is_team_mode(self) -> bool:
        # 显式 env 优先，否则有 users.json 即团队模式
        if os.environ.get("TABLETALK_AUTH_MODE", "").lower() == "team":
            return True
        if os.environ.get("TABLETALK_AUTH_MODE", "").lower() == "single":
            return False
        return self._path.exists() and len(self._users) > 0

    def list_users(self) -> list[dict[str, Any]]:
        return [{"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at} for u in self._users.values()]

    def create_user(self, username: str, password: str, role: str = "member") -> User:
        username = username.strip()
        if not username or not password:
            raise ValueError("username/password required")
        if username in self._users:
            raise ValueError("username exists")
        if role not in ("admin", "member"):
            role = "member"
        import uuid, time as _time
        u = User(id=f"u_{uuid.uuid4().hex[:8]}", username=username, password_hash=_hash_pwd(password), role=role, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self._users[username] = u
        self._save()
        # 首个用户自动为 admin
        if len(self._users) == 1 and u.role != "admin":
            u.role = "admin"
            self._save()
        return u

    def verify_password(self, username: str, password: str) -> User | None:
        u = self._users.get(username)
        if not u:
            return None
        if hmac.compare_digest(u.password_hash, _hash_pwd(password)):
            return u
        return None

    def issue_token(self, user: User, ttl: int = 86400) -> str:
        payload = {"sub": user.id, "username": user.username, "role": user.role, "exp": int(time.time()) + ttl, "iat": int(time.time())}
        return _sign(payload, self._secret)

    def verify_token(self, token: str) -> dict[str, Any] | None:
        return _verify(token, self._secret)

    def get_user_by_id(self, uid: str) -> User | None:
        for u in self._users.values():
            if u.id == uid:
                return u
        return None
