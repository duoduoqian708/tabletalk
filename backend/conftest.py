"""把 backend 根目录加入 sys.path，使 app/ 与 scripts/ 可导入。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
