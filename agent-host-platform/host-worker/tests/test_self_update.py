"""Spec §22 — worker self-update: atomic and recoverable.

Covers the mandated cases with injected fakes (no real systemctl, no
network):

  * valid update -> 'updated', symlink swung to the new release
  * checksum failure -> UpdateError, tarball deleted, symlink untouched
  * corrupt archive -> UpdateError, no half-extracted release left behind
  * download failure -> UpdateError
  * import/compile failure -> UpdateError, release dir removed
  * restart failure -> UpdateError, symlink rolled back to previous release
  * post-restart health failure -> UpdateError, symlink rolled back, the
    old release is restarted again
  * not-newer version -> 'noop', no download attempted

wait_for_healthy is unit-tested separately with an injected clock via
is_active_fn.
"""
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from updater import self_update


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_tarball(files: dict) -> bytes:
    """Build a .tar.gz from {arcname: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, data in files.items():
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _download_of(blob: bytes):
    def _download(url, dest_path):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(blob)
    return _download


class _SubprocessFake:
    """Stands in for subprocess.run; programmable per argv prefix."""

    def __init__(self):
        self.calls = []
        self.restart_results = []  # queue of (returncode, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "restart"]:
            if self.restart_results:
                rc, err = self.restart_results.pop(0)
            else:
                rc, err = 0, ""
            return _Completed(rc, err)
        if argv[0].endswith("python") or "python" in argv[0]:
            return _Completed(0, "")
        raise AssertionError(f"unexpected subprocess call: {argv}")


class _Completed:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    (w / "releases" / "1.0.0").mkdir(parents=True)
    (w / "current").symlink_to(w / "releases" / "1.0.0")
    return w


def _info(version, blob):
    return {"version": version, "url": "https://example.com/worker.tar.gz",
            "sha256": hashlib.sha256(blob).hexdigest()}


def _apply(monkeypatch, update_info, work_dir, current_version,
           download_fn, restart_results=(0, ""), healthy=True):
    """apply_update with systemctl + health gate fully faked."""
    fake = _SubprocessFake()
    fake.restart_results = [restart_results]
    monkeypatch.setattr(self_update.subprocess, "run", fake)
    monkeypatch.setattr(self_update, "_health_check_release",
                        lambda release_dir, log=None: None)
    monkeypatch.setattr(self_update, "wait_for_healthy",
                        lambda *a, **k: healthy)
    result = self_update.apply_update(
        update_info, str(work_dir), current_version,
        download_fn=download_fn, log=lambda line: None)
    return result, fake


# ---------------------------------------------------------------------------
# The §22 cases
# ---------------------------------------------------------------------------
def test_valid_update_swings_symlink_atomically(tmp_path, monkeypatch, work):
    blob = _make_tarball({"pkg/__init__.py": b"x = 1\n"})
    result, fake = _apply(monkeypatch, _info("1.1.0", blob), work, "1.0.0",
                          _download_of(blob))
    assert result == "updated"
    assert (work / "current").resolve() == (work / "releases" / "1.1.0").resolve()
    assert (work / "releases" / "1.1.0" / "pkg" / "__init__.py").exists()
    restarts = [c for c in fake.calls if c[:2] == ["systemctl", "restart"]]
    assert len(restarts) == 1  # exactly one restart on the happy path


def test_checksum_failure_aborts_before_touching_anything(
        tmp_path, monkeypatch, work):
    blob = _make_tarball({"pkg/__init__.py": b"x = 1\n"})
    info = _info("1.1.0", b"different bytes")
    with pytest.raises(self_update.UpdateError, match="checksum mismatch"):
        _apply(monkeypatch, info, work, "1.0.0", _download_of(blob))
    # tarball deleted, no release dir, symlink untouched
    assert not (work / "updates" / "1.1.0" / "worker.tar.gz").exists()
    assert not (work / "releases" / "1.1.0").exists()
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_corrupt_archive_aborts_with_no_half_extracted_release(
        tmp_path, monkeypatch, work):
    blob = b"this is not a tarball at all"
    with pytest.raises(self_update.UpdateError, match="corrupt"):
        _apply(monkeypatch, _info("1.1.0", blob), work, "1.0.0",
               _download_of(blob))
    assert not (work / "releases" / "1.1.0").exists()
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_download_failure_aborts(tmp_path, monkeypatch, work):
    def _boom(url, dest):
        raise ConnectionError("network down")

    with pytest.raises(self_update.UpdateError, match="download failed"):
        _apply(monkeypatch, _info("1.1.0", b"x"), work, "1.0.0", _boom)
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_import_failure_removes_release_dir(tmp_path, monkeypatch, work):
    """A release that fails the import self-check never reaches the
    symlink switch, and its directory is cleaned up."""
    blob = _make_tarball({"broken_mod.py": b"import nonexistent_xyz_123\n"})
    monkeypatch.setattr(self_update, "HEALTH_IMPORTS", "import broken_mod")
    with pytest.raises(self_update.UpdateError, match="import check"):
        self_update.apply_update(
            _info("1.1.0", blob), str(work), "1.0.0",
            download_fn=_download_of(blob), log=lambda line: None)
    assert not (work / "releases" / "1.1.0").exists()
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_compile_failure_removes_release_dir(tmp_path, monkeypatch, work):
    blob = _make_tarball({"broken_mod.py": b"def f(:\n  broken syntax ((("})
    monkeypatch.setattr(self_update, "HEALTH_IMPORTS", "import broken_mod")
    with pytest.raises(self_update.UpdateError, match="byte-compile"):
        self_update.apply_update(
            _info("1.1.0", blob), str(work), "1.0.0",
            download_fn=_download_of(blob), log=lambda line: None)
    assert not (work / "releases" / "1.1.0").exists()


def test_restart_failure_rolls_back_symlink(tmp_path, monkeypatch, work):
    blob = _make_tarball({"pkg/__init__.py": b"x = 1\n"})
    with pytest.raises(self_update.UpdateError, match="systemctl restart failed"):
        _apply(monkeypatch, _info("1.1.0", blob), work, "1.0.0",
               _download_of(blob), restart_results=(1, "unit not found"))
    # symlink swung back; the new release stays on disk for forensics but
    # is no longer live
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_health_failure_rolls_back_and_restarts_old(
        tmp_path, monkeypatch, work):
    blob = _make_tarball({"pkg/__init__.py": b"x = 1\n"})
    with pytest.raises(self_update.UpdateError, match="did not come up healthy"):
        _apply(monkeypatch, _info("1.1.0", blob), work, "1.0.0",
               _download_of(blob), healthy=False)
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_health_failure_restart_of_old_is_attempted(
        tmp_path, monkeypatch, work):
    """On health-gate failure the old release is restarted (two systemctl
    restarts total: new, then old)."""
    blob = _make_tarball({"pkg/__init__.py": b"x = 1\n"})
    fake = _SubprocessFake()
    monkeypatch.setattr(self_update.subprocess, "run", fake)
    monkeypatch.setattr(self_update, "_health_check_release",
                        lambda release_dir, log=None: None)
    monkeypatch.setattr(self_update, "wait_for_healthy",
                        lambda *a, **k: False)
    with pytest.raises(self_update.UpdateError, match="did not come up healthy"):
        self_update.apply_update(
            _info("1.1.0", blob), str(work), "1.0.0",
            download_fn=_download_of(blob), log=lambda line: None)
    restarts = [c for c in fake.calls if c[:2] == ["systemctl", "restart"]]
    assert len(restarts) == 2
    assert (work / "current").resolve() == (work / "releases" / "1.0.0").resolve()


def test_not_newer_is_noop_without_download(tmp_path, monkeypatch, work):
    def _must_not_run(url, dest):
        raise AssertionError("download must not be attempted")

    assert self_update.apply_update(
        _info("1.0.0", b"x"), str(work), "1.0.0",
        download_fn=_must_not_run) == "noop"
    assert self_update.apply_update(
        {"version": "0.9.0", "url": "https://example.com/x",
         "sha256": "ab" * 32}, str(work), "1.0.0",
        download_fn=_must_not_run) == "noop"


def test_missing_fields_rejected(tmp_path, work):
    with pytest.raises(self_update.UpdateError, match="missing version/url/sha256"):
        self_update.apply_update({"version": "1.1.0"}, str(work), "1.0.0")


def test_check_and_apply_noop_without_advertised_update(tmp_path, work):
    class _Ctx:
        class config:
            work_dir = str(work)
            worker_version = "1.0.0"

    assert self_update.check_and_apply({}, _Ctx()) == "noop"
    assert self_update.check_and_apply({"worker_update": None}, _Ctx()) == "noop"


# ---------------------------------------------------------------------------
# wait_for_healthy unit tests
# ---------------------------------------------------------------------------
def _write_marker(work_dir, version, boot_ts):
    (Path(work_dir) / self_update.STATUS_FILENAME).write_text(
        json.dumps({"version": version, "boot_ts": boot_ts, "pid": 1234}))


def test_wait_for_healthy_accepts_fresh_marker(tmp_path):
    pre_ts = time.time() - 5
    _write_marker(str(tmp_path), "1.1.0", time.time())
    assert self_update.wait_for_healthy(
        str(tmp_path), "1.1.0", pre_ts,
        timeout_s=5, is_active_fn=lambda: True,
        sleep_fn=lambda s: None) is True


def test_wait_for_healthy_rejects_stale_marker(tmp_path):
    pre_ts = time.time()
    _write_marker(str(tmp_path), "1.1.0", pre_ts - 600)  # old boot
    assert self_update.wait_for_healthy(
        str(tmp_path), "1.1.0", pre_ts,
        timeout_s=1, is_active_fn=lambda: True,
        sleep_fn=lambda s: None) is False


def test_wait_for_healthy_rejects_wrong_version(tmp_path):
    pre_ts = time.time() - 5
    _write_marker(str(tmp_path), "1.0.0", time.time())  # old version came up
    assert self_update.wait_for_healthy(
        str(tmp_path), "1.1.0", pre_ts,
        timeout_s=1, is_active_fn=lambda: True,
        sleep_fn=lambda s: None) is False


def test_wait_for_healthy_requires_active_service(tmp_path):
    pre_ts = time.time() - 5
    _write_marker(str(tmp_path), "1.1.0", time.time())
    assert self_update.wait_for_healthy(
        str(tmp_path), "1.1.0", pre_ts,
        timeout_s=1, is_active_fn=lambda: False,
        sleep_fn=lambda s: None) is False


def test_is_newer_ordering():
    assert self_update.is_newer("1.1.0", "1.0.0")
    assert self_update.is_newer("1.0.1", "1.0.0")
    assert not self_update.is_newer("1.0.0", "1.0.0")
    assert not self_update.is_newer("0.9.9", "1.0.0")
