"""config 层测试：sidecar 鉴权 token 的生成与持久化。"""
from __future__ import annotations

import app.config as cfg


def test_get_token_persists_to_file(tmp_path, monkeypatch):
    cfg._token = None
    monkeypatch.delenv("TABLETALK_SIDECAR_TOKEN", raising=False)
    s = cfg.reset_env(tmp_path)
    tok = cfg.get_token()
    assert tok
    f = tmp_path / "tabletalk.token"
    assert f.exists()
    assert f.read_text(encoding="utf-8").strip() == tok
    # 幂等：再次调用返回同一 token
    assert cfg.get_token() == tok
    # 权限收紧到 0600
    assert (f.stat().st_mode & 0o777) == 0o600


def test_get_token_uses_env_when_set(tmp_path, monkeypatch):
    cfg._token = None
    monkeypatch.setenv("TABLETALK_SIDECAR_TOKEN", "env-token")
    cfg.reset_env(tmp_path)  # 重建 settings，使 env token 生效
    assert cfg.get_token() == "env-token"
    # env token 时不落盘
    assert not (tmp_path / "tabletalk.token").exists()
    cfg._token = None


async def test_migrate_legacy_data_dir(tmp_path):
    """~/.cleared → ~/.tabletalk 迁移：复制内容 + sidecar.token 改名 + 幂等。"""
    from app.config import _migrate_legacy_data_dir

    legacy = tmp_path / "old"
    legacy.mkdir()
    (legacy / "connections.json").write_text("[]", encoding="utf-8")
    (legacy / "sidecar.token").write_text("tok123", encoding="utf-8")
    (legacy / "demo.db").write_bytes(b"\x00\x01")

    target = tmp_path / "new"
    _migrate_legacy_data_dir(target, legacy_dir=legacy)
    assert (target / "connections.json").read_text(encoding="utf-8") == "[]"
    assert (target / "demo.db").read_bytes() == b"\x00\x01"
    # token 文件名同步
    assert not (target / "sidecar.token").exists()
    assert (target / "tabletalk.token").read_text(encoding="utf-8") == "tok123"
    # 旧目录保留（备份不删除）
    assert legacy.exists()
    # 幂等：目标已存在不再迁移
    (target / "connections.json").write_text("[1]", encoding="utf-8")
    _migrate_legacy_data_dir(target, legacy_dir=legacy)
    assert (target / "connections.json").read_text(encoding="utf-8") == "[1]"
