"""Phase 7 ingress tests.

Covers:
  * IngressConfig validation (disabled by default; bad provider rejected;
    cloudflare-tunnel without a token -> clear error naming WORKER_TUNNEL_TOKEN)
  * build_routes(): hostname -> 127.0.0.1:port mapping from fake deployment
    states + domain entries, with skip reasons
  * render_cloudflared_config(): exact config.yml rendering
  * the ingress-sync handler with a fake provider (no cloudflared download,
    no subprocess, no network)
  * provider base-class route-table semantics (add/remove/sync diffing,
    loopback-only enforcement)
  * cloudflared provider: disabled-by-default status, checksum table shape,
    unsupported arch refusal

No test here downloads cloudflared or spawns a real tunnel process.
"""
import pytest

from agent.config import ConfigError, WorkerConfig
from executor import handlers
from agent import policy
from ingress import (
    IngressConfig,
    IngressError,
    IngressProvider,
    build_ingress_config,
    build_routes,
    get_provider,
    is_valid_hostname,
    render_cloudflared_config,
)
from ingress import sync as ingress_sync


# ---------------------------------------------------------------------------
# config validation
# ---------------------------------------------------------------------------

def _base_config(**overrides):
    return WorkerConfig(
        control_plane_url="https://cp.example.com",
        host_name="h1",
        host_token="tok",
        host_id="host-1",
        **overrides,
    )


def test_ingress_disabled_by_default():
    cfg = build_ingress_config(_base_config())
    assert cfg.enabled is False
    assert cfg.provider == ""
    assert cfg.tunnel_token == ""
    cfg.validate()  # disabled: no-op, never raises


def test_bad_provider_rejected():
    cfg = build_ingress_config(
        _base_config(ingress_enabled=True, ingress_provider="bogus"))
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate()
    assert "bogus" in str(excinfo.value)
    assert "cloudflare-tunnel" in str(excinfo.value)


def test_missing_token_clear_error():
    cfg = build_ingress_config(
        _base_config(ingress_enabled=True,
                     ingress_provider="cloudflare-tunnel"))
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate()
    msg = str(excinfo.value)
    assert "WORKER_TUNNEL_TOKEN" in msg
    assert "tunnel token" in msg.lower()


def test_valid_tunnel_config_passes():
    cfg = build_ingress_config(
        _base_config(ingress_enabled=True,
                     ingress_provider="cloudflare-tunnel",
                     tunnel_token="secret-token"))
    cfg.validate()  # must not raise
    redacted = cfg.redacted()
    assert redacted["tunnel_token"] == "***"
    assert "secret-token" not in str(redacted)


def test_ingress_enabled_bool_parsing():
    def _load(**kw):
        return WorkerConfig.load(
            config_path="/nonexistent-worker-env",
            control_plane_url="https://cp.example.com",
            host_name="h1", host_token="t", host_id="h",
            **kw,
        )
    assert _load(ingress_enabled="true").ingress_enabled is True
    assert _load(ingress_enabled="1").ingress_enabled is True
    assert _load(ingress_enabled="0").ingress_enabled is False
    assert _load().ingress_enabled is False  # default off
    with pytest.raises(ConfigError):
        _load(ingress_enabled="maybe-not-a-bool")


def test_unknown_provider_lookup_rejected():
    with pytest.raises(IngressError):
        get_provider("teleport-hole")


def test_known_provider_lookup():
    provider = get_provider("cloudflare-tunnel")
    assert isinstance(provider, IngressProvider)
    assert provider.name == "cloudflare-tunnel"
    assert provider.enabled is False


# ---------------------------------------------------------------------------
# route table building
# ---------------------------------------------------------------------------

def _states():
    return [
        {"deployment_id": "dep-1", "host_port": 8080, "status": "running"},
        {"deployment_id": "dep-2", "host_port": 9090, "status": "running"},
        {"deployment_id": "dep-3", "host_port": None, "status": "running"},
        {"deployment_id": "dep-4", "host_port": 7070, "status": "removed"},
    ]


def test_build_routes_mapping_correctness():
    entries = [
        {"deployment_id": "dep-1", "hostname": "api.example.com",
         "ingress": "tunnel"},
        {"deployment_id": "dep-2", "hostname": "www.example.com",
         "ingress": "tunnel"},
    ]
    routes, skipped = build_routes(_states(), entries)
    assert skipped == []
    assert routes == [
        {"hostname": "api.example.com", "target_host": "127.0.0.1",
         "target_port": 8080},
        {"hostname": "www.example.com", "target_host": "127.0.0.1",
         "target_port": 9090},
    ]


def test_build_routes_skips_with_reasons():
    entries = [
        {"deployment_id": "dep-1", "hostname": "direct.example.com",
         "ingress": "direct"},                      # not tunnel mode
        {"deployment_id": "dep-9", "hostname": "gone.example.com",
         "ingress": "tunnel"},                      # unknown deployment
        {"deployment_id": "dep-3", "hostname": "noport.example.com",
         "ingress": "tunnel"},                      # no host port
        {"deployment_id": "dep-4", "hostname": "dead.example.com",
         "ingress": "tunnel"},                      # terminal status
        {"deployment_id": "dep-1", "hostname": "not a hostname!!",
         "ingress": "tunnel"},                      # invalid hostname
        {"deployment_id": "dep-1", "hostname": "db.internal",
         "ingress": "tunnel"},                      # internal-only name (W10)
        {"deployment_id": "dep-1", "hostname": "*.example.com",
         "ingress": "tunnel"},                      # wildcard (W10)
        {"deployment_id": "dep-1", "hostname": "api.example.com",
         "ingress": "tunnel"},                      # ok
        {"deployment_id": "dep-2", "hostname": "api.example.com",
         "ingress": "tunnel"},                      # duplicate
    ]
    routes, skipped = build_routes(_states(), entries)
    assert routes == [
        {"hostname": "api.example.com", "target_host": "127.0.0.1",
         "target_port": 8080},
    ]
    reasons = {s["hostname"]: s["reason"] for s in skipped}
    assert "not 'tunnel'" in reasons["direct.example.com"]
    assert "no local deployment" in reasons["gone.example.com"]
    assert "no host port" in reasons["noport.example.com"]
    assert "removed" in reasons["dead.example.com"]
    assert "invalid hostname" in reasons["not a hostname!!"]
    assert "invalid hostname" in reasons["db.internal"]
    assert "invalid hostname" in reasons["*.example.com"]
    assert "duplicate" in reasons["api.example.com"]


def test_build_routes_empty_inputs():
    routes, skipped = build_routes([], [])
    assert routes == [] and skipped == []


def test_hostname_validation():
    assert is_valid_hostname("api.example.com")
    assert is_valid_hostname("a-b.c0.example.co")
    assert not is_valid_hostname("not a hostname!!")
    assert not is_valid_hostname("x\n  - service: evil")
    assert not is_valid_hostname("")


def test_hostname_validation_w10_hardening():
    # wildcards: every hostname is explicit
    for bad in ("*.example.com", "api.*.example.com", "*"):
        assert not is_valid_hostname(bad), bad
    # localhost / special-use / internal-only names
    for bad in ("localhost", "api.localhost", "printer.local",
                "db.internal", "x.invalid", "y.example",
                "z.test", "q.onion"):
        assert not is_valid_hostname(bad), bad
    # IP literals
    for bad in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "8.8.8.8"):
        assert not is_valid_hostname(bad), bad
    # normal names still pass
    for good in ("api.example.com", "deep.sub.domain.co",
                 "abc123.cfargotunnel.com"):
        assert is_valid_hostname(good), good


# ---------------------------------------------------------------------------
# config.yml rendering
# ---------------------------------------------------------------------------

def test_render_cloudflared_config():
    routes = [
        {"hostname": "www.example.com", "target_host": "127.0.0.1",
         "target_port": 9090},
        {"hostname": "api.example.com", "target_host": "127.0.0.1",
         "target_port": 8080},
    ]
    text = render_cloudflared_config(routes)
    assert "ingress:" in text
    # sorted by hostname
    api_idx = text.index("hostname: api.example.com")
    www_idx = text.index("hostname: www.example.com")
    assert api_idx < www_idx
    assert "service: http://127.0.0.1:8080" in text
    assert "service: http://127.0.0.1:9090" in text
    assert "service: http_status:404" in text  # catch-all last
    assert text.index("http_status:404") > www_idx


def test_render_cloudflared_config_empty_routes():
    text = render_cloudflared_config([])
    assert "ingress:" in text
    assert "http_status:404" in text
    assert "hostname:" not in text


def test_rendered_config_states_non_authoritative_mirror():
    # W9: for token-based tunnels the remote tunnel configuration is the
    # routing authority; the local config.yml is a mirror only. The
    # rendered file must say so — nothing may imply it routes traffic.
    text = render_cloudflared_config([
        {"hostname": "api.example.com", "target_host": "127.0.0.1",
         "target_port": 8080},
    ])
    assert "NON-AUTHORITATIVE" in text
    assert "remote" in text.lower()


def test_render_refuses_bad_hostname():
    with pytest.raises(IngressError):
        render_cloudflared_config([
            {"hostname": "evil\n  - service: http://x", "target_host": "127.0.0.1",
             "target_port": 80}])


# ---------------------------------------------------------------------------
# provider base-class semantics
# ---------------------------------------------------------------------------

class _FakeProvider(IngressProvider):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.applied = 0
        self._enabled = True

    def setup(self, config):
        self._enabled = True
        return self

    def start(self):
        pass

    def shutdown(self):
        pass

    def status(self):
        return {"enabled": self._enabled, "routes": self.routes()}

    def _apply_routes(self):
        self.applied += 1


def test_provider_route_crud_and_diffing():
    p = _FakeProvider()
    assert p.sync_routes([
        {"hostname": "a.example.com", "target_host": "127.0.0.1",
         "target_port": 8001},
    ]) is True
    assert p.applied == 1
    # identical sync: no change, no apply
    assert p.sync_routes([
        {"hostname": "a.example.com", "target_host": "127.0.0.1",
         "target_port": 8001},
    ]) is False
    assert p.applied == 1
    p.add_route("b.example.com", "127.0.0.1", 8002)
    assert p.applied == 2
    p.remove_route("a.example.com")
    assert [r["hostname"] for r in p.routes()] == ["b.example.com"]
    p.remove_route("nonexistent.example.com")  # no-op, no apply
    assert p.applied == 3


def test_provider_rejects_non_loopback_target():
    p = _FakeProvider()
    with pytest.raises(IngressError):
        p.add_route("x.example.com", "10.0.0.5", 80)
    with pytest.raises(IngressError):
        p.sync_routes([{"hostname": "x.example.com",
                        "target_host": "evil.example.com",
                        "target_port": 80}])


# ---------------------------------------------------------------------------
# ingress-sync handler with a fake provider (no cloudflared, no network)
# ---------------------------------------------------------------------------

class _FakeAPI:
    def __init__(self, domains):
        self._domains = domains

    def list_host_domains(self):
        return {"domains": self._domains}


class _FakeStore:
    def __init__(self, states):
        self._states = states

    def list_all(self):
        return self._states


class _FakeCtx:
    def __init__(self, domains, states):
        self.api = _FakeAPI(domains)
        self.deployment_store = _FakeStore(states)
        self.ingress = _FakeProvider()
        self.logged = []

    def log(self, task_id, line):
        self.logged.append((task_id, line))
        return line


def test_handle_ingress_sync_end_to_end_with_fake_provider():
    ctx = _FakeCtx(
        domains=[
            {"deployment_id": "dep-1", "hostname": "api.example.com",
             "ingress": "tunnel"},
            {"deployment_id": "dep-2", "hostname": "www.example.com",
             "ingress": "direct"},  # left alone
        ],
        states=_states(),
    )
    result = handlers.handle_ingress_sync(
        ctx, {"id": "task-1", "type": "ingress-sync", "payload": {}})
    assert result["status"] == "ok"
    assert result["provider"] == "fake"
    assert result["changed"] is True
    assert result["routes"] == [
        {"hostname": "api.example.com", "target_host": "127.0.0.1",
         "target_port": 8080},
    ]
    assert ctx.ingress.applied == 1
    assert any("ingress sync" in line for _, line in ctx.logged)


def test_handle_ingress_sync_second_run_is_noop():
    ctx = _FakeCtx(
        domains=[{"deployment_id": "dep-1", "hostname": "api.example.com",
                  "ingress": "tunnel"}],
        states=_states(),
    )
    first = handlers.handle_ingress_sync(
        ctx, {"id": "t1", "type": "ingress-sync", "payload": {}})
    second = handlers.handle_ingress_sync(
        ctx, {"id": "t2", "type": "ingress-sync", "payload": {}})
    assert first["changed"] is True
    assert second["changed"] is False  # idempotent reconcile


def test_sync_ingress_disabled_without_provider():
    class _NoIngressCtx(_FakeCtx):
        def __init__(self):
            super().__init__([], [])
            self.ingress = None

    result = ingress_sync.sync_ingress(_NoIngressCtx())
    assert result["status"] == "disabled"
    assert result["routes"] == []


def test_maybe_sync_on_change_never_raises():
    class _BoomAPI(_FakeAPI):
        def list_host_domains(self):
            raise RuntimeError("control plane unreachable")

    class _BoomCtx(_FakeCtx):
        def __init__(self):
            super().__init__([], [])
            self.api = _BoomAPI([])

    outcome = ingress_sync.maybe_sync_on_change(_BoomCtx(), "dep-1")
    assert outcome["status"] == "error"
    assert "unreachable" in outcome["error"]


def test_maybe_sync_on_change_disabled_returns_none():
    class _NoIngressCtx(_FakeCtx):
        def __init__(self):
            super().__init__([], [])
            self.ingress = None

    assert ingress_sync.maybe_sync_on_change(_NoIngressCtx(), "dep-1") is None


def test_ingress_sync_in_policy_allowlist():
    assert policy.is_allowed("ingress-sync")
    assert policy.get_handler("ingress-sync") is handlers.handle_ingress_sync
    assert "ingress-sync" in policy.ALLOWED_TASK_TYPES


# ---------------------------------------------------------------------------
# cloudflared provider: offline-safe checks (no download, no spawn)
# ---------------------------------------------------------------------------

def test_cloudflared_checksum_table_shape():
    from ingress import cloudflared as cf
    assert cf.CLOUDFLARED_VERSION and "." in cf.CLOUDFLARED_VERSION
    for arch, digest in cf.CLOUDFLARED_SHA256.items():
        assert arch in ("amd64", "arm64")
        assert len(digest) == 64
        int(digest, 16)  # valid hex
    # pinned digests are the official 2026.10.0 release digests
    assert cf.CLOUDFLARED_SHA256["amd64"] == \
        "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db"


def test_cloudflared_unsupported_arch_refuses(tmp_path, monkeypatch):
    from ingress import cloudflared as cf
    monkeypatch.setattr(cf.platform, "machine", lambda: "mips64")
    monkeypatch.setattr(cf.shutil, "which", lambda _name: None)
    with pytest.raises(IngressError) as excinfo:
        cf.ensure_cloudflared_binary(str(tmp_path))
    assert "mips64" in str(excinfo.value)


def test_cloudflared_download_url_pinned():
    from ingress import cloudflared as cf
    url = cf._download_url("amd64")
    assert url == ("https://github.com/cloudflare/cloudflared/releases/download/"
                   "2026.10.0/cloudflared-linux-amd64")
    assert "latest" not in url  # never auto-trust latest


def test_cloudflared_status_without_setup_has_no_secrets():
    from ingress.cloudflared import CloudflaredTunnelProvider
    p = CloudflaredTunnelProvider()
    st = p.status()
    assert st["enabled"] is False
    assert st["running"] is False
    assert "token" not in str(st).lower().replace("tunnel_token", "")


def test_cloudflared_setup_requires_token(tmp_path):
    from ingress.cloudflared import CloudflaredTunnelProvider
    p = CloudflaredTunnelProvider()
    cfg = IngressConfig(enabled=True, provider="cloudflare-tunnel",
                        tunnel_token="", work_dir=str(tmp_path))
    with pytest.raises(ConfigError) as excinfo:
        p.setup(cfg)
    assert "WORKER_TUNNEL_TOKEN" in str(excinfo.value)


def test_build_ingress_config_from_worker_config():
    cfg = build_ingress_config(_base_config(
        ingress_enabled=True, ingress_provider="cloudflare-tunnel",
        tunnel_token="tok123"))
    assert cfg.enabled is True
    assert cfg.provider == "cloudflare-tunnel"
    assert cfg.tunnel_token == "tok123"


def test_apply_routes_rewrites_mirror_without_restarting_tunnel(tmp_path):
    # W9: the local config.yml is a NON-AUTHORITATIVE mirror — route
    # changes must not terminate the tunnel process (routing comes from
    # the remote tunnel configuration; a restart would only drop
    # in-flight connections for no benefit).
    import subprocess
    from ingress.cloudflared import CloudflaredTunnelProvider
    p = CloudflaredTunnelProvider()
    p._config_path = str(tmp_path / "config.yml")
    p._routes = {"api.example.com": {
        "hostname": "api.example.com",
        "target_host": "127.0.0.1", "target_port": 8080}}
    proc = subprocess.Popen(["sleep", "60"])
    p._proc = proc
    try:
        p._apply_routes()
        assert proc.poll() is None, "tunnel process was restarted on a route change"
        text = (tmp_path / "config.yml").read_text()
        assert "NON-AUTHORITATIVE" in text
        assert "hostname: api.example.com" in text
    finally:
        proc.terminate()
        proc.wait(timeout=10)
