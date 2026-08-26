"""连接注册表：SQLite 持久化（tabletalk.db: connections），兼容旧 JSON 迁移。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.dialects.base import DialectConfig
from app.core.system_db import get_conn, init_system_db


@dataclass
class ConnectionConfig:
    id: str
    name: str
    dialect: str
    host: str = ""
    port: int | None = None
    user: str = ""
    password: str = ""
    database: str = ""
    file: str = ""
    ssl: bool = False
    read_only: bool = False
    timeout: int = 10
    credential_ref: str | None = None
    created_at: str = ""
    sensitive: list[str | dict[str, Any]] = field(default_factory=list)
    kb_status: str = "none"
    kb_updated_at: str = ""

    def to_dialect_config(self) -> DialectConfig:
        return DialectConfig(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            file=self.file,
            ssl=self.ssl,
            read_only=self.read_only,
            timeout=self.timeout,
        )


class ConnectionRegistry:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "connections.json"  # 旧文件，仅迁移
        self._conns: dict[str, ConnectionConfig] = {}
        self._lock = threading.Lock()
        init_system_db(self._data_dir)
        self._load()
        self._migrate_json_if_needed()

    def _load(self) -> None:
        # 从 DB 读
        try:
            con = get_conn(self._data_dir)
            cur = con.execute("SELECT data FROM connections")
            for (data_json,) in cur.fetchall():
                try:
                    item = json.loads(data_json)
                    pwd = item.get("password", "")
                    if isinstance(pwd, str) and pwd.startswith("ENC@"):
                        try:
                            from app.core.vault import decrypt
                            item["password"] = decrypt(pwd, self._data_dir)
                        except Exception:
                            item["password"] = ""
                    cfg = ConnectionConfig(**item)
                    self._conns[cfg.id] = cfg
                except Exception:
                    continue
            con.close()
        except Exception:
            pass

    def _migrate_json_if_needed(self) -> None:
        # 仅当 DB 空且旧文件存在时迁移
        if self._conns:
            return
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw:
                try:
                    pwd = item.get("password", "")
                    if isinstance(pwd, str) and pwd.startswith("ENC@"):
                        try:
                            from app.core.vault import decrypt
                            item["password"] = decrypt(pwd, self._data_dir)
                        except Exception:
                            item["password"] = ""
                    cfg = ConnectionConfig(**item)
                    self._conns[cfg.id] = cfg
                except Exception:
                    continue
            if self._conns:
                self.save()
                try:
                    bak = self._path.with_suffix(".json.bak")
                    if not bak.exists():
                        self._path.rename(bak)
                except OSError:
                    pass
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            con = get_conn(self._data_dir)
            try:
                # 全量替换：删旧插新（连接数少，简单可靠）
                con.execute("DELETE FROM connections")
                for c in self._conns.values():
                    d = asdict(c)
                    pwd = d.get("password", "")
                    if pwd and not str(pwd).startswith("ENC@"):
                        try:
                            from app.core.vault import encrypt
                            d["password"] = encrypt(str(pwd), self._data_dir)
                        except Exception:
                            pass
                    con.execute(
                        "INSERT INTO connections (id, data, created_at) VALUES (?,?,?)",
                        (c.id, json.dumps(d, ensure_ascii=False), c.created_at or ""),
                    )
                con.commit()
            finally:
                con.close()

    def list(self) -> list[ConnectionConfig]:
        return list(self._conns.values())

    def get(self, conn_id: str) -> ConnectionConfig:
        cfg = self._conns.get(conn_id)
        if cfg is None:
            raise KeyError(f"连接不存在: {conn_id}")
        return cfg

    def create(self, data: dict[str, Any]) -> ConnectionConfig:
        cfg = ConnectionConfig(
            id=uuid.uuid4().hex[:12],
            name=data.get("name") or "未命名连接",
            dialect=data.get("dialect") or "sqlite",
            host=data.get("host") or "",
            port=data.get("port"),
            user=data.get("user") or "",
            password=data.get("password") or "",
            database=data.get("database") or "",
            file=data.get("file") or "",
            ssl=bool(data.get("ssl", False)),
            read_only=bool(data.get("read_only", False)),
            timeout=int(data.get("timeout") or 10),
            credential_ref=data.get("credential_ref"),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            sensitive=list(data.get("sensitive") or []),
        )
        self._conns[cfg.id] = cfg
        self.save()
        return cfg

    def set_kb_status(self, conn_id: str, status: str) -> None:
        cfg = self.get(conn_id)
        cfg.kb_status = status
        if status != "building":
            cfg.kb_updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self.save()
        except PermissionError:
            import logging
            logging.getLogger(__name__).warning("连接状态写盘失败：%s", self._path)

    def update(self, conn_id: str, patch: dict[str, Any]) -> ConnectionConfig:
        cfg = self.get(conn_id)
        allowed = {
            "name", "dialect", "host", "port", "user", "password", "database",
            "file", "ssl", "read_only", "timeout", "credential_ref", "sensitive",
        }
        for k, v in patch.items():
            if k in allowed and v is not None:
                setattr(cfg, k, v)
        self.save()
        return cfg

    def delete(self, conn_id: str) -> None:
        if conn_id in self._conns:
            del self._conns[conn_id]
            self.save()

    def public(self, cfg: ConnectionConfig) -> dict[str, Any]:
        d = asdict(cfg)
        d["password"] = "•••" if d["password"] else ""
        return d
