"""Configuration-name consistency (W3): the worker runtime's tunnel token is
WORKER_TUNNEL_TOKEN. UAHT_TUNNEL_TOKEN is the *installer input* name only;
scripts/install-host.sh maps it into worker.env as WORKER_TUNNEL_TOKEN.
The worker must load exactly one runtime name and must not read the
operator-facing UAHT_ name.
"""
import os

import pytest

from agent.config import WorkerConfig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("WORKER_TUNNEL_TOKEN", "UAHT_TUNNEL_TOKEN", "WORKER_CONFIG"):
        monkeypatch.delenv(key, raising=False)


def _write_env_file(tmp_path, body: str) -> str:
    path = tmp_path / "worker.env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_tunnel_token_from_worker_env_var(monkeypatch):
    monkeypatch.setenv("WORKER_TUNNEL_TOKEN", "env-token-123")
    cfg = WorkerConfig.load(config_path="/nonexistent-worker.env")
    assert cfg.tunnel_token == "env-token-123"


def test_tunnel_token_from_worker_env_file(tmp_path):
    path = _write_env_file(tmp_path, "WORKER_TUNNEL_TOKEN=file-token-456\n")
    cfg = WorkerConfig.load(config_path=path)
    assert cfg.tunnel_token == "file-token-456"


def test_tunnel_token_env_overrides_file(tmp_path, monkeypatch):
    path = _write_env_file(tmp_path, "WORKER_TUNNEL_TOKEN=file-token\n")
    monkeypatch.setenv("WORKER_TUNNEL_TOKEN", "env-token")
    cfg = WorkerConfig.load(config_path=path)
    assert cfg.tunnel_token == "env-token"


def test_uaht_tunnel_token_env_is_not_read(monkeypatch):
    # The installer input name must not leak into worker runtime config.
    # If an operator exports the installer input directly, the worker
    # reports the tunnel token as missing (naming WORKER_TUNNEL_TOKEN),
    # not silently.
    monkeypatch.setenv("UAHT_TUNNEL_TOKEN", "installer-input-token")
    cfg = WorkerConfig.load(config_path="/nonexistent-worker.env")
    assert cfg.tunnel_token == ""
    assert "installer-input-token" not in repr(cfg.redacted())


def test_legacy_uaht_file_key_is_not_read(tmp_path):
    path = _write_env_file(tmp_path, "UAHT_TUNNEL_TOKEN=old-file-token\n")
    cfg = WorkerConfig.load(config_path=path)
    assert cfg.tunnel_token == ""


def test_tunnel_token_redacted_in_logs(monkeypatch):
    monkeypatch.setenv("WORKER_TUNNEL_TOKEN", "super-secret")
    cfg = WorkerConfig.load(config_path="/nonexistent-worker.env")
    redacted = cfg.redacted()
    assert redacted["tunnel_token"] == "***"
    assert "super-secret" not in str(redacted)


def test_worker_env_uses_worker_prefix_for_all_runtime_keys(tmp_path):
    # Every runtime key in a worker.env file must be WORKER_-prefixed;
    # the loader normalizes them to unprefixed field names.
    path = _write_env_file(
        tmp_path,
        "WORKER_CONTROL_PLANE_URL=https://cp.example.com\n"
        "WORKER_HOST_NAME=h1\n"
        "WORKER_HOST_TOKEN=tok\n"
        "WORKER_HOST_ID=hid\n"
        "WORKER_TUNNEL_TOKEN=t\n",
    )
    cfg = WorkerConfig.load(config_path=path)
    assert cfg.control_plane_url == "https://cp.example.com"
    assert cfg.host_name == "h1"
    assert cfg.tunnel_token == "t"
    assert os.environ.get("WORKER_CONFIG") is None  # fixture hygiene
