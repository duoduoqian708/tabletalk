"""应用级依赖容器：供 API/AI/核心模块访问共享服务。"""
from __future__ import annotations

from dataclasses import dataclass

from app.audit.logger import AuditLogger
from app.config import Settings, reset_env
from app.core.chat_store import ChatStore
from app.core.connections import ConnectionRegistry
from app.core.pool import PoolManager
from app.core.settings import SettingsStore
from app.knowledge.store import KnowledgeBase


@dataclass
class AppState:
    env: Settings
    runtime: SettingsStore
    connections: ConnectionRegistry
    pools: PoolManager
    audit: AuditLogger
    knowledge: KnowledgeBase
    chats: ChatStore


_state: AppState | None = None


def _build_state(data_dir=None) -> AppState:
    env = reset_env(data_dir)
    connections = ConnectionRegistry(env.data_dir)
    runtime = SettingsStore(env.data_dir)
    return AppState(
        env=env,
        runtime=runtime,
        connections=connections,
        pools=PoolManager(connections),
        audit=AuditLogger(env.data_dir),
        knowledge=KnowledgeBase(env.data_dir, runtime=runtime),
        chats=ChatStore(env.data_dir),
    )


def init_state() -> AppState:
    global _state
    if _state is None:
        _state = _build_state()
    return _state


def reset_state(data_dir=None) -> AppState:
    """测试用：重建应用状态（可指定独立数据目录）。"""
    global _state
    _state = _build_state(data_dir)
    return _state


def get_state() -> AppState:
    if _state is None:
        return init_state()
    return _state
