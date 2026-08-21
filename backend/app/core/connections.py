"""连接注册表：统一配置模型 + JSON 持久化。

每个连接一个 `dialect` 字段，映射到方言注册表——新增数据库无需改这里。
MVP 凭据明文存 JSON，钥匙串加密走 `credential_ref`（二期 Electron 层）。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.dialects.base import DialectConfig


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
    file: str = ""                 # SQLite 路径
    ssl: bool = False
    read_only: bool = False
    timeout: int = 10
    credential_ref: str | None = None   # 预留：钥匙串凭据引用
    created_at: str = ""
    sensitive: list[str] = field(default_factory=list)   # 敏感表/列 glob 名单，如 ["payroll_*"]
    kb_status: str = "none"              # 知识库状态机：none|building|pending_review|ready
    kb_updated_at: str = ""              # 构建/确认时间

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
        self._path = data_dir / "connections.json"
        self._conns: dict[str, ConnectionConfig] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            # 迁移：若 password 为 ENC@ 格式则解密，否则保持明文（旧库）并在下次 save 时加密
            for item in raw:
                pwd = item.get("password", "")
                if isinstance(pwd, str) and pwd.startswith("ENC@"):
                    try:
                        from app.core.vault import decrypt
                        # data_dir 为 connections.json 的父目录
                        item["password"] = decrypt(pwd, self._path.parent)
                    except Exception:
                        item["password"] = ""
                cfg = ConnectionConfig(**item)
                self._conns[cfg.id] = cfg
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            # 加密 password 再落盘（若有主密钥则加密，否则明文）
            to_save = []
            for c in self._conns.values():
                d = asdict(c)
                pwd = d.get("password", "")
                if pwd and not str(pwd).startswith("ENC@"):
                    try:
                        from app.core.vault import encrypt
                        d["password"] = encrypt(str(pwd), self._path.parent)
                    except Exception:
                        pass
                to_save.append(d)
            self._path.write_text(
                json.dumps(to_save, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                self._path.chmod(0o600)
            except OSError:
                pass

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
        """更新知识库状态机位（none|building|pending_review|ready），持久化。"""
        cfg = self.get(conn_id)
        cfg.kb_status = status
        if status != "building":
            cfg.kb_updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self.save()
        except PermissionError:
            # 只读数据目录/沙箱等极端情况：内存态优先，落盘失败不致命
            import logging
            logging.getLogger(__name__).warning("连接状态写盘失败（只读目录？）：%s", self._path)

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
