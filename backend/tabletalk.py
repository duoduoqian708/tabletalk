"""tabletalk 一键启动器：从零到可用一条命令。

用法：
    python tabletalk.py            # 自动完成：venv → 依赖 → 前端构建 → 起 sidecar → 开浏览器
    python tabletalk.py --check    # 只检查环境（venv/依赖/前端），不起服务

流程（全部幂等，重复运行秒起）：
  1. 确保 backend/.venv 存在（无则 python3 -m venv 创建）
  2. 确保依赖已装（pip install -r requirements.txt，已满足秒过——含 numpy / sqlite-vec 等
     嵌入式组件，用户无需手动部署任何服务）
  3. 确保前端已构建（dist 缺失或源码更新则 npm install + npm run build）
  4. 自动选空闲端口 → 启动 sidecar → 打开浏览器

读取 backend/app.config 的环境变量（TABLETALK_PORT / TABLETALK_DATA_DIR / TABLETALK_HOST），
默认 127.0.0.1:8765；端口被占用时自动向后探测空闲端口。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
WEB_DIST = ROOT / "frontend" / "dist"
FRONTEND_DIR = ROOT / "frontend"
VENV_DIR = BACKEND_DIR / ".venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQ = BACKEND_DIR / "requirements.txt"

_c = lambda s: f"\033[36m{s}\033[0m"  # noqa: E731
_g = lambda s: f"\033[32m{s}\033[0m"  # noqa: E731
_y = lambda s: f"\033[33m{s}\033[0m"  # noqa: E731


def _run(cmd: list[str], cwd: Path, quiet: bool = False) -> int:
    print(_c("$ " + " ".join(str(x) for x in cmd)))
    return subprocess.run(cmd, cwd=str(cwd), capture_output=quiet).returncode


# ---------------------------------------------------------------- 1. venv

def _ensure_venv() -> Path:
    if VENV_PY.exists():
        print(_g("✓ venv 已就绪 ") + str(VENV_PY))
        return VENV_PY
    print(_y("· 创建虚拟环境 backend/.venv …"))
    py = os.environ.get("TABLETALK_PYTHON", "python3")
    if _run([py, "-m", "venv", str(VENV_DIR)], BACKEND_DIR) != 0:
        raise SystemExit("创建 venv 失败：请确认已安装 Python 3.11+")
    print(_g("✓ venv 创建完成"))
    return VENV_PY


# ---------------------------------------------------------------- 2. 依赖（含嵌入式组件）

def _ensure_deps(venv_py: Path) -> None:
    print(_c("· 检查依赖（requirements.txt）…"))
    ok = _run([str(venv_py), "-m", "pip", "install", "-q", "-r", str(REQ)], BACKEND_DIR, quiet=True) == 0
    if not ok:
        print(_y("· 增量安装依赖…"))
        if _run([str(venv_py), "-m", "pip", "install", "-r", str(REQ)], BACKEND_DIR) != 0:
            raise SystemExit("依赖安装失败：请检查网络后重试")
    print(_g("✓ 依赖就绪（含 numpy / sqlite-vec 嵌入式组件，无需部署外部服务）"))


# ---------------------------------------------------------------- 3. 前端构建

def _frontend_stale() -> bool:
    if not WEB_DIST.exists():
        return True
    idx = WEB_DIST / "index.html"
    if not idx.exists():
        return True
    try:
        src_newest = max(
            (p.stat().st_mtime for p in (FRONTEND_DIR / "src").rglob("*") if p.is_file()),
            default=0,
        )
    except Exception:
        src_newest = 0
    return src_newest > idx.stat().st_mtime + 5  # 源码比构建新 5 秒以上 → 需重建


def _ensure_frontend() -> None:
    if not _frontend_stale():
        print(_g("✓ 前端已构建 ") + str(WEB_DIST))
        return
    print(_y("· 构建前端（首次或源码更新）…"))
    if not (FRONTEND_DIR / "node_modules").exists():
        print(_c("$ npm install"))
        if subprocess.run(["npm", "install"], cwd=str(FRONTEND_DIR)).returncode != 0:
            raise SystemExit("npm install 失败：请确认已安装 Node.js 18+")
    print(_c("$ npm run build"))
    if subprocess.run(["npm", "run", "build"], cwd=str(FRONTEND_DIR)).returncode != 0:
        raise SystemExit("前端构建失败")
    print(_g("✓ 前端构建完成"))


# ---------------------------------------------------------------- 4. 启动

def _find_free_port(start: int = 8765) -> int:
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
        time.sleep(1.5)  # 等服务真正起来
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def _wait_health(host: str, port: int, timeout: float = 20.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/v1/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    args = sys.argv[1:]
    check_only = "--check" in args

    print("tabletalk · 本地 AI 优先数据库查询工具（一键启动）")
    venv_py = _ensure_venv()
    _ensure_deps(venv_py)
    _ensure_frontend()
    if check_only:
        print(_g("✓ 环境检查通过：venv / 依赖 / 前端全部就绪"))
        return 0

    # 用 venv 解释器重新执行自身（保证后续 import 的都是 venv 依赖）。
    # 注意：venv/bin/python 是符号链接，executable.resolve() 会指向系统 python——
    # 必须用 sys.prefix 判断（venv 内 sys.prefix = venv 目录）。
    if Path(sys.prefix).resolve() != VENV_DIR.resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), "--internal", *args])

    sys.path.insert(0, str(BACKEND_DIR))
    import uvicorn
    from app.config import get_env

    env = get_env()
    host, port = env.host, env.port
    port = _find_free_port(port)
    print(f"· 服务地址：http://{host}:{port}")
    open_ok = os.environ.get("TABLETALK_NO_OPEN", "") != "1"
    if open_ok:
        _open_browser(f"http://{host}:{port}")
        print("· 已尝试打开浏览器（停止用 Ctrl+C）。")
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
