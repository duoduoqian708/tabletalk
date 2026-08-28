"""本地账号与会话（E4 最小闭环，SQLite 持久化 tabletalk.db: users）。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from app.core.timeutil import utcnow_iso
import hmac
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.core.system_db import get_conn, init_system_db


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    role: str = "member"
    created_at: str = ""
    is_initial: bool = False

def _hash_pwd(pwd: str, salt: str | None = None) -> str:
    if salt is not None:
        if salt == "tabletalk-salt":
            return hashlib.sha256((salt + pwd).encode()).hexdigest()
        salt_bytes = bytes.fromhex(salt) if len(salt) == 32 else salt.encode()
        dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt_bytes, 200_000)
        return f"pbkdf2${salt}${dk.hex()}"
    salt_hex = secrets.token_hex(16)
    salt_bytes = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt_bytes, 200_000)
    return f"pbkdf2${salt_hex}${dk.hex()}"

def _verify_pwd(pwd: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        try:
            _, salt_hex, hash_hex = stored.split("$", 2)
            salt_bytes = bytes.fromhex(salt_hex)
            expect = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt_bytes, 200_000).hex()
            return hmac.compare_digest(expect, hash_hex)
        except Exception:
            return False
    return hmac.compare_digest(stored, hashlib.sha256(("tabletalk-salt" + pwd).encode()).hexdigest())

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
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "users.json"  # 旧文件，仅迁移
        self._secret = secret or self._load_secret(data_dir)
        self._users: dict[str, User] = {}
        init_system_db(self._data_dir)
        self._load()

    def _load_secret(self, data_dir: Path) -> str:
        p = Path(data_dir) / "tabletalk.token"
        if p.exists():
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        s = secrets.token_hex(32)
        try:
            p.write_text(s, encoding="utf-8")
            p.chmod(0o600)
        except OSError:
            pass
        return s

    def _load(self):
        # 优先从 DB 读
        try:
            con = get_conn(self._data_dir)
            cur = con.execute("SELECT data FROM users")
            rows = cur.fetchall()
            con.close()
            if rows:
                for (data_json,) in rows:
                    try:
                        u = json.loads(data_json)
                        u.setdefault("is_initial", False)
                        self._users[u["username"]] = User(**u)
                    except Exception:
                        continue
                return
        except Exception:
            pass
        # 回退：旧 JSON 迁移
        if not self._path.exists():
            try:
                self.create_user("admin", "admin123", "admin")
                self._users["admin"].is_initial = True
                self._save()
            except Exception:
                pass
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for u in data:
                u.setdefault("is_initial", False)
                self._users[u["username"]] = User(**u)
            if self._users:
                self._save()
                try:
                    bak = self._path.with_suffix(".json.bak")
                    if not bak.exists():
                        self._path.rename(bak)
                except OSError:
                    pass
        except Exception:
            pass

    def _save(self):
        try:
            con = get_conn(self._data_dir)
            con.execute("DELETE FROM users")
            for u in self._users.values():
                con.execute("INSERT INTO users (id, username, data) VALUES (?,?,?)",
                            (u.id, u.username, json.dumps(asdict(u), ensure_ascii=False)))
            con.commit()
            con.close()
        except Exception:
            pass
        # 旧文件归档
        if self._path.exists():
            try:
                bak = self._path.with_suffix(".json.bak")
                if not bak.exists():
                    self._path.rename(bak)
            except OSError:
                pass

    def is_team_mode(self) -> bool:
        mode = os.environ.get("TABLETALK_AUTH_MODE", "").lower()
        if mode == "team":
            return True
        if mode == "single":
            return False
        # DB 为空即单机
        if len(self._users) == 0:
            return False
        if len(self._users) == 1:
            u = next(iter(self._users.values()))
            if u.username == "admin" and getattr(u, "is_initial", False):
                return False
        return True

    def list_users(self) -> list[dict[str, Any]]:
        return [{"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at, "is_initial": getattr(u, "is_initial", False)} for u in self._users.values()]

    def create_user(self, username: str, password: str, role: str = "member") -> User:
        username = username.strip()
        if not username or not password:
            raise ValueError("username/password required")
        if username in self._users:
            raise ValueError("username exists")
        if role not in ("admin", "member"):
            role = "member"
        import uuid, time as _time
        u = User(id=f"u_{uuid.uuid4().hex[:8]}", username=username, password_hash=_hash_pwd(password), role=role, created_at=utcnow_iso())
        self._users[username] = u
        self._save()
        if len(self._users) == 1 and u.role != "admin":
            u.role = "admin"
            self._save()
        return u

    def verify_password(self, username: str, password: str) -> User | None:
        u = self._users.get(username)
        if not u:
            return None
        if _verify_pwd(password, u.password_hash):
            if not u.password_hash.startswith("pbkdf2$"):
                u.password_hash = _hash_pwd(password)
                self._save()
            return u
        return None

    def change_password(self, username: str, old_password: str, new_password: str) -> User | None:
        u = self.verify_password(username, old_password)
        if not u:
            return None
        if not new_password or len(new_password) < 4:
            raise ValueError("新密码至少 4 位")
        u.password_hash = _hash_pwd(new_password)
        u.is_initial = False
        self._save()
        return u

    def issue_token(self, user: User, ttl: int = 86400) -> str:
        payload = {"sub": user.id, "username": user.username, "role": user.role, "is_initial": getattr(user, "is_initial", False), "exp": int(time.time()) + ttl, "iat": int(time.time())}
        return _sign(payload, self._secret)

    def verify_token(self, token: str) -> dict[str, Any] | None:
        return _verify(token, self._secret)

    def get_user_by_id(self, uid: str) -> User | None:
        for u in self._users.values():
            if u.id == uid:
                return u
        return None
