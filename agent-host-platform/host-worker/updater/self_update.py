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
import re
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
# The running worker writes this on every boot (see agent.main); the
# updater reads it after a self-update restart to confirm the NEW version
# actually came up healthy before declaring the update done.
STATUS_FILENAME = "worker.status.json"
# Seconds to wait for the new version to report healthy after restart
# (env-overridable; honoring the operator's rollout patience).
HEALTH_TIMEOUT_S = int(os.environ.get("WORKER_UPDATE_HEALTH_TIMEOUT_S", "120") or 120)
HEALTH_POLL_INTERVAL_S = 5


def write_status_file(work_dir: str, version: str) -> Path:
    """Record a boot marker for the post-update health gate to read."""
    import json
    path = Path(work_dir) / STATUS_FILENAME
    path.write_text(json.dumps({
        "version": version,
        "boot_ts": time.time(),
        "pid": os.getpid(),
    }), encoding="utf-8")
    return path


def read_status_file(work_dir: str) -> dict | None:
    """Return the boot marker dict, or None if absent/unparseable."""
    import json
    try:
        data = json.loads((Path(work_dir) / STATUS_FILENAME).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _service_active() -> bool:
    """True when systemd reports the worker unit active."""
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def wait_for_healthy(work_dir: str, version: str, pre_ts: float,
                     timeout_s: int = HEALTH_TIMEOUT_S,
                     is_active_fn=None, sleep_fn=time.sleep,
                     log=None) -> bool:
    """Poll until the restarted worker proves it is the new version.

    Healthy = systemd unit active AND the boot marker shows ``version``
    with a boot timestamp at/after ``pre_ts`` (i.e. written by the
    post-restart process, not a stale marker from the old run).
    Returns True on success, False on timeout. ``is_active_fn`` and
    ``sleep_fn`` are injectable for tests.
    """
    is_active = is_active_fn or _service_active
    deadline = time.time() + max(1, timeout_s)
    while time.time() < deadline:
        marker = read_status_file(work_dir)
        if (marker
                and marker.get("version") == version
                and isinstance(marker.get("boot_ts"), (int, float))
                and marker["boot_ts"] >= pre_ts - 1
                and is_active()):
            if log:
                log(f"update {version} healthy after restart "
                    f"(pid {marker.get('pid')})")
            return True
        sleep_fn(HEALTH_POLL_INTERVAL_S)
    return False


def _rollback_symlink(work: Path, previous_target: str | None) -> None:
    """Swing <work>/current back to the previous release."""
    if not previous_target:
        return
    rb = work / "current.rb"
    if rb.is_symlink() or rb.exists():
        rb.unlink()
    rb.symlink_to(previous_target)
    os.replace(rb, work / "current")


class UpdateError(Exception):
    """Self-update failed; the previous version is still active."""


# The advertised version becomes path components (<work>/updates/<version>,
# <work>/releases/<version>). It arrives in the heartbeat response, so
# validate it as a single safe path component: "../.." or absolute paths
# must never steer the download/extraction outside the work dir.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise UpdateError(
            f"refusing worker update: version {version!r} is not a safe "
            "path component"
        )
    return version


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
    _validate_version(version)
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
    try:
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
    except Exception as exc:
        tarball.unlink(missing_ok=True)
        raise UpdateError(
            f"update {version} download failed: {exc}; "
            "current version untouched") from exc

    actual_sha = _sha256_file(tarball)
    if not hmac.compare_digest(actual_sha, expected_sha):
        tarball.unlink(missing_ok=True)
        raise UpdateError(
            f"update {version} checksum mismatch (got {actual_sha[:16]}...); "
            "aborted, current version untouched")

    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            # tarfile.data_filter (3.12+) blocks absolute paths, ".." escapes,
            # and symlink/hardlink members resolving outside release_dir.
            tf.extractall(release_dir, filter="data")
    except Exception as exc:
        # Corrupt/truncated archive: never leave a half-extracted release
        # behind, and never proceed to the health check with it.
        shutil.rmtree(release_dir, ignore_errors=True)
        raise UpdateError(
            f"update {version} archive is corrupt ({exc}); "
            "aborted, current version untouched") from exc

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
    pre_ts = time.time()
    proc = subprocess.run(
        ["systemctl", "restart", SERVICE_NAME],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
    )
    if proc.returncode != 0:
        # rollback the symlink; the old code is still on disk
        _rollback_symlink(work, previous_target)
        raise UpdateError(
            f"systemctl restart failed: {proc.stderr[-2000:]}; "
            "symlink rolled back to previous release")
    # Post-restart health gate: the NEW worker must write a fresh boot
    # marker and stay active within the timeout, or we roll back. This is
    # what catches a release that installs cleanly but crashes on boot.
    if not wait_for_healthy(str(work), version, pre_ts,
                            timeout_s=HEALTH_TIMEOUT_S, log=log):
        _rollback_symlink(work, previous_target)
        rb_proc = subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=120,
        )
        rb_note = ("and restarted it" if rb_proc.returncode == 0
                   else f"but restarting it ALSO failed: "
                        f"{rb_proc.stderr[-2000:]}")
        raise UpdateError(
            f"update {version} did not come up healthy within "
            f"{HEALTH_TIMEOUT_S}s of restart; rolled back to previous "
            f"release {rb_note}")
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
