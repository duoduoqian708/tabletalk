"""进程级环境配置（端口、数据目录、env 默认值）。

可编辑的运行时配置（AI 网关、闸门参数）见 app.core.settings.RuntimeSettings。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def _migrate_legacy_data_dir(data_dir: Path, legacy_dir: Path | None = None) -> None:
    """旧版数据目录（~/.cleared）→ 新版（~/.tabletalk）一次性迁移。

    - 仅在目标目录不存在且旧目录存在时触发（复制保留旧目录作备份，不删除）
    - sidecar.token 文件名同步为 tabletalk.token
    - "仅默认数据目录生效"由调用方（__post_init__）判断
    """
    if data_dir.exists():
        return
    legacy = legacy_dir or _expand(os.environ.get("CLEARED_DATA_DIR", "~/.cleared"))  # noqa: S105 - 旧 env 兼容
    if not legacy.exists():
        return
    try:
        import shutil
        import logging

        shutil.copytree(legacy, data_dir)
        old_token = data_dir / "sidecar.token"
        if old_token.exists():
            old_token.rename(data_dir / "tabletalk.token")
        _rewrite_connection_files(data_dir, legacy)
        logging.getLogger(__name__).info("数据目录迁移完成：%s → %s（旧目录保留）", legacy, data_dir)
    except Exception as e:  # noqa: BLE001 - 只读目录/沙箱：迁移失败不致命
        import logging

        logging.getLogger(__name__).warning("数据目录迁移失败：%s（%s）", legacy, e)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def _rewrite_connection_files(data_dir: Path, legacy: Path) -> None:
    """迁移后改写连接配置里的 SQLite 文件路径：旧数据目录 → 新数据目录。

    连接配置存的是绝对路径（如 ~/.cleared/demo.db），迁移后应指向新目录。
    """
    conn_path = data_dir / "connections.json"
    if not conn_path.exists():
        return
    try:
        import json as _json

        data = _json.loads(conn_path.read_text(encoding="utf-8"))
        legacy_prefix = str(legacy).rstrip("/") + "/"
        new_prefix = str(data_dir).rstrip("/") + "/"
        changed = False
        for c in data:
            f = c.get("file", "")
            if f.startswith(legacy_prefix):
                c["file"] = new_prefix + f[len(legacy_prefix):]
                changed = True
        if changed:
            conn_path.write_text(
                _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception:  # noqa: BLE001 - 改写失败不致命
        pass


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8777
    data_dir: Path = field(default_factory=lambda: _expand(os.environ.get("TABLETALK_DATA_DIR", "~/.tabletalk")))
    # 前端 SPA 构建产物目录（相对 backend cwd）；后端同源托管
    web_dist: Path = field(default_factory=lambda: Path(os.environ.get("TABLETALK_WEB_DIST", "../frontend/dist")))
    cors_origins: list[str] = field(default_factory=lambda: ["*"])

    # AI 网关默认值（可被 runtime settings 覆盖）
    ai_provider: str = "mock"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = "deepseek-v4-flash"
    # 推理思考开关（默认开；仅对支持推理的模型生效）
    ai_reasoning: bool = True
    ai_temperature: float = 0.2
    ai_timeout: float = 120.0

    # 查询
    query_max_rows: int = 1000

    # 连接池：PG/MySQL 每连接并发句柄数（SQLite 恒为 1，单连接串行）
    pool_size: int = 3

    # 影响行数预览（COUNT 同 WHERE）超时秒数；超时返回 None（"无法预估"）
    gate_preview_timeout: float = 3.0

    # sidecar 鉴权 token（TABLETALK_SIDECAR_TOKEN）；为空则启动时生成并写入 data_dir/tabletalk.token
    sidecar_token: str = ""

    # 知识库（可被 runtime settings 覆盖）
    embedding_provider: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "bge-m3"
    embedding_api_key: str = ""
    kb_sample_rows: int = 15
    kb_ai_annotation_samples: bool = False

    def __post_init__(self) -> None:
        # 仅默认数据目录做旧版迁移（用户显式指定 TABLETALK_DATA_DIR 时不迁移）。
        # 守卫必须与字面默认目录比较，而不能与 env 同源比较（否则自定义目录也恒等触发迁移，
        # 会把 ~/.cleared 的连接配置与 API key 灌进隔离/测试目录）。
        if str(self.data_dir) == str(_expand("~/.tabletalk")):
            _migrate_legacy_data_dir(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("TABLETALK_HOST", "127.0.0.1"),
            port=int(os.environ.get("TABLETALK_PORT", "8777")),
            data_dir=_expand(os.environ.get("TABLETALK_DATA_DIR", "~/.tabletalk")),
            web_dist=Path(os.environ.get("TABLETALK_WEB_DIST", "../frontend/dist")),
            ai_provider=os.environ.get("TABLETALK_AI_PROVIDER", ""),
            ai_base_url=os.environ.get("TABLETALK_AI_BASE_URL", ""),
            ai_api_key=os.environ.get("TABLETALK_AI_API_KEY", ""),
            ai_model=os.environ.get("TABLETALK_AI_MODEL", "deepseek-v4-flash"),
            ai_reasoning=os.environ.get("TABLETALK_AI_REASONING", "1") == "1",
            ai_temperature=float(os.environ.get("TABLETALK_AI_TEMPERATURE", "0.2")),
            ai_timeout=float(os.environ.get("TABLETALK_AI_TIMEOUT", "120")),
            query_max_rows=int(os.environ.get("TABLETALK_QUERY_MAX_ROWS", "1000")),
            pool_size=int(os.environ.get("TABLETALK_POOL_SIZE", "3")),
            gate_preview_timeout=float(os.environ.get("TABLETALK_GATE_PREVIEW_TIMEOUT", "3.0")),
            sidecar_token=os.environ.get("TABLETALK_SIDECAR_TOKEN", ""),
            embedding_provider=os.environ.get("TABLETALK_EMBEDDING_PROVIDER", ""),
            embedding_base_url=os.environ.get("TABLETALK_EMBEDDING_BASE_URL", ""),
            embedding_model=os.environ.get("TABLETALK_EMBEDDING_MODEL", "bge-m3"),
            embedding_api_key=os.environ.get("TABLETALK_EMBEDDING_API_KEY", ""),
            kb_sample_rows=int(os.environ.get("TABLETALK_KB_SAMPLE_ROWS", "15")),
            kb_ai_annotation_samples=os.environ.get("TABLETALK_KB_AI_ANNOTATION_SAMPLES", "") == "1",
        )


_settings: Settings | None = None


def get_env() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_env(data_dir: Path | None = None) -> Settings:
    """测试用：重建环境配置（可指定数据目录）。"""
    global _settings
    s = Settings.from_env()
    if data_dir is not None:
        s.data_dir = Path(data_dir)
        s.data_dir.mkdir(parents=True, exist_ok=True)
    _settings = s
    return s


_token: str | None = None


def get_token() -> str:
    """sidecar 鉴权 token：env 提供则用之；否则生成一次并持久化到 data_dir/tabletalk.token。

    Electron 拉起 sidecar 时读取该文件（或直接通过 TABLETALK_SIDECAR_TOKEN 传入）。
    """
    global _token
    if _token is None:
        env = get_env()
        if env.sidecar_token:
            _token = env.sidecar_token
        else:
            path = env.data_dir / "tabletalk.token"
            _token = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            if not _token:
                _token = secrets.token_urlsafe(32)
                path.write_text(_token, encoding="utf-8")
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
    return _token
