"""Worker self-update (advertised via the heartbeat response).

When the control plane wants the worker to update, it includes
``worker_update: {version, url, sha256}`` in the heartbeat response.
The flow is:

  1. download the release tarball from ``url`` (outbound HTTPS) to
     <work_dir>/updates/<version>/
  2. verify SHA-256; mismatch => abort, keep running current version
  3. extract to <work_dir>/releases/<version>/
  4. health self-check: byte-compile the new tree and import the core
     modules from it in a subprocess (argv list, no shell)
  5. atomically swing <work_dir>/current -> releases/<version> (symlink)
  6. restart via ``systemctl restart agent-host-worker`` (argv list)
  7. if the self-check (or a post-restart check) fails, swing the symlink
     back to the previous release and raise

Safe no-op when no update is advertised or the advertised version is not
newer than the running one.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

SERVICE_NAME = "agent-host-worker"
HEALTH_IMPORTS = (
    "import agent.config, agent.api, agent.policy, "
    "executor.dispatcher, executor.handlers, "
    "deployments.manifest, deployments.pipeline, deployments.state, "
    "docker.client, health.collector, health.checker, logs.store"
)


class UpdateError(Exception):
    """Self-update failed; the previous version is still active."""


def _version_key(version: str) -> tuple:
    """Rough semver-ish ordering: '1.2.3' -> (1, 2, 3)."""
    parts = []
    for piece in str(version).strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    try:
        return _version_key(candidate) > _version_key(current)
    except Exception:
        return str(candidate) != str(current)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _health_check_release(release_dir: Path, log=None) -> None:
    """Byte-compile + import core modules from the new tree in a subprocess."""
    script = (
        "import compileall, sys; "
        f"ok = compileall.compile_dir({str(release_dir)!r}, quiet=1, force=True); "
        "sys.exit(0 if ok else 1)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise UpdateError(
            f"new release failed byte-compile check: {proc.stderr[-2000:]}")

    import_script = (
        f"import sys; sys.path.insert(0, {str(release_dir)!r}); {HEALTH_IMPORTS}"
    )
    proc = subprocess.run(
        [sys.executable, "-c", import_script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise UpdateError(
            f"new release failed import check: {proc.stderr[-2000:]}")
    if log:
        log("update self-check passed (compile + import)")


def apply_update(update_info: dict, work_dir: str, current_version: str,
                 download_fn=None, log=None) -> str:
    """Apply one advertised update. Returns 'updated'; raises UpdateError.

    download_fn(url, dest_path) may be injected (tests); default streams
    the URL with requests (outbound HTTPS).
    """
    version = str(update_info.get("version", "")).strip()
    url = str(update_info.get("url", "")).strip()
    expected_sha = str(update_info.get("sha256", "")).strip().lower()
    if not version or not url or not expected_sha:
        raise UpdateError("worker_update missing version/url/sha256")
    if not is_newer(version, current_version):
        if log:
            log(f"update {version} not newer than {current_version}; skipping")
        return "noop"

    work = Path(work_dir)
    updates_dir = work / "updates" / version
    releases_dir = work / "releases"
    release_dir = releases_dir / version
    updates_dir.mkdir(parents=True, exist_ok=True)
    releases_dir.mkdir(parents=True, exist_ok=True)
    tarball = updates_dir / "worker.tar.gz"

    if log:
        log(f"downloading worker update {version} from {url}")
    if download_fn is None:
        import requests
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(tarball, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
    else:
        download_fn(url, str(tarball))

    actual_sha = _sha256_file(tarball)
    if not hmac.compare_digest(actual_sha, expected_sha):
        tarball.unlink(missing_ok=True)
        raise UpdateError(
            f"update {version} checksum mismatch (got {actual_sha[:16]}...); "
            "aborted, current version untouched")

    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as tf:
        # traversal protection
        base = release_dir.resolve()
        for member in tf.getmembers():
            target = (base / member.name).resolve()
            if target != base and base not in target.parents:
                raise UpdateError(f"update tarball escapes: {member.name!r}")
        tf.extractall(release_dir)

    current_link = work / "current"
    previous_target = None
    if current_link.is_symlink():
        previous_target = os.readlink(current_link)

    try:
        _health_check_release(release_dir, log=log)
    except UpdateError:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise

    # atomically swing the symlink, then restart the service
    tmp_link = work / "current.tmp"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(release_dir)
    os.replace(tmp_link, current_link)
    if log:
        log(f"update {version} installed; restarting {SERVICE_NAME}")
    proc = subprocess.run(
        ["systemctl", "restart", SERVICE_NAME],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
    )
    if proc.returncode != 0:
        # rollback the symlink; the old code is still on disk
        if previous_target:
            rb = work / "current.rb"
            if rb.is_symlink() or rb.exists():
                rb.unlink()
            rb.symlink_to(previous_target)
            os.replace(rb, current_link)
        raise UpdateError(
            f"systemctl restart failed: {proc.stderr[-2000:]}; "
            "symlink rolled back to previous release")
    return "updated"


def check_and_apply(heartbeat_response: dict, ctx, log=None) -> str:
    """Entry point from the heartbeat loop. Returns 'noop' | 'updated'.

    Raises UpdateError on failure (caller logs it; worker keeps running).
    """
    update_info = (heartbeat_response or {}).get("worker_update")
    if not update_info:
        return "noop"
    return apply_update(update_info, ctx.config.work_dir,
                        ctx.config.worker_version, log=log)
