"""Spec section 43 acceptance cases: artifact + archive + manifest safety.

Each case must FAIL SAFELY: a clear error, no docker interaction, no
partial side effects (no container/image/network changes, no running
deployment recorded, malicious bytes quarantined or removed).

  1. wrong SHA-256        -> checksum mismatch, artifact quarantined
  2. wrong size           -> download refused (real ControlPlaneClient +
                             pipeline level)
  3. corrupt archive      -> DeployError, nothing extracted
  4. malicious traversal  -> tar ../, tar absolute, zip ../ all rejected
  5. malicious symlink    -> tar symlink->outside, tar hardlink->outside
                             rejected; zip symlink entries land as regular
                             files (never materialize as symlinks)
  6. missing manifest     -> DeployError, no docker calls
  7. invalid manifest     -> DeployError listing validation errors
  8. unsupported runtime  -> DeployError naming the runtime
"""
import hashlib
import io
import json
import os
import stat
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent.api import ControlPlaneClient, WorkerAPIError
from deployments import pipeline
from deployments.state import DeploymentStore
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDocker:
    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, args))

    def __getattr__(self, name):
        # Any docker interaction is recorded; tests assert none happens.
        def _rec(*args, **kwargs):
            self._record(name, *args)
            raise AssertionError(
                f"docker.{name} must not be called on this path")
        return _rec


class FakeAPI:
    """Mirrors the real client's size enforcement."""

    def __init__(self, artifact_bytes: bytes):
        self.artifact_bytes = artifact_bytes

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        data = self.artifact_bytes
        if expected_size is not None and len(data) != expected_size:
            raise WorkerAPIError(
                f"artifact {artifact_id} size mismatch: got {len(data)}, "
                f"expected {expected_size}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return dest_path


class FakeConfig:
    def __init__(self, work_dir):
        self.work_dir = str(work_dir)
        self.apps_dir = str(work_dir / "apps")


class FakeCtx:
    def __init__(self, tmp_path, docker, api):
        self.config = FakeConfig(tmp_path)
        self.api = api
        self.docker = docker
        self.log_store = LogStore(str(tmp_path / "logs"))
        self.deployment_store = DeploymentStore(str(tmp_path))
        self.scrub = lambda s: s

    def log(self, task_id, line):
        clean = self.scrub(line)
        self.log_store.append_task(task_id, clean)
        return clean

    def require_docker(self):
        return self.docker


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _tar_members(members) -> bytes:
    """members: (name, data|target, kind) with kind in file|symlink|hardlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload, kind in members:
            ti = tarfile.TarInfo(name)
            if kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                tf.addfile(ti)
            elif kind == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = payload
                tf.addfile(ti)
            else:
                data = payload if isinstance(payload, bytes) else payload.encode()
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _zip_members(members) -> bytes:
    """members: (name, data, symlink: bool)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, symlink in members:
            zi = zipfile.ZipInfo(name)
            if symlink:
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(zi, data if isinstance(data, bytes) else data.encode())
    return buf.getvalue()


def _manifest(**over):
    m = {"name": "spec43app", "runtime": "docker",
         "service": {"port": 3000, "healthcheck": "/"}}
    m.update(over)
    return m


def _deploy_task(artifact_id, checksum, size, manifest=_manifest(),
                 deployment_id="dep-spec43"):
    payload = {
        "project_id": "proj-1",
        "project_name": "spec43app",
        "version": "1.0.0",
        "deployment_id": deployment_id,
        "artifact_id": artifact_id,
        "artifact_checksum": checksum,
        "artifact_size": size,
        "image": "prebuilt:1",  # no build step; failures happen before run
        "healthcheck_timeout": 1,
    }
    if manifest is not None:
        payload["manifest"] = manifest
    return {"id": "task-spec43", "type": "deploy", "payload": payload}


def _artifact_with(manifest_dict, files=None, fmt="tar"):
    files = dict(files or {})
    if manifest_dict is not None:
        files["agent.deploy.json"] = json.dumps(manifest_dict)
    if fmt == "tar":
        return _tar_members([(n, d, "file") for n, d in files.items()])
    return _zip_members([(n, d, False) for n, d in files.items()])


def _no_side_effects(ctx, docker, deployment_id):
    assert docker.calls == [], f"docker was touched: {docker.calls}"
    assert ctx.deployment_store.load(deployment_id) is None


# ---------------------------------------------------------------------------
# 1. wrong SHA-256
# ---------------------------------------------------------------------------
def test_wrong_sha256_quarantines_and_aborts_before_docker(tmp_path):
    art = _artifact_with(_manifest(), {"app.py": "print(1)"})
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    bad_sum = "sha256:" + "0" * 64
    with pytest.raises(pipeline.DeployError, match="checksum mismatch"):
        pipeline.deploy(ctx, _deploy_task("art-1", bad_sum, len(art)))
    quarantined = list((Path(ctx.config.work_dir) / "quarantine").glob("*.bin"))
    assert len(quarantined) == 1, "bad artifact must be quarantined"
    assert quarantined[0].read_bytes() == art
    _no_side_effects(ctx, docker, "dep-spec43")


# ---------------------------------------------------------------------------
# 2. wrong size
# ---------------------------------------------------------------------------
class _FixedBytesHandler(BaseHTTPRequestHandler):
    BODY = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)

    def log_message(self, *a):
        pass


@pytest.fixture()
def http_plane():
    _FixedBytesHandler.BODY = b"x" * 100
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixedBytesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_wrong_size_real_client_refuses_and_cleans_up(tmp_path, http_plane):
    client = ControlPlaneClient(http_plane, "tok")
    dest = str(tmp_path / "art.bin")
    with pytest.raises(WorkerAPIError, match="size mismatch"):
        client.download_artifact("art-1", dest, expected_size=99)
    assert not Path(dest).exists()
    assert not Path(dest + ".part").exists(), "partial file must be removed"


def test_correct_size_real_client_downloads(tmp_path, http_plane):
    client = ControlPlaneClient(http_plane, "tok")
    dest = str(tmp_path / "art.bin")
    client.download_artifact("art-1", dest, expected_size=100)
    assert Path(dest).read_bytes() == b"x" * 100


def test_wrong_size_pipeline_level_aborts_before_docker(tmp_path):
    art = _artifact_with(_manifest(), {"app.py": "print(1)"})
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    with pytest.raises(WorkerAPIError, match="size mismatch"):
        pipeline.deploy(ctx, _deploy_task("art-1",
                                          "sha256:" + hashlib.sha256(art).hexdigest(),
                                          len(art) + 1))
    _no_side_effects(ctx, docker, "dep-spec43")


# ---------------------------------------------------------------------------
# 3. corrupt archive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    b"\x00\x01\x02not a tar or zip at all\xff\xfe",
    b"PK\x03\x04 truncated zip header",
])
def test_corrupt_archive_fails_safely(tmp_path, payload):
    art = payload
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    with pytest.raises(pipeline.DeployError,
                       match="unsupported artifact format|extraction failed"):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(art).hexdigest(), len(art)))
    _no_side_effects(ctx, docker, "dep-spec43")


def test_truncated_tarball_fails_safely(tmp_path):
    full = _artifact_with(_manifest(), {"app.py": "print(1)" * 1000})
    art = full[: len(full) // 3]  # truncated mid-stream
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    with pytest.raises(pipeline.DeployError,
                       match="unsupported artifact format|extraction failed"):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(art).hexdigest(), len(art)))
    _no_side_effects(ctx, docker, "dep-spec43")


# ---------------------------------------------------------------------------
# 4. malicious traversal paths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("member", ["../../escape.txt", "/abs/escape.txt"])
def test_malicious_tar_traversal_rejected(tmp_path, member):
    evil = _tar_members([(member, b"pwned", "file"),
                         ("agent.deploy.json", json.dumps(_manifest()), "file")])
    dest = tmp_path / "dest"
    with pytest.raises(pipeline.DeployError):
        pipeline.extract_archive(_write(tmp_path, evil), str(dest))
    assert not (tmp_path / "escape.txt").exists()
    assert not Path("/abs/escape.txt").exists()


def test_malicious_zip_traversal_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../zip-escape.txt", b"pwned")
        zf.writestr("agent.deploy.json", json.dumps(_manifest()))
    with pytest.raises(pipeline.DeployError, match="escapes destination"):
        pipeline.extract_archive(_write(tmp_path, buf.getvalue()),
                                 str(tmp_path / "zdest"))
    assert not (tmp_path / "zip-escape.txt").exists()


def test_traversal_pipeline_level_no_side_effects(tmp_path):
    evil = _tar_members([("../../escape.txt", b"pwned", "file")])
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(evil))
    with pytest.raises(pipeline.DeployError):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(evil).hexdigest(), len(evil)))
    _no_side_effects(ctx, docker, "dep-spec43")
    assert not (tmp_path / "escape.txt").exists()


def _write(tmp_path, data: bytes) -> str:
    p = tmp_path / "archive.bin"
    p.write_bytes(data)
    return str(p)


# ---------------------------------------------------------------------------
# 5. malicious symlink / hardlink
# ---------------------------------------------------------------------------
def test_malicious_tar_symlink_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = _tar_members([
        ("link", str(outside), "symlink"),
        ("link/pwned.txt", b"pwned", "file"),
        ("agent.deploy.json", json.dumps(_manifest()), "file"),
    ])
    with pytest.raises(pipeline.DeployError):
        pipeline.extract_archive(_write(tmp_path, evil), str(tmp_path / "dest"))
    assert list(outside.iterdir()) == [], "nothing may be written outside"


def test_malicious_tar_hardlink_rejected(tmp_path):
    evil = _tar_members([
        ("hard", "/etc/passwd", "hardlink"),
        ("agent.deploy.json", json.dumps(_manifest()), "file"),
    ])
    with pytest.raises(pipeline.DeployError):
        pipeline.extract_archive(_write(tmp_path, evil), str(tmp_path / "dest"))
    assert not (tmp_path / "dest" / "hard").exists()


def test_zip_symlink_entry_never_materializes_as_symlink(tmp_path):
    data = _zip_members([
        ("link", "/etc/passwd", True),  # symlink-flagged entry
        ("agent.deploy.json", json.dumps(_manifest()), False),
    ])
    dest = tmp_path / "zdest"
    pipeline.extract_archive(_write(tmp_path, data), str(dest))
    link = dest / "link"
    assert not link.is_symlink(), "zip extraction must not create symlinks"
    assert link.is_file()  # lands as a regular file holding the target text


def test_symlink_pipeline_level_no_side_effects(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = _tar_members([
        ("link", str(outside), "symlink"),
        ("link/pwned.txt", b"pwned", "file"),
    ])
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(evil))
    with pytest.raises(pipeline.DeployError):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(evil).hexdigest(), len(evil)))
    _no_side_effects(ctx, docker, "dep-spec43")
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# 6. missing manifest
# ---------------------------------------------------------------------------
def test_missing_manifest_fails_with_clear_error(tmp_path):
    art = _tar_members([("app.py", "print(1)", "file")])  # no manifest
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    with pytest.raises(pipeline.DeployError, match="no agent.deploy.json"):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(art).hexdigest(), len(art),
            manifest=None))
    _no_side_effects(ctx, docker, "dep-spec43")


# ---------------------------------------------------------------------------
# 7. invalid manifest
# ---------------------------------------------------------------------------
def test_invalid_manifest_fails_with_validation_errors(tmp_path):
    bad = {"name": "", "runtime": "docker", "service": {"port": 99999}}
    art = _artifact_with(bad, {"app.py": "print(1)"})
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    with pytest.raises(pipeline.DeployError,
                       match="manifest validation failed"):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(art).hexdigest(), len(art),
            manifest=None))
    _no_side_effects(ctx, docker, "dep-spec43")


# ---------------------------------------------------------------------------
# 8. unsupported runtime
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("runtime", ["kubernetes", "lambda", "vm"])
def test_unsupported_runtime_rejected(tmp_path, runtime):
    art = _artifact_with(_manifest(runtime=runtime), {"app.py": "print(1)"})
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    with pytest.raises(pipeline.DeployError, match="runtime"):
        pipeline.deploy(ctx, _deploy_task(
            "art-1", "sha256:" + hashlib.sha256(art).hexdigest(), len(art),
            manifest=None))
    _no_side_effects(ctx, docker, "dep-spec43")
