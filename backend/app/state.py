"""应用级依赖容器：供 API/AI/核心模块访问共享服务。"""
from __future__ import annotations

from dataclasses import dataclass

from app.audit.logger import AuditLogger
from app.config import Settings, reset_env
from app.core.chat_store import ChatStore
from app.core.connections import ConnectionRegistry
from app.core.pool import PoolManager
from app.core.approvals import ApprovalStore
from app.core.auth import AuthStore
from app.core.questions import QuestionStore
from app.core.settings import SettingsStore
from app.knowledge.jobs import BuildJobManager, SyncLoop
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
    questions: QuestionStore
    auth: AuthStore
    approvals: ApprovalStore
    build_jobs: "BuildJobManager"
    sync_loop: "SyncLoop"


_state: AppState | None = None


def _build_state(data_dir=None) -> AppState:
    env = reset_env(data_dir)
    from app.ai.skills.registry import load_custom
    load_custom(env.data_dir)  # 恢复自定义技能（技能广场持久化）
    connections = ConnectionRegistry(env.data_dir)
    # 性能/污染修复：启动时清理指向已消失临时文件的僵尸连接（pytest 污染，102 条变 3 条的根因）
    try:
        from pathlib import Path as _P
        for c in list(connections.list()):
            f = (c.file or "").strip()
            if not f:
                continue
            # 仅清理明显的临时/pytest 路径且文件已不存在的连接
            if ("/pytest" in f or f.startswith("/private/var/folders/") or f.startswith("/tmp/")) and not _P(f).exists():
                try:
                    connections.delete(c.id)
                except Exception:
                    pass
    except Exception:
        pass
    runtime = SettingsStore(env.data_dir)
    knowledge = KnowledgeBase(env.data_dir, runtime=runtime)
    _migrate_kb_status(knowledge, connections)  # 老连接：artifact 已存在 → 视为已就绪
    return AppState(
        env=env,
        runtime=runtime,
        connections=connections,
        pools=PoolManager(connections),
        audit=AuditLogger(env.data_dir),
        knowledge=knowledge,
        chats=ChatStore(env.data_dir),
        questions=QuestionStore(env.data_dir),
        auth=AuthStore(env.data_dir),
        approvals=ApprovalStore(env.data_dir),
        build_jobs=BuildJobManager(),
        sync_loop=SyncLoop(),
    )


def _migrate_kb_status(knowledge, connections) -> None:
    """状态机迁移：kb_status 字段是新加的；已有知识库 artifact 的老连接置为 ready，
    避免"构建过却卡死"。artifact 不存在的连接保持 none（走新接入流程）。"""
    for c in connections.list():
        if c.kb_status == "none" and knowledge.is_built(c.id):
            connections.set_kb_status(c.id, "ready")


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
