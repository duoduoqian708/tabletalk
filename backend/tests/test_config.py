"""config 层测试：sidecar 鉴权 token 的生成与持久化。"""
from __future__ import annotations

import app.config as cfg


def test_get_token_persists_to_file(tmp_path, monkeypatch):
    cfg._token = None
    monkeypatch.delenv("CLEARED_SIDECAR_TOKEN", raising=False)
    s = cfg.reset_env(tmp_path)
    tok = cfg.get_token()
    assert tok
    f = tmp_path / "sidecar.token"
    assert f.exists()
    assert f.read_text(encoding="utf-8").strip() == tok
    # 幂等：再次调用返回同一 token
    assert cfg.get_token() == tok
    # 权限收紧到 0600
    assert (f.stat().st_mode & 0o777) == 0o600


def test_get_token_uses_env_when_set(tmp_path, monkeypatch):
    cfg._token = None
    monkeypatch.setenv("CLEARED_SIDECAR_TOKEN", "env-token")
    cfg.reset_env(tmp_path)  # 重建 settings，使 env token 生效
    assert cfg.get_token() == "env-token"
    # env token 时不落盘
    assert not (tmp_path / "sidecar.token").exists()
    cfg._token = None
