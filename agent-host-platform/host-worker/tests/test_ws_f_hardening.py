"""WS-F production hardening tests (§16 artifact security, §17 docker
security, §18 compose, §19 health checks).

Covers the defects fixed in the final hardening pass:

  * deployments.pipeline.extract_archive — decompression-bomb caps
    (declared uncompressed size > 16x archive size; member-count cap),
    device files / fifos rejected, benign archives still extract.
  * deployments.pipeline.validate_artifact_id — path-component validation
    for artifact_id values that become artifacts/<id>.bin on disk.
  * agent.api.ControlPlaneClient.download_artifact — rejects a hostile
    artifact_id before makedirs()/network (no directory creation outside
    the work dir).
  * executor.handlers.handle_build / handle_artifact_download — HandlerError
    on hostile artifact_id.
  * docker.client.DockerClient.run — rejects a dash-led image (the image is
    the last positional in the argv; a leading "-" would be parsed as a
    docker flag).
  * deployments.compose_validate — denies `runtime:` and
    `userns_mode: host`.
  * pipeline.compose_project_name_for — per-project isolation of compose
    project names; legacy (pre-suffix) stacks are torn down on redeploy.
  * health.checker.check_once — redirects are never followed.
"""
import hashlib
import io
import json
import socket
import tarfile
import zipfile
from pathlib import Path

import pytest

from agent.api import ControlPlaneClient, WorkerAPIError
from deployments import compose_validate, pipeline
from deployments.state import DeploymentStore
from docker import client as docker_client_mod
from executor import handlers
from health import checker as health_checker
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

def _tar_bytes_raw(members):
    """members: list of tarfile.TarInfo (+ optional BytesIO data)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for ti, data in members:
            tf.addfile(ti, data)
    return buf.getvalue()


def _tar_file(name, size, data_byte=b"a"):
    ti = tarfile.TarInfo(name)
    ti.size = size
    # Store only a small real payload but DECLARE a huge size: the
    # declaration is what a bomb's headers carry (e.g. 42.zip-style).
    # tarfile writes size bytes, so we fake it by patching the header
    # after the fact instead — see _tar_bomb.
    return ti


def _tar_bomb(member_name, declared_size):
    """A tar.gz whose member header declares declared_size bytes.

    Built by writing a real small member, patching the size octal in the
    raw header, then recomputing the header checksum — the archive stays
    tiny on disk while extraction would write declared_size bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo(member_name)
        ti.size = 8
        tf.addfile(ti, io.BytesIO(b"tinydata"))
    raw = bytearray(buf.getvalue())
    # size field: bytes 124..136 (12 bytes, octal, NUL-terminated)
    octal = oct(declared_size)[2:].encode("ascii").rjust(11, b"0") + b"\x00"
    raw[124:136] = octal
    # recompute the header checksum (bytes 148..156), else "bad checksum"
    raw[148:156] = b"        "
    chksum = sum(raw[0:512])
    raw[148:156] = ("%06o\x00 " % chksum).encode("ascii")
    # re-tar through gzip so the file is a valid .tar.gz
    out = io.BytesIO()
    import gzip
    with gzip.open(out, "wb") as gz:
        gz.write(bytes(raw))
    return out.getvalue()


def _zip_bomb(member_name, declared_size):
    """A zip whose headers declare declared_size for a member.

    file_size is a 32-bit field (zip64 aside), so the max declarable bomb
    here is 0xFFFFFFFF; tests monkeypatch MAX_EXTRACT_BYTES down to make
    the rejection deterministic.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_name, b"tiny")
    raw = bytearray(buf.getvalue())
    loc = raw.find(member_name.encode())
    assert loc != -1
    lh = loc - 30  # local header starts 30 bytes before the filename
    assert raw[lh:lh + 4] == b"PK\x03\x04"
    raw[lh + 18:lh + 22] = declared_size.to_bytes(4, "little")
    cd = raw.find(b"PK\x01\x02")
    assert cd != -1
    raw[cd + 24:cd + 28] = declared_size.to_bytes(4, "little")
    return bytes(raw)


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# §16: decompression-bomb caps
# ---------------------------------------------------------------------------

def test_extract_archive_rejects_tar_bomb(tmp_path):
    bomb = _tar_bomb("huge.bin", 10 * 1024 * 1024 * 1024)  # 10GB declared
    assert len(bomb) < 2048  # tiny on disk
    archive = _write(tmp_path, "bomb.tar.gz", bomb)
    with pytest.raises(pipeline.DeployError, match="decompression bomb"):
        pipeline.extract_archive(str(archive), str(tmp_path / "dest"))
    assert not (tmp_path / "dest" / "huge.bin").exists()


def test_extract_archive_rejects_zip_bomb(tmp_path, monkeypatch):
    # file_size is u32: max declarable is 0xFFFFFFFF (~4GB). Patch the cap
    # down so the rejection is deterministic under the default 8GiB cap.
    monkeypatch.setattr(pipeline, "MAX_EXTRACT_BYTES", 1024)
    bomb = _zip_bomb("huge.bin", 0xFFFFFFFF)
    archive = _write(tmp_path, "bomb.zip", bomb)
    with pytest.raises(pipeline.DeployError, match="decompression bomb"):
        pipeline.extract_archive(str(archive), str(tmp_path / "zdest"))
    assert not (tmp_path / "zdest" / "huge.bin").exists()


def test_extract_archive_rejects_member_count_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_ARCHIVE_MEMBERS", 4)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(10):
            ti = tarfile.TarInfo(f"f{i}.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
    archive = _write(tmp_path, "many.tar.gz", buf.getvalue())
    with pytest.raises(pipeline.DeployError, match="more than"):
        pipeline.extract_archive(str(archive), str(tmp_path / "dest"))


def test_extract_archive_allows_compressible_but_sane_archive(tmp_path):
    # 200KB of highly compressible data in a ~1KB archive: ratio is high
    # but the absolute declared size is small — must still extract.
    data = b"a" * 200_000
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("big.txt")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    assert len(raw) < len(data) // 50
    archive = _write(tmp_path, "ok.tar.gz", raw)
    dest = pipeline.extract_archive(str(archive), str(tmp_path / "dest"))
    assert (Path(dest) / "big.txt").read_bytes() == data


# ---------------------------------------------------------------------------
# §16: special files rejected
# ---------------------------------------------------------------------------

def _special_tar(kind):
    ti = tarfile.TarInfo("node")
    ti.type = kind
    if kind == tarfile.CHRTYPE:
        ti.devmajor, ti.devminor = 1, 5
    return _tar_bytes_raw([(ti, None)])


def test_extract_archive_rejects_char_device(tmp_path):
    archive = _write(tmp_path, "dev.tar.gz", _special_tar(tarfile.CHRTYPE))
    with pytest.raises(pipeline.DeployError, match="extraction failed"):
        pipeline.extract_archive(str(archive), str(tmp_path / "dest"))
    assert not (tmp_path / "dest" / "node").exists()


def test_extract_archive_rejects_fifo(tmp_path):
    archive = _write(tmp_path, "fifo.tar.gz", _special_tar(tarfile.FIFOTYPE))
    with pytest.raises(pipeline.DeployError, match="extraction failed"):
        pipeline.extract_archive(str(archive), str(tmp_path / "dest"))
    assert not (tmp_path / "dest" / "node").exists()


def test_extract_archive_rejects_hardlink_escape(tmp_path):
    ti = tarfile.TarInfo("hl")
    ti.type = tarfile.LNKTYPE
    ti.linkname = "/etc/passwd"
    archive = _write(tmp_path, "hl.tar.gz", _tar_bytes_raw([(ti, None)]))
    with pytest.raises(pipeline.DeployError):
        pipeline.extract_archive(str(archive), str(tmp_path / "dest"))


# ---------------------------------------------------------------------------
# §16: checksum format validation (no unhandled TypeError on hostile input)
# ---------------------------------------------------------------------------

def test_verify_checksum_rejects_malformed_digest(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"data")
    good = "sha256:" + hashlib.sha256(b"data").hexdigest()
    assert pipeline.verify_checksum(str(path), good) is True
    assert pipeline.verify_checksum(str(path), "sha256:" + "0" * 64) is False
    # Malformed digests raise DeployError — never an unhandled TypeError
    # from compare_digest (non-ASCII) and never a silent False that the
    # caller would quarantine as a "mismatch".
    for bad in ["sha256:abc", "sha256:" + "zz" * 32, "sha256:é" * 16,
                "md5:" + "0" * 32, "sha256:"]:
        with pytest.raises(pipeline.DeployError, match="checksum format"):
            pipeline.verify_checksum(str(path), bad)


# ---------------------------------------------------------------------------
# §16: artifact_id path-component validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", [
    "art-1", "abc123", "550e8400-e29b-41d4-a716-446655440000",
    "a" * 128, "x.y_z-9",
])
def test_validate_artifact_id_accepts(good):
    assert pipeline.validate_artifact_id(good) == good


@pytest.mark.parametrize("bad", [
    "../../config/worker", "..", "../x", "/abs", "a/b", "", ".",
    "x" * 129, "-lead", " semi", "a;b", None, 123,
])
def test_validate_artifact_id_rejects(bad):
    with pytest.raises(ValueError, match="invalid artifact_id"):
        pipeline.validate_artifact_id(bad)


def test_api_download_rejects_hostile_artifact_id_without_side_effects(tmp_path):
    client = ControlPlaneClient("http://127.0.0.1:9", "tok")
    evil = str(tmp_path / "artifacts" / ".." / ".." / "pwned.bin")
    with pytest.raises(WorkerAPIError, match="invalid artifact_id"):
        client.download_artifact("../../pwned", evil)
    # makedirs() must not have run: no directory created outside work area.
    assert not (tmp_path / "pwned.bin").exists()
    assert not (tmp_path / "pwned.bin.part").exists()


class _DummyCtx:
    pass


def test_handle_build_rejects_hostile_artifact_id():
    task = {"id": "t1", "type": "docker-build",
            "payload": {"project_id": "p1", "artifact_id": "../../x"}}
    with pytest.raises(handlers.HandlerError, match="invalid artifact_id"):
        handlers.handle_build(_DummyCtx(), task)


def test_handle_artifact_download_rejects_hostile_artifact_id():
    task = {"id": "t1", "type": "artifact-download",
            "payload": {"artifact_id": ".."}}
    with pytest.raises(handlers.HandlerError, match="invalid artifact_id"):
        handlers.handle_artifact_download(_DummyCtx(), task)


# ---------------------------------------------------------------------------
# §17: docker run image positional guard
# ---------------------------------------------------------------------------

def _bare_client():
    # Skip __init__ (requires a docker binary); run() validates before
    # any subprocess use.
    return docker_client_mod.DockerClient.__new__(docker_client_mod.DockerClient)


def test_docker_run_rejects_dash_led_image():
    client = _bare_client()
    with pytest.raises(ValueError, match="must not start with"):
        client.run("c", "--privileged")
    with pytest.raises(ValueError, match="must not start with"):
        client.run("c", "-v")
    with pytest.raises(ValueError, match="invalid image"):
        client.run("c", "")


def test_docker_run_accepts_normal_image_shape():
    # Validation passes; the failure (if any) comes from _run, not the
    # guard — prove the guard lets sane values through.
    client = _bare_client()
    calls = []

    def fake_run(*argv, **kwargs):
        calls.append(argv)
        raise RuntimeError("stop here")

    client._run = fake_run
    with pytest.raises(RuntimeError, match="stop here"):
        client.run("My Container!", "nginx:1.27", ports={8080: 80})
    argv = calls[0]
    assert argv[-1] == "nginx:1.27"  # image stays the last positional
    assert "--privileged" not in argv


# ---------------------------------------------------------------------------
# §17/§18: compose validator additions
# ---------------------------------------------------------------------------

def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def test_validator_denies_runtime(tmp_path):
    model = {"services": {"web": {"image": "x:1", "runtime": "runc"}}}
    errors = compose_validate.validate_compose_model(model, _ws(tmp_path))
    assert any("runtime is denied" in e for e in errors)


def test_validator_denies_userns_mode_host(tmp_path):
    model = {"services": {"web": {"image": "x:1", "userns_mode": "host"}}}
    errors = compose_validate.validate_compose_model(model, _ws(tmp_path))
    assert any("userns_mode" in e and "denied" in e for e in errors)


def test_validator_allows_benign_without_runtime_or_userns(tmp_path):
    model = {"services": {"web": {"image": "x:1",
                                  "ports": [{"published": 8080,
                                             "target": 80}]}}}
    assert compose_validate.validate_compose_model(
        model, _ws(tmp_path)) == []


# ---------------------------------------------------------------------------
# §18: compose project-name isolation
# ---------------------------------------------------------------------------

def test_compose_project_name_includes_project_id():
    name = pipeline.compose_project_name_for(
        "550e8400-e29b-41d4-a716-446655440000", "my-app")
    assert name == "uaht-my-app-550e8400"


def test_compose_project_name_differs_across_projects():
    a = pipeline.compose_project_name_for("11111111-2222-3333-4444-555555555555",
                                          "my-app")
    b = pipeline.compose_project_name_for("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                          "my-app")
    assert a != b
    # The pre-fix collision: "My App" and "my-app" sanitize identically.
    c = pipeline.compose_project_name_for("11111111-2222-3333-4444-555555555555",
                                          "my-app")
    assert a == c  # same project -> same stack (in-place redeploy)


# ---------------------------------------------------------------------------
# §19: health checker never follows redirects
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_check_once_does_not_follow_redirects(monkeypatch):
    seen = {}

    def fake_get(url, timeout=5, **kwargs):
        seen.update(kwargs)
        return _FakeResp(302, {"Location": "http://169.254.169.254/x"})

    monkeypatch.setattr(health_checker.requests, "get", fake_get)
    assert health_checker.check_once("http://127.0.0.1:9999/") is False
    assert seen.get("allow_redirects") is False


def test_check_once_true_only_on_200(monkeypatch):
    def fake_get(url, timeout=5, **kwargs):
        return _FakeResp(201)

    monkeypatch.setattr(health_checker.requests, "get", fake_get)
    assert health_checker.check_once("http://127.0.0.1:9999/") is False

    def fake_get_500(url, timeout=5, **kwargs):
        return _FakeResp(500)

    monkeypatch.setattr(health_checker.requests, "get", fake_get_500)
    assert health_checker.check_once("http://127.0.0.1:9999/") is False

    def fake_get_200(url, timeout=5, **kwargs):
        return _FakeResp(200)

    monkeypatch.setattr(health_checker.requests, "get", fake_get_200)
    assert health_checker.check_once("http://127.0.0.1:9999/") is True


def test_check_once_connection_refused_is_false(monkeypatch):
    import requests

    def fake_get(url, timeout=5, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(health_checker.requests, "get", fake_get)
    assert health_checker.check_once("http://127.0.0.1:9999/") is False


# ---------------------------------------------------------------------------
# §18: legacy (pre-suffix) compose stacks are torn down on redeploy
# ---------------------------------------------------------------------------

class _FakeDockerCompose:
    """Minimal compose-capable fake for the legacy-migration test."""

    def __init__(self):
        self.compose_up_calls = []
        self.compose_down_calls = []
        self.compose_config_models = {}
        self.next_compose_ports = []
        self.ps_rows = []
        self.compose_projects = {}

    def compose_available(self):
        return True

    def compose_config_json(self, compose_file, timeout=60):
        return self.compose_config_models[compose_file]

    def compose_up(self, compose_file, project_name=None, build=False,
                   timeout=1200):
        self.compose_up_calls.append((compose_file, project_name, build))
        self.compose_projects[project_name] = {
            "ports": list(self.next_compose_ports)}
        return "up"

    def compose_down(self, compose_file, project_name=None, timeout=300):
        self.compose_down_calls.append((compose_file, project_name))
        self.compose_projects.pop(project_name, None)
        return "down"

    def compose_ps(self, project_name, timeout=60):
        data = self.compose_projects.get(project_name)
        if not data:
            return []
        return [{
            "Name": f"{project_name}-web-1",
            "State": "running",
            "Publishers": [{"PublishedPort": str(p), "TargetPort": 3000,
                            "Protocol": "tcp"} for p in data["ports"]],
        }]

    def ps(self, all=False):
        return list(self.ps_rows)

    def container_exists(self, name):
        return False

    def stop(self, name, timeout_secs=10, timeout=120):
        pass

    def rm(self, name, force=False, timeout=120):
        pass


class _FakeAPIDl:
    def __init__(self, data):
        self.data = data

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(self.data)
        return dest_path


class _FakeConfig:
    def __init__(self, work_dir):
        self.work_dir = str(work_dir)
        self.apps_dir = str(work_dir / "apps")


class _FakeCtx:
    def __init__(self, tmp_path, docker, api):
        self.config = _FakeConfig(tmp_path)
        self.api = api
        self.docker = docker
        self.log_store = LogStore(str(tmp_path / "logs"))
        self.deployment_store = DeploymentStore(str(tmp_path))
        self.scrub = lambda s: s

    def log(self, task_id, line):
        return self.scrub(line)

    def require_docker(self):
        return self.docker


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tarball(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            raw = data.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


def test_deploy_compose_tears_down_legacy_project_on_redeploy(
        tmp_path, monkeypatch):
    from health import checker
    monkeypatch.setattr(checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    port = _free_port()
    project_id = "11111111-2222-3333-4444-555555555555"
    manifest = {"name": "webapp", "runtime": "docker-compose",
                "service": {"port": 3000, "healthcheck": "/"}}
    art = _tarball({"docker-compose.yml": "services:\n  web:\n    image: x:1\n",
                    "agent.deploy.json": json.dumps(manifest)})
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    docker = _FakeDockerCompose()
    docker.next_compose_ports = [port]
    ctx = _FakeCtx(tmp_path, docker, _FakeAPIDl(art))

    # Previous RUNNING deployment used the legacy (pre-suffix) project name.
    legacy_file = (Path(ctx.config.work_dir) / "deployments" / "dep-old"
                   / "source" / "docker-compose.yml")
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text("services:\n  web:\n    image: x:1\n")
    ctx.deployment_store.save({
        "deployment_id": "dep-old", "project_id": project_id,
        "project_name": "webapp", "version": "0.9",
        "compose_project": "uaht-webapp",  # legacy name
        "compose_file": str(legacy_file),
        "container_name": None, "status": "running",
        "health_status": "healthy",
        "created_at": "2026-10-05T00:00:00Z",
    })

    new_model = {"services": {"web": {
        "image": "x:1", "ports": [{"published": port, "target": 3000}]}}}
    model_path = str(Path(ctx.config.work_dir) / "deployments" / "dep-new"
                     / "source" / "docker-compose.yml")
    docker.compose_config_models[model_path] = new_model

    task = {"id": "t-new", "type": "deploy", "payload": {
        "project_id": project_id, "project_name": "webapp",
        "version": "1.0.0", "deployment_id": "dep-new",
        "artifact_id": "art-new", "artifact_checksum": checksum,
        "artifact_size": len(art), "manifest": manifest,
        "healthcheck_timeout": 3,
    }}
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"
    # Legacy stack torn down first...
    assert docker.compose_down_calls == [(str(legacy_file), "uaht-webapp")]
    # ...and the new stack uses the isolated (suffixed) project name.
    assert docker.compose_up_calls[0][1] == "uaht-webapp-11111111"
    state = ctx.deployment_store.load("dep-new")
    assert state["compose_project"] == "uaht-webapp-11111111"
