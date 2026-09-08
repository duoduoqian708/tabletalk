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
from app.core.results_store import ResultsStore
from app.core.settings import SettingsStore
from app.knowledge.jobs import BuildJobManager, SyncLoop
from app.knowledge.facade import KnowledgeBase
from app.tasks.jobs import JobRegistry
from app.tasks.scheduler import JobScheduler
from app.tasks.store import JobStore


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
    jobs: JobRegistry
    job_store: JobStore
    job_scheduler: JobScheduler
    results: "ResultsStore"


_state: AppState | None = None


def _build_state(data_dir=None) -> AppState:
    env = reset_env(data_dir)
    connections = ConnectionRegistry(env.data_dir)
    # 测试污染治理（2026-09 加严）：
    # ① pytest 临时路径（pytest-of-mac）的连接无条件删除——pytest 目录绝不属于真实用户连接，
    #    不再要求"文件已消失"（macOS pytest 保留最近 3 轮，旧逻辑导致泄漏连接长期滞留）；
    # ② /tmp 下文件已消失的僵尸连接照旧清理；
    # ③ 孤儿 KB 工件：conn_id 已不存在的 knowledge-{id}.db 一并删除（52 → 存活连接数）。
    try:
        from pathlib import Path as _P
        import re as _re
        pytest_pat = _re.compile(r"/pytest-of-[^/]+/|/\.pytest_cache/")
        alive_ids: set[str] = set()
        for c in list(connections.list()):
            f = (c.file or "").strip()
            if f and (pytest_pat.search(f) or (("/tmp/" in f or f.startswith("/private/var/folders/")) and not _P(f).exists())):
                try:
                    connections.delete(c.id)
                    continue
                except Exception:
                    pass
            alive_ids.add(c.id)
        # 孤儿知识库工件（连接已删但 artifact 残留）
        for art in _P(env.data_dir).glob("knowledge-*.db"):
            if art.stem.replace("knowledge-", "") not in alive_ids:
                try:
                    art.unlink()
                except Exception:
                    pass
    except Exception:
        pass
    runtime = SettingsStore(env.data_dir)
    from app.tasks.migrate import migrate_legacy_tasks
    from app.tasks.seed import seed_jobs_dir

    seed_jobs_dir(env.data_dir)  # 播种 jobs/（SDK lib.py + 系统保留脚本），幂等
    migrate_legacy_tasks(env.data_dir)  # 旧 tasks.db → jobs/*.py（一次性）
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
        jobs=JobRegistry(env.data_dir),
        job_store=JobStore(env.data_dir),
        job_scheduler=JobScheduler(),
        results=ResultsStore(env.data_dir),
    )


def _migrate_kb_status(knowledge, connections) -> None:
    """状态机迁移：kb_status 字段是新加的；已有知识库 artifact 的老连接置为 ready，
    避免"构建过却卡死"。artifact 不存在的连接保持 none（走新接入流程）。
    另外：进程崩溃会把持久化的 building 带到下次启动——重启后无任务可取消，
    一律归位 none（草案如已落盘，重新构建即可恢复，或由用户重新构建覆盖）。"""
    for c in connections.list():
        if c.kb_status == "none" and knowledge.is_built(c.id):
            connections.set_kb_status(c.id, "ready")
        elif c.kb_status == "building":
            connections.set_kb_status(c.id, "none")


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
