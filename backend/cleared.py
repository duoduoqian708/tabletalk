"""CLEARED 一条命令启动器。

用法：`python cleared.py`  →  自动选空闲端口 → 启动 sidecar → 打开浏览器。

读取 backend/app.config 的环境变量（CLEARED_PORT / CLEARED_DATA_DIR / CLEARED_HOST），
默认 127.0.0.1:8765；端口被占用时自动向后探测空闲端口。前端 dist 需已构建
（`cd frontend && npm run build`），否则提示。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
WEB_DIST = BACKEND_DIR.parent / "frontend" / "dist"


def _find_free_port(start: int = 8765) -> int:
    """返回从 start 起第一个空闲端口。"""
    for port in range(start, start + 20):
        if not _is_used("127.0.0.1", port):
            return port
    raise RuntimeError(f"端口 {start}~{start+19} 均被占用")


def _is_used(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def _open_browser(url: str) -> None:
    def _open() -> None:
        time.sleep(1.0)  # 等服务真正起来
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> int:
    print("CLEARED · 本地 AI 优先数据库查询工具")
    if not WEB_DIST.exists():
        print(f"⚠ 前端未构建（未找到 {WEB_DIST}）。\n  请先运行：cd frontend && npm run build")
        return 1

    sys.path.insert(0, str(BACKEND_DIR))
    import uvicorn
    from app.config import get_env

    env = get_env()
    host, port = env.host, env.port
    port = _find_free_port(port)   # 配置端口被占则向后探测空闲；空闲则返回原值
    print(f"· 前端已就绪：{WEB_DIST}")
    print(f"· 服务地址：http://{host}:{port}")
    open_ok = _env("CLEARED_NO_OPEN", "") != "1"
    if open_ok:
        _open_browser(f"http://{host}:{port}")
        print("· 已尝试打开浏览器（停止用 Ctrl+C）。")
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
    return 0


def _env(key: str, default: str = "") -> str:
    import os

    return os.environ.get(key, default)


if __name__ == "__main__":
    raise SystemExit(main())
