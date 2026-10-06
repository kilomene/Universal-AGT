"""Tunnel run-token handling (§25): never on a command line, safe rotation.

- _spawn hands the token to cloudflared through the TUNNEL_TOKEN
  environment variable — argv must never carry it (argv is world-readable
  via `ps` / /proc/<pid>/cmdline; the child environment is only readable
  by the same UID).
- Rotation model: the token is read once at provider setup; a rotated
  token takes effect when the provider is set up again (worker restart).
  The running tunnel keeps the old token until then — never a mix.
- §27: provider routes terminate ONLY at the literal 127.0.0.1 —
  "localhost" and other variants are refused.
"""
import pytest

from ingress import IngressConfig, IngressError, _check_route
from ingress import cloudflared as cf
from ingress.cloudflared import CloudflaredTunnelProvider


class _SubprocessStub:
    """Stands in for the subprocess module inside ingress.cloudflared."""

    # Sentinel values mirroring the real subprocess constants.
    STDOUT = "STDOUT"
    DEVNULL = "DEVNULL"

    def __init__(self):
        self.spawns = []

    def Popen(self, argv, **kwargs):  # noqa: N802 (mirrors subprocess.Popen)
        self.spawns.append({"argv": list(argv), "kwargs": dict(kwargs)})

        class _Proc:
            pid = 4242

            def poll(self):
                return None

        return _Proc()


def _provider_with_token(tmp_path, monkeypatch, token):
    # Stub the binary lookup so setup never touches the network.
    monkeypatch.setattr(cf.shutil, "which", lambda _name: "/usr/bin/cloudflared")
    p = CloudflaredTunnelProvider()
    cfg = IngressConfig(enabled=True, provider="cloudflare-tunnel",
                        tunnel_token=token, work_dir=str(tmp_path))
    p.setup(cfg)
    return p


def _capture_spawns(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(cf, "subprocess", stub)
    return stub


def test_spawn_passes_token_via_env_never_argv(tmp_path, monkeypatch):
    p = _provider_with_token(tmp_path, monkeypatch, "tok-aaa-secret")
    stub = _capture_spawns(monkeypatch)
    p._spawn()
    assert len(stub.spawns) == 1
    spawn = stub.spawns[0]
    # argv carries no secret: exactly [binary, tunnel, run]
    assert spawn["argv"] == ["/usr/bin/cloudflared", "tunnel", "run"]
    assert "tok-aaa-secret" not in " ".join(spawn["argv"])
    assert not any("tok-aaa-secret" in str(a) for a in spawn["argv"])
    # ... the token travels in the child environment instead
    env = spawn["kwargs"]["env"]
    assert env["TUNNEL_TOKEN"] == "tok-aaa-secret"


def test_token_rotation_takes_effect_on_re_setup_only(tmp_path, monkeypatch):
    # Rotation = new token in config + provider set up again (what a worker
    # restart does). The new spawn uses the new token; a provider instance
    # that was set up with the old token keeps spawning with the old one —
    # there is never a half-rotated mix, and rotation never touches the
    # already-running tunnel process.
    old = _provider_with_token(tmp_path, monkeypatch, "tok-old")
    stub = _capture_spawns(monkeypatch)
    old._spawn()
    assert stub.spawns[0]["kwargs"]["env"]["TUNNEL_TOKEN"] == "tok-old"

    new = _provider_with_token(tmp_path, monkeypatch, "tok-new")
    new._spawn()
    assert stub.spawns[1]["kwargs"]["env"]["TUNNEL_TOKEN"] == "tok-new"

    # The old provider instance is unaffected by the rotation.
    old._spawn()
    assert stub.spawns[2]["kwargs"]["env"]["TUNNEL_TOKEN"] == "tok-old"


def test_rotated_token_never_in_argv(tmp_path, monkeypatch):
    p = _provider_with_token(tmp_path, monkeypatch, "tok-rotated-xyz")
    stub = _capture_spawns(monkeypatch)
    p._spawn()
    argv_text = " ".join(stub.spawns[0]["argv"])
    assert "tok-rotated-xyz" not in argv_text
    assert "--token" not in stub.spawns[0]["argv"]


@pytest.mark.parametrize("bad", [
    "localhost",
    "LOCALHOST",
    "::1",
    "0.0.0.0",
    "10.0.0.5",
    "172.16.0.9",
    "192.168.1.7",
    "169.254.169.254",
    "127.0.0.1.evil.com",
    "",
])
def test_check_route_rejects_non_exact_loopback_targets(bad):
    with pytest.raises(IngressError):
        _check_route("api.example.com", bad, 8080)


def test_check_route_accepts_exact_loopback():
    _check_route("api.example.com", "127.0.0.1", 8080)  # must not raise
