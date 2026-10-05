"""Cloudflare Tunnel ingress provider (``cloudflare-tunnel``).

Runs ``cloudflared tunnel --token <TOKEN> run`` as a supervised subprocess.
The tunnel dials OUT to Cloudflare's edge over QUIC; Cloudflare then routes
inbound public HTTPS for the tunnel's hostnames back down that outbound
connection to the host's loopback container ports. At no point does the
host listen for — or accept — inbound connections initiated from outside.

Binary provisioning: ``cloudflared`` is resolved from PATH, then from
``<work_dir>/bin/cloudflared``; when absent it is downloaded from the
official GitHub release for the pinned version below and SHA-256-verified
against the pinned checksums (published by Cloudflare in the release
metadata; the amd64 digest was additionally verified by direct download).
The worker never auto-trusts "latest": an unknown version or a checksum
mismatch refuses the binary. A failed download logs clearly and leaves
ingress disabled — the worker keeps running its deployment duties.

The tunnel token comes from worker config / the ``WORKER_TUNNEL_TOKEN``
environment variable (the installer accepts ``UAHT_TUNNEL_TOKEN`` and maps
it into ``worker.env``). It is never logged, never appears in status output,
and never lives in the repo.

Route application: the provider rewrites ``config.yml`` on route changes —
and that is ALL it does. Be clear about what that file IS: for token-based
(remotely-managed) tunnels — the only kind this system runs — cloudflared
IGNORES local config.yml ingress rules; the authoritative route table is
the tunnel's REMOTE configuration, written by the control plane via the
Cloudflare API (see
control-plane/api/src/lib/cloudflare-tunnel.ts). The local config.yml is
a NON-AUTHORITATIVE mirror — an operator checklist and debugging
artifact. Rewriting it never routes traffic, so the tunnel is NOT
restarted on route changes (remotely-managed cloudflared picks up a new
remote config version within seconds on its own; restarting would only
drop in-flight connections for no benefit). A rotated tunnel token takes
effect on worker restart — the token is read once at provider setup and
is never re-read by a route sync.

All subprocess calls use argv lists with shell=False.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request

from agent import backoff as backoff_mod
from agent.config import ConfigError
from ingress import (
    IngressConfig,
    IngressError,
    IngressProvider,
    render_cloudflared_config,
)

LOG = logging.getLogger("agent-host-worker.ingress")

#: Pinned cloudflared release — never "latest". Checksums are Cloudflare's
#: own per-asset digests from the official release metadata (verified
#: 2026-10-05; the amd64 binary was additionally downloaded and re-hashed
#: locally: d33ff2d1...9db — match).
CLOUDFLARED_VERSION = "2026.10.0"
CLOUDFLARED_SHA256: dict[str, str] = {
    # asset name suffix -> sha256
    "amd64": "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db",
    "arm64": "e6422b9d4f72d3194bc5a38676f13667c06666523217b842a877d72a80b5ac08",
}
CLOUDFLARED_RELEASE_BASE = (
    "https://github.com/cloudflare/cloudflared/releases/download"
)

_ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

_DOWNLOAD_TIMEOUT_S = 120
_RESTART_GRACE_S = 10


def _host_arch() -> str:
    machine = platform.machine().lower()
    arch = _ARCH_MAP.get(machine)
    if not arch:
        raise IngressError(
            f"cloudflared download not supported on arch {machine!r}: no "
            f"pinned checksum; install cloudflared manually and put it on "
            f"PATH, or disable ingress"
        )
    return arch


def _download_url(arch: str) -> str:
    return (f"{CLOUDFLARED_RELEASE_BASE}/{CLOUDFLARED_VERSION}/"
            f"cloudflared-linux-{arch}")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_cloudflared_binary(work_dir: str) -> str:
    """Return a verified cloudflared binary path.

    Order: PATH, then <work_dir>/bin/cloudflared, then download the pinned
    release with SHA-256 verification. Raises IngressError with a clear
    message when no verified binary can be produced (caller stays disabled).
    """
    found = shutil.which("cloudflared")
    if found:
        LOG.info("using cloudflared from PATH: %s", found)
        return found
    dest = os.path.join(work_dir, "bin", "cloudflared")
    if os.path.isfile(dest):
        LOG.info("using cloudflared at %s", dest)
        return dest
    return _download_cloudflared(dest)


def _download_cloudflared(dest: str) -> str:
    arch = _host_arch()
    expected = CLOUDFLARED_SHA256[arch]
    url = _download_url(arch)
    LOG.info("downloading cloudflared %s (%s) for checksum verification",
             CLOUDFLARED_VERSION, arch)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="cloudflared-dl-",
                                        dir=os.path.dirname(dest)
                                        if os.path.isdir(os.path.dirname(dest))
                                        else None)
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            with urllib.request.urlopen(url,
                                        timeout=_DOWNLOAD_TIMEOUT_S) as resp:
                if resp.status != 200:
                    raise IngressError(
                        f"cloudflared download failed: HTTP {resp.status} "
                        f"from {url}")
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
        actual = _sha256_file(tmp_path)
        if actual != expected:
            raise IngressError(
                "cloudflared checksum MISMATCH: refusing the binary "
                f"(expected {expected}, got {actual}); ingress stays disabled"
            )
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.chmod(tmp_path, os.stat(tmp_path).st_mode
                 | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp_path, dest)
        LOG.info("cloudflared %s verified (sha256 %s..) and installed at %s",
                 CLOUDFLARED_VERSION, expected[:16], dest)
        return dest
    except IngressError:
        raise
    except Exception as exc:
        raise IngressError(
            f"cloudflared download failed ({exc}); ingress stays disabled. "
            f"Install cloudflared manually on PATH or check network egress."
        ) from exc
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


class CloudflaredTunnelProvider(IngressProvider):
    """Supervised ``cloudflared tunnel --token <TOKEN> run`` process."""

    name = "cloudflare-tunnel"

    def __init__(self) -> None:
        super().__init__()
        self._binary = ""
        self._config_dir = ""
        self._config_path = ""
        self._log_path = ""
        self._token = ""
        self._proc: subprocess.Popen | None = None
        self._supervisor: threading.Thread | None = None
        self._stop = threading.Event()
        self._restarts = 0
        self._last_error = ""
        self._lock = threading.Lock()

    # -- setup / lifecycle -------------------------------------------
    def setup(self, config: IngressConfig) -> "CloudflaredTunnelProvider":
        config.validate()
        if config.provider != self.name:
            raise ConfigError(
                f"provider mismatch: configured {config.provider!r}, "
                f"this provider is {self.name!r}")
        self._binary = ensure_cloudflared_binary(config.work_dir)
        self._config_dir = os.path.join(config.work_dir, "ingress",
                                        "cloudflared")
        os.makedirs(self._config_dir, exist_ok=True)
        self._config_path = os.path.join(self._config_dir, "config.yml")
        self._log_path = os.path.join(config.work_dir, "logs",
                                      "cloudflared.log")
        os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
        # The token is held in memory only; it never goes to disk or logs.
        self._token = config.tunnel_token
        self._enabled = True
        # Write the initial (possibly empty) route table so the file always
        # exists and is never hand-edited state.
        self._write_config()
        LOG.info("cloudflare-tunnel provider set up (binary=%s config=%s)",
                 self._binary, self._config_path)
        return self

    def start(self) -> None:
        if not self._enabled:
            raise IngressError("provider not set up; call setup() first")
        if self._supervisor and self._supervisor.is_alive():
            return
        self._stop.clear()
        self._supervisor = threading.Thread(target=self._supervise,
                                            name="cloudflared-supervisor",
                                            daemon=True)
        self._supervisor.start()
        LOG.info("cloudflared supervisor started")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=_RESTART_GRACE_S)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._supervisor:
            self._supervisor.join(timeout=_RESTART_GRACE_S + 5)

    def status(self) -> dict:
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            pid = proc.pid if running else None
        return {
            "enabled": self._enabled,
            "provider": self.name,
            "binary": self._binary,
            "version": CLOUDFLARED_VERSION,
            "running": running,
            "pid": pid,
            "restarts": self._restarts,
            "routes": self.routes(),
            "config_path": self._config_path,
            "log_path": self._log_path,
            "last_error": self._last_error,
            # the tunnel token is deliberately absent
        }

    # -- supervision ---------------------------------------------------
    def _spawn(self) -> subprocess.Popen:
        log_fh = open(self._log_path, "ab")
        argv = [self._binary, "tunnel", "--token", self._token, "run"]
        LOG.info("starting cloudflared tunnel (token redacted)")
        # start_new_session: the tunnel dies with its own process group on
        # shutdown instead of taking the worker's group with it.
        return subprocess.Popen(argv, stdout=log_fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True, shell=False)

    def _supervise(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                proc = self._spawn()
            except Exception as exc:
                self._last_error = f"spawn failed: {exc}"
                LOG.error("cloudflared spawn failed: %s", exc)
                failures += 1
                if self._stop.wait(backoff_mod.backoff_delay(failures)):
                    return
                continue
            with self._lock:
                self._proc = proc
            failures = 0  # a successful spawn resets the backoff
            LOG.info("cloudflared running (pid=%s)", proc.pid)
            rc = proc.wait()
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            if self._stop.is_set():
                LOG.info("cloudflared stopped (rc=%s)", rc)
                return
            # Unexpected exit: crash-loop backoff, then respawn.
            self._restarts += 1
            failures += 1
            self._last_error = (f"cloudflared exited rc={rc}; "
                                f"restart #{self._restarts}")
            delay = backoff_mod.backoff_delay(failures)
            LOG.warning("cloudflared exited rc=%s; restarting in %.0fs "
                        "(restart #%d)", rc, delay, self._restarts)
            if self._stop.wait(delay):
                return

    # -- routes ----------------------------------------------------------
    def _write_config(self) -> None:
        text = render_cloudflared_config(self._routes.values())
        tmp = self._config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self._config_path)

    def _apply_routes(self) -> None:
        """Rewrite the local config.yml mirror (diagnostic only).

        The mirror is NON-AUTHORITATIVE for token-based tunnels: the
        remote tunnel configuration is the routing authority and the
        control plane writes it via the Cloudflare API. Rewriting the
        mirror never routes traffic, so the tunnel is deliberately NOT
        restarted here — a restart would drop in-flight connections for
        no routing benefit. (A rotated tunnel token takes effect on
        worker restart, not on a route sync.)"""
        self._write_config()
        LOG.info("cloudflared route mirror rewritten (%d routes); "
                 "tunnel NOT restarted (remote config is authoritative)",
                 len(self._routes))
