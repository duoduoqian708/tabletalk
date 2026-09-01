"""tabletalk sidecar — FastAPI 应用入口。

运行：uvicorn app.main:app --reload --port 8777
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import ai, approvals, audit, auth, connections, cost, health, knowledge, query, questions, safety, schema, settings, skills, suggestions, tasks, usage
from app.config import get_env, get_token
from app.state import get_state

# 导入方言模块以完成注册（新增数据库：写适配器 + 在此导入）
import app.core.dialects.mysql  # noqa: F401
import app.core.dialects.postgres  # noqa: F401
import app.core.dialects.sqlite  # noqa: F401


def _ensure_demo_db() -> None:
    """首次启动自动播种演示库（原 Electron ensureDemoDb 迁入后端）。失败不阻塞启动。"""
    try:
        from scripts.seed_demo_db import build_demo_db

        db = get_env().data_dir / "demo.db"
        if not db.exists():
            build_demo_db(db)
    except Exception as e:
        logging.getLogger(__name__).warning("演示库播种失败（不阻塞启动）：%s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = get_state()
    get_token()  # 启动即生成/读取鉴权 token，写入 data_dir/tabletalk.token，供 /bootstrap 读取
    _ensure_demo_db()
    from app.ai.tools import validate_registry
    validate_registry()  # WS7 T7.1：工具 trust 元数据启动自检
    # 启动时清理过期 LLM 日志和成本日志（默认保留30天）
    try:
        from app.ai.llm_log import LlmCallLog
        from app.ai.cost_tracker import CostTracker
        LlmCallLog(get_env().data_dir).cleanup(keep_days=30)
        CostTracker(get_env().data_dir).cleanup(keep_days=30)
    except Exception:
        pass
    state.sync_loop.start()  # 知识库增量同步周期任务（kb_sync_minutes）
    yield
    state.sync_loop.stop()
    await state.pools.close_all()


def create_app() -> FastAPI:
    # 根日志无 handler 时配置一次（幂等守卫）：否则模块 logger 的 INFO 不会显示
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.environ.get("TABLETALK_LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    env = get_env()
    app = FastAPI(title="tabletalk sidecar", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=env.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def no_cache_html(request: Request, call_next):
        """SPA 外壳(index.html)完全禁止缓存，避免浏览器长期持有指向旧 JS 的 HTML；
        带 hash 的静态资源(js/css)保持默认缓存即可。"""
        resp = await call_next(request)
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.middleware("http")
    async def sidecar_token_guard(request: Request, call_next):
        """全接口强鉴权：除 health 与 POST /auth/login 外均需登录（为企业版预留 per-user 钩子）。"""
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if not path.startswith("/api/"):
            return await call_next(request)
        if path == "/api/v1/health":
            return await call_next(request)
        if path == "/api/v1/auth/login":
            return await call_next(request)
        # 个人版：bootstrap 首次用于取 token 以便 login，允许免鉴权（后续所有业务接口均需 JWT）
        if path == "/api/v1/bootstrap":
            return await call_next(request)
        # 兼容：auth/register/change-password 免鉴权（首个用户/改密）
        if path in ("/api/v1/auth/register", "/api/v1/auth/change-password"):
            return await call_next(request)
        # 兼容：X-TableTalk-Token 单共享密钥（单机） + Bearer JWT（团队）
        supplied = request.headers.get("X-TableTalk-Token", "") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        # 单机 token
        if supplied and hmac.compare_digest(supplied, get_token()):
            request.state.user = None
            request.state.user_id = None
            return await call_next(request)
        # 团队 JWT
        try:
            from app.state import get_state as _gs2
            payload = _gs2().auth.verify_token(supplied) if supplied else None
            if payload:
                request.state.user = payload
                request.state.user_id = payload.get("sub")
                return await call_next(request)
        except Exception:
            pass
        return JSONResponse(status_code=401, content={"detail": "missing or invalid sidecar token"})

    for r in (health.router, connections.router, schema.router, query.router,
              audit.router, settings.router, ai.router, knowledge.router, skills.router, questions.router, auth.router, approvals.router, usage.router,
              tasks.router, cost.router, suggestions.router, safety.router):
        app.include_router(r)

    # 同源托管前端 SPA（路由先注册先匹配；StaticFiles 兜底未匹配路径）
    web_dist = get_env().web_dist
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:

        @app.get("/", include_in_schema=False)
        async def web_unbuilt() -> HTMLResponse:
            return HTMLResponse(
                "<h1>tabletalk</h1><p>前端未构建：在 frontend/ 下执行 <code>npm run build</code> 后刷新。</p>"
            )

    return app


app = create_app()
