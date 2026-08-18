"""兼容层：resolve_provider_cfg 已迁入 app.ai.gateway，本文件仅为向后兼容转发。

TODO: 随 skill 化重构清理，直接 import 自 app.ai.gateway。
"""
from __future__ import annotations

from app.ai.gateway import resolve_provider_cfg  # noqa: F401
