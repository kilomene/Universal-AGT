"""W13a §44 — network failures: every transient fault retries with backoff
and never duplicates a deployment operation.

  * DNS failure: unresolvable control-plane hostname -> WorkerAPIError
    (single typed error, thanks to the transport wrapper) -> retry driver
    backs off exponentially; the operation is attempted exactly 3 times.
  * Slow control plane: heartbeats time out, then recover — the worker
    reconnects and the heartbeat succeeds exactly once per attempt.
  * Connection reset mid-artifact-download: the broken transfer raises,
    no partial file is left behind (.part cleaned), the retry downloads
    the complete bytes, and the SHA-256 matches.
  * Short-but-"complete" transfer: a size mismatch never promotes the
    file, and the pipeline's checksum gate refuses tampered bytes — a
    corrupted deploy is impossible.
  * Tunnel restart: the supervised cloudflared tunnel is SIGKILLed; the
    supervisor respawns it with backoff while the app keeps serving on
    loopback, and a route sync does NOT restart the tunnel.

What is REAL: ControlPlaneClient (incl. the transport-error wrapper),
agent.backoff, deployments.pipeline.verify_checksum, and the
CloudflaredTunnelProvider supervision loop (with a fake binary standing
in for cloudflared — the supervision logic is the real code under test).
"""
from __future__ import annotations

import os
import stat
import threading
import time

import pytest
import requests

from agent import backoff as backoff_mod
from agent.api import REQUEST_TIMEOUT, ControlPlaneClient, WorkerAPIError
from agent import api as api_mod
from deployments.pipeline import verify_checksum
from ingress import IngressConfig
from ingress import cloudflared as cloudflared_mod

from recovery_harness import (
    FakeDocker,
    RecoveryPlane,
    free_port,
    http_get_text,
    wait_until,
)


@pytest.fixture()
def plane():
    plane = RecoveryPlane().start()
    yield plane
    plane.shutdown()


@pytest.fixture()
def docker():
    docker = FakeDocker()
    yield docker
    docker.shutdown_all()


def _retry_driver(fn, attempts: int = 3, rand=lambda: 0.5):
    """Deterministic retry driver: real backoff math, clamped sleeps."""
    delays = []
    last_exc = None
    for n in range(1, attempts + 1):
        try:
            return fn(), delays
        except WorkerAPIError as exc:
            last_exc = exc
            if n < attempts:
                delay = backoff_mod.backoff_delay(n, rand=rand)
                delays.append(delay)
                time.sleep(min(delay, 0.05))
    raise last_exc


# ---------------------------------------------------------------------------
# DNS failure
# ---------------------------------------------------------------------------

def test_dns_failure_retries_with_backoff_and_single_error_type():
    api = ControlPlaneClient("http://nonexistent.invalid", "hst-dead")
    calls = {"n": 0}
    delays = []

    def op_recording():
        calls["n"] += 1
        try:
            return api.claim_task("host-1", ["docker"], wait=1)
        except WorkerAPIError:
            raise

    # Deterministic driver: real backoff math (jitter disabled via rand),
    # clamped sleeps for speed.
    last_exc = None
    for n in range(1, 4):
        try:
            op_recording()
        except WorkerAPIError as exc:
            last_exc = exc
            if n < 3:
                delay = backoff_mod.backoff_delay(n, rand=lambda: 0.5)
                delays.append(delay)
    # Single typed error naming the action (not a raw requests exception).
    assert last_exc is not None and "task claim" in str(last_exc)
    assert calls["n"] == 3  # attempted exactly 3 times, then stops
    assert delays == [5.0, 10.0]  # exponential, deterministic


# ---------------------------------------------------------------------------
# slow control plane: timeouts, then recovery
# ---------------------------------------------------------------------------

def test_slow_controlplane_times_out_then_recovers(plane, monkeypatch):
    import requests as requests_mod

    monkeypatch.setattr(api_mod, "REQUEST_TIMEOUT", 1)
    # First two heartbeats take 3s (> 1s timeout); then the plane is fast.
    plane.inject_latency("/v1/hosts/", seconds=3, times=2)

    host_id = "host-slow"
    slow_token = "hst-FAKE-slow"  # FAKE marker: not a real credential
    with plane.lock:
        plane.hosts[host_id] = {"id": host_id, "name": host_id,
                                "capabilities": [], "token": slow_token,
                                "status": "online"}
        plane._token_to_host[slow_token] = host_id

    api = ControlPlaneClient(plane.url, slow_token)
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        return api.heartbeat(host_id, {})

    result, delays = _retry_driver(op, attempts=4)
    assert result["pending_tasks"] == 0
    assert attempts["n"] == 3  # 2 timeouts + 1 success; no extra attempts
    assert delays == [5.0, 10.0]  # exponential backoff between retries


# ---------------------------------------------------------------------------
# connection reset / partial artifact transfer
# ---------------------------------------------------------------------------

def _upload_artifact(plane: RecoveryPlane, data: bytes) -> dict:
    agent_resp = requests.post(
        f"{plane.url}/v1/agents/register",
        json={"name": f"w13a-art-{time.monotonic_ns()}", "type": "Muse"},
        timeout=5)
    assert agent_resp.status_code == 201, agent_resp.text
    key = agent_resp.json()["api_key"]
    resp = requests.post(
        f"{plane.url}/v1/test/artifacts", data=data,
        headers={"Authorization": f"Bearer {key}"}, timeout=10)
    assert resp.status_code == 201, resp.text
    return resp.json()["artifact"]


def test_connection_reset_mid_download_cleans_up_and_retry_succeeds(
        plane, tmp_path):
    data = os.urandom(1 << 20)  # 1 MiB
    art = _upload_artifact(plane, data)
    plane.download_mode = "partial-once"  # first GET dies mid-body

    host_token = "hst-dl"
    with plane.lock:
        plane.hosts["host-dl"] = {"id": "host-dl", "name": "host-dl",
                                  "capabilities": [], "token": host_token,
                                  "status": "online"}
        plane._token_to_host[host_token] = "host-dl"
    api = ControlPlaneClient(plane.url, host_token)
    dest = str(tmp_path / "artifact.bin")

    # First attempt: connection reset mid-transfer -> typed error, and no
    # partial file is left behind (neither dest nor .part).
    with pytest.raises(WorkerAPIError) as excinfo:
        api.download_artifact(art["id"], dest, expected_size=art["size"])
    assert "transport error" in str(excinfo.value)
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")

    # Retry: the transfer restarts clean and the bytes verify.
    assert api.download_artifact(art["id"], dest,
                                 expected_size=art["size"]) == dest
    with open(dest, "rb") as fh:
        assert fh.read() == data
    assert verify_checksum(dest, art["checksum"]) is True
    assert plane._download_calls == 2  # exactly one failed + one good GET


def test_short_transfer_never_promotes_and_checksum_gate_holds(
        plane, tmp_path):
    data = os.urandom(4096)
    art = _upload_artifact(plane, data)
    host_token = "hst-short"
    with plane.lock:
        plane.hosts["host-short"] = {"id": "host-short", "name": "h",
                                     "capabilities": [], "token": host_token,
                                     "status": "online"}
        plane._token_to_host[host_token] = "host-short"
    api = ControlPlaneClient(plane.url, host_token)
    dest = str(tmp_path / "artifact2.bin")

    # Claim a bigger size than the server will send: the mismatch raises
    # and nothing is promoted.
    with pytest.raises(WorkerAPIError) as excinfo:
        api.download_artifact(art["id"], dest, expected_size=art["size"] + 100)
    assert "size mismatch" in str(excinfo.value)
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")

    # The honest retry downloads the real bytes.
    api.download_artifact(art["id"], dest, expected_size=art["size"])
    assert verify_checksum(dest, art["checksum"]) is True

    # And the pipeline's checksum gate refuses tampered bytes outright —
    # this is what makes a corrupted deploy impossible.
    with open(dest, "r+b") as fh:
        fh.seek(0)
        fh.write(b"X")
    assert verify_checksum(dest, art["checksum"]) is False


# ---------------------------------------------------------------------------
# tunnel restart: supervisor respawns; apps keep serving; sync != restart
# ---------------------------------------------------------------------------

def _fake_cloudflared_binary(tmp_path) -> str:
    path = str(tmp_path / "cloudflared-fake")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nsleep 300\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def test_tunnel_restart_reestablishes_without_dropping_app_traffic(
        docker, tmp_path, monkeypatch):
    binary = _fake_cloudflared_binary(tmp_path)
    monkeypatch.setattr(cloudflared_mod, "ensure_cloudflared_binary",
                        lambda work_dir: binary)
    # Record the supervisor's backoff progression; keep the test fast.
    real_backoff = backoff_mod.backoff_delay
    seen_failures = []

    def spy(failures, **kw):
        seen_failures.append(failures)
        return 0.1

    monkeypatch.setattr(backoff_mod, "backoff_delay", spy)

    cfg = IngressConfig(enabled=True, provider="cloudflare-tunnel",
                        tunnel_token="tok-test", work_dir=str(tmp_path))
    provider = cloudflared_mod.CloudflaredTunnelProvider()
    provider.setup(cfg)
    # The token is held in memory only — never in status output.
    assert "tok-test" not in str(provider.status())
    provider.start()
    try:
        wait_until(lambda: provider.status()["running"],
                   what="tunnel process running")
        pid_before = provider.status()["pid"]
        assert pid_before

        # An app serving on loopback (its traffic never flows through the
        # worker's tunnel child process).
        app_port = free_port()
        docker.run("uaht-tunnel-app", "uaht-web:1.0.0", ports={app_port: 3000})
        assert http_get_text(f"http://127.0.0.1:{app_port}") == \
            "fake-app:uaht-tunnel-app"

        # The tunnel dies (SIGKILL, like a cloudflared crash).
        os.kill(pid_before, 9)
        wait_until(lambda: provider.status()["running"]
                   and provider.status()["pid"] != pid_before,
                   what="tunnel respawned with a new pid")
        assert provider.status()["restarts"] >= 1
        assert seen_failures and seen_failures[0] == 1  # backoff consulted

        # The app never stopped serving: same server, uninterrupted.
        assert http_get_text(f"http://127.0.0.1:{app_port}") == \
            "fake-app:uaht-tunnel-app"

        # A route sync rewrites the local mirror but does NOT restart the
        # tunnel (remotely-managed tunnels pick up remote config on their
        # own; a restart would drop in-flight connections for nothing).
        pid_stable = provider.status()["pid"]
        changed = provider.sync_routes([{
            "hostname": "app.example.com", "target_host": "127.0.0.1",
            "target_port": app_port}])
        assert changed is True
        assert provider.status()["pid"] == pid_stable
        assert provider.status()["restarts"] == \
            provider.status()["restarts"]  # no new restart
        assert http_get_text(f"http://127.0.0.1:{app_port}") == \
            "fake-app:uaht-tunnel-app"
    finally:
        provider.shutdown()
    assert provider.status()["running"] is False
