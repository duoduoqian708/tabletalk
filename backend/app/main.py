"""tabletalk sidecar — FastAPI 应用入口。

运行：uvicorn app.main:app --reload --port 8777
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import ai, audit, connections, health, knowledge, query, schema, settings, skills
from app.config import get_env, get_token
from app.debuglog import dbg  # TODO: 测试后删除
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
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = get_state()
    get_token()  # 启动即生成/读取鉴权 token，写入 data_dir/tabletalk.token，供 /bootstrap 读取
    _ensure_demo_db()
    state.sync_loop.start()  # 知识库增量同步周期任务（kb_sync_minutes）
    yield
    state.sync_loop.stop()
    await state.pools.close_all()


def create_app() -> FastAPI:
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
        """本机鉴权：仅守 /api/*；health 与 bootstrap 免鉴权，静态资源/SPA 放行。"""
        path = request.url.path
        # TODO: 测试后删除——全请求进入/完成日志
        dbg("[http] IN ", request.method, path)
        if request.method == "OPTIONS":
            resp = await call_next(request)
            dbg("[http] OUT", request.method, path, resp.status_code)
            return resp
        if not path.startswith("/api/"):
            resp = await call_next(request)
            dbg("[http] OUT", request.method, path, resp.status_code)
            return resp
        if path in ("/api/v1/health", "/api/v1/bootstrap"):
            resp = await call_next(request)
            dbg("[http] OUT", request.method, path, resp.status_code)
            return resp
        supplied = request.headers.get("X-TableTalk-Token", "")
        if not hmac.compare_digest(supplied, get_token()):
            dbg("[http] 401", path)
            return JSONResponse(status_code=401, content={"detail": "missing or invalid sidecar token"})
        resp = await call_next(request)
        dbg("[http] OUT", request.method, path, resp.status_code)
        return resp

    for r in (health.router, connections.router, schema.router, query.router,
              audit.router, settings.router, ai.router, knowledge.router, skills.router):
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
