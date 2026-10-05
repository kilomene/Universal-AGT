"""Phase 6 security tests (worker side).

Covers the Phase-1 audit findings fixed in Phase 6:
  * docker-run `extra_args` removal / rejection (CRITICAL)
  * tar symlink escape via tarfile.data_filter (MEDIUM)
  * build.dockerfile / build.context / compose_file path traversal (MEDIUM)
  * artifact-upload exfiltration of worker.env (MEDIUM)
  * project secrets injected as container env, never persisted (LOW/14)
  * task-id path traversal in log filenames (LOW/10, verify)
  * handlers._confined_path escapes (MEDIUM/7, verify)
  * --self-check CLI + post-update health gate helpers (MEDIUM/9)
"""
import hashlib
import inspect
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from agent import main as worker_main
from agent import policy
from agent.config import WorkerConfig
from deployments import pipeline
from deployments.state import DeploymentStore
from docker import client as docker_client_mod
from executor import handlers
from executor.dispatcher import TaskDispatcher
from logs.store import LogStore, sanitize_log_id
from updater import self_update


# ---------------------------------------------------------------------------
# Fakes (same injection style as test_pipeline.py)
# ---------------------------------------------------------------------------
class FakeDockerClient:
    def __init__(self):
        self.run_calls = []
        self.containers = {}

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self.run_calls.append({"name": name, "image": image, "env": env or {}})
        self.containers[name] = True
        return "fake-id-" + name

    def container_exists(self, name):
        return name in self.containers

    def stop(self, name, timeout_secs=10, timeout=120):
        pass

    def rm(self, name, force=False, timeout=120):
        self.containers.pop(name, None)

    def ps(self, all=False):
        return []

    def compose_available(self):
        return True

    def compose_config_json(self, compose_file):
        return {"services": {}}

    def compose_up(self, compose_file, project_name=None, build=False):
        return None


class FakeAPI:
    def __init__(self, artifact_bytes=b"", secrets=None):
        self.artifact_bytes = artifact_bytes
        self.secrets = secrets
        self.secret_fetches = []
        self.progress_calls = []

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(self.artifact_bytes)
        return dest_path

    def upload_artifact_content(self, artifact_id, file_path):
        return {"artifact_id": artifact_id}

    def progress(self, task_id, payload):
        self.progress_calls.append((task_id, payload))
        return {"ok": True}

    def get_project_secrets(self, project_id):
        self.secret_fetches.append(project_id)
        if isinstance(self.secrets, Exception):
            raise self.secrets
        return dict(self.secrets or {})


class FakeConfig:
    def __init__(self, work_dir):
        self.work_dir = str(work_dir)
        self.apps_dir = str(work_dir / "apps")
        self.config_path = str(work_dir / "config" / "worker.env")
        self.worker_version = "9.9.9-test"
        self.host_token = "fake-host-token"


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


def _tar_bytes(members):
    """members: list of (name, data|link-target, is_link)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload, is_link in members:
            if is_link:
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                tf.addfile(ti)
            else:
                data = payload if isinstance(payload, bytes) else payload.encode()
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. extra_args removal / rejection
# ---------------------------------------------------------------------------
def test_docker_client_run_has_no_extra_args_param():
    sig = inspect.signature(docker_client_mod.DockerClient.run)
    assert "extra_args" not in sig.parameters


def test_policy_rejects_docker_run_with_extra_args():
    task = {"id": "t1", "type": "docker-run",
            "payload": {"image": "img", "name": "c",
                        "extra_args": ["--privileged", "-v", "/:/host"]}}
    with pytest.raises(policy.TaskRejected, match="extra_args"):
        policy.reject_disallowed_fields("docker-run", task)


def test_policy_allows_docker_run_without_extra_args():
    task = {"id": "t1", "type": "docker-run",
            "payload": {"image": "img", "name": "c"}}
    policy.reject_disallowed_fields("docker-run", task)  # no raise
    policy.reject_disallowed_fields("deploy", {"payload": {"extra_args": []}})


def test_dispatcher_rejects_extra_args_without_executing(tmp_path):
    docker = FakeDockerClient()
    api = FakeAPI()
    ctx = FakeCtx(tmp_path, docker, api)
    dispatcher = TaskDispatcher(ctx, api)
    task = {"id": "t-evil", "type": "docker-run",
            "payload": {"image": "img:x", "name": "evil",
                        "extra_args": ["--privileged"]}}
    outcome = dispatcher.dispatch(task)
    assert outcome["status"] == "failed"
    assert outcome.get("rejected") is True
    # the rejection reason is reported via progress (failed), naming the field
    reported = [p for _, p in api.progress_calls]
    assert reported, "expected progress reports"
    assert "extra_args" in str(reported[-1].get("error", ""))
    assert docker.run_calls == []  # nothing executed


# ---------------------------------------------------------------------------
# 6. tar symlink escape (data_filter)
# ---------------------------------------------------------------------------
def test_extract_archive_blocks_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    evil = _tar_bytes([
        ("link", str(outside), True),          # symlink -> outside dest
        ("link/pwned.txt", b"pwned", False),  # file THROUGH the symlink
    ])
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(evil)
    with pytest.raises(Exception):
        pipeline.extract_archive(str(archive), str(dest))
    assert list(outside.iterdir()) == []  # nothing written outside
    assert not (dest / "link" / "pwned.txt").exists()


def test_extract_archive_blocks_dotdot_member(tmp_path):
    evil = _tar_bytes([("../escape.txt", b"x", False)])
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(evil)
    with pytest.raises(Exception):
        pipeline.extract_archive(str(archive), str(tmp_path / "dest2"))
    assert not (tmp_path / "escape.txt").exists()


def test_extract_archive_allows_benign_tar(tmp_path):
    good = _tar_bytes([("app/main.py", b"print(1)", False),
                       ("data/nested.txt", b"hi", False)])
    archive = tmp_path / "good.tar.gz"
    archive.write_bytes(good)
    dest = pipeline.extract_archive(str(archive), str(tmp_path / "ok"))
    assert (Path(dest) / "app" / "main.py").read_bytes() == b"print(1)"


def test_extract_archive_blocks_zip_dotdot(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../zip-escape.txt", b"x")
    archive = tmp_path / "evil.zip"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(pipeline.DeployError, match="escapes destination"):
        pipeline.extract_archive(str(archive), str(tmp_path / "zdest"))
    assert not (tmp_path / "zip-escape.txt").exists()


# ---------------------------------------------------------------------------
# 7. dockerfile / context / compose_file traversal
# ---------------------------------------------------------------------------
def test_confined_under_rejects_escapes(tmp_path):
    base = tmp_path / "extract"
    base.mkdir()
    with pytest.raises(pipeline.DeployError, match="escapes"):
        pipeline.confined_under(base, "../../etc/passwd", "build.dockerfile")
    with pytest.raises(pipeline.DeployError, match="escapes"):
        pipeline.confined_under(base, "/etc/passwd", "build.context")
    ok = pipeline.confined_under(base, "docker/Dockerfile", "build.dockerfile")
    assert ok == (base / "docker" / "Dockerfile").resolve()


def _artifact_with_manifest(manifest: dict) -> bytes:
    return _tar_bytes([("agent.deploy.json", json.dumps(manifest), False),
                       ("Dockerfile", b"FROM scratch", False)])


def _deploy_task_with_artifact(artifact_id, checksum, **overrides):
    payload = {
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "1.0.0",
        "deployment_id": "dep-trav",
        "artifact_id": artifact_id,
        "artifact_checksum": checksum,
    }
    payload.update(overrides)
    return {"id": "task-trav", "type": "deploy", "payload": payload}


def test_deploy_rejects_dockerfile_traversal(tmp_path, monkeypatch):
    manifest = {"name": "my-api", "runtime": "docker",
                "build": {"dockerfile": "../../evil-Dockerfile", "context": "."},
                "service": {"port": 3000}, "resources": {},
                "restart": "unless-stopped", "env": {}}
    art = _artifact_with_manifest(manifest)
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI(art))
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    with pytest.raises(pipeline.DeployError, match="escapes the allowed directory"):
        pipeline.deploy(ctx, _deploy_task_with_artifact("art-t", checksum))


def test_deploy_rejects_compose_file_traversal(tmp_path, monkeypatch):
    manifest = {"name": "my-api", "runtime": "docker-compose",
                "service": {"port": 3000}, "resources": {},
                "restart": "unless-stopped", "env": {}}
    art = _artifact_with_manifest(manifest)
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI(art))
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    task = _deploy_task_with_artifact("art-c", checksum,
                                      compose_file="/etc/passwd")
    with pytest.raises(pipeline.DeployError, match="escapes the allowed directory"):
        pipeline.deploy(ctx, task)


def test_handlers_confined_path_rejects_escapes(tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    with pytest.raises(handlers.HandlerError, match="escapes"):
        handlers._confined_path(base, "../../x", "dockerfile")
    with pytest.raises(handlers.HandlerError, match="escapes"):
        handlers._confined_path(base, "/abs/path", "context")
    ok = handlers._confined_path(base, "sub/dir", "context")
    assert ok == (base / "sub" / "dir").resolve()


# ---------------------------------------------------------------------------
# 8. worker.env exfiltration via artifact-upload
# ---------------------------------------------------------------------------
def test_artifact_upload_refuses_worker_env(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    secret_env = cfg_dir / "worker.env"
    secret_env.write_text("WORKER_HOST_TOKEN=topsecret-token\n")
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI())
    task = {"id": "t-up", "type": "artifact-upload",
            "payload": {"artifact_id": "a1", "destination": "config/worker.env"}}
    with pytest.raises(handlers.HandlerError, match="sensitive"):
        handlers.handle_artifact_upload(ctx, task)


def test_artifact_upload_refuses_config_dir_subpath(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "other.conf").write_text("x")
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI())
    task = {"id": "t-up", "type": "artifact-upload",
            "payload": {"artifact_id": "a1", "destination": "config/other.conf"}}
    with pytest.raises(handlers.HandlerError, match="sensitive"):
        handlers.handle_artifact_upload(ctx, task)


def test_artifact_upload_allows_benign_file(tmp_path):
    data_dir = tmp_path / "builds"
    data_dir.mkdir()
    target = data_dir / "report.txt"
    target.write_text("benign")
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI())
    task = {"id": "t-up", "type": "artifact-upload",
            "payload": {"artifact_id": "a1", "destination": "builds/report.txt"}}
    out = handlers.handle_artifact_upload(ctx, task)
    assert out["artifact_id"] == "a1"


# ---------------------------------------------------------------------------
# 14. secrets injected as env, never persisted
# ---------------------------------------------------------------------------
def _prebuilt_deploy_task(**overrides):
    payload = {
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "1.0.0",
        "deployment_id": "dep-sec",
        "image": "img:1.0.0",
        "manifest": {"name": "my-api", "runtime": "docker",
                     "service": {"port": 3000, "healthcheck": "/health"},
                     "resources": {}, "restart": "unless-stopped",
                     "env": {"NODE_ENV": "production"}},
        "healthcheck_timeout": 3,
    }
    payload.update(overrides)
    return {"id": "task-sec", "type": "deploy", "payload": payload}


def test_fetched_secrets_injected_but_not_persisted(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    api = FakeAPI(secrets={"API_KEY": "s3cr3t-fetched", "DB_PASS": "p@ss"})
    ctx = FakeCtx(tmp_path, docker, api)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, _prebuilt_deploy_task())
    assert result["status"] == "running"
    assert api.secret_fetches == ["proj-1"]
    env = docker.run_calls[0]["env"]
    assert env["API_KEY"] == "s3cr3t-fetched"
    assert env["DB_PASS"] == "p@ss"
    # ...but the state file carries no secret values
    state = ctx.deployment_store.load("dep-sec")
    blob = json.dumps(state)
    assert "s3cr3t-fetched" not in blob
    assert "p@ss" not in blob


def test_payload_secrets_win_over_fetched(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    api = FakeAPI(secrets={"API_KEY": "fetched-loses"})
    ctx = FakeCtx(tmp_path, docker, api)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    pipeline.deploy(ctx, _prebuilt_deploy_task(
        secrets={"API_KEY": "payload-wins"}))
    assert docker.run_calls[0]["env"]["API_KEY"] == "payload-wins"


def test_secret_fetch_failure_does_not_fail_deploy(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    api = FakeAPI(secrets=RuntimeError("control plane unreachable"))
    ctx = FakeCtx(tmp_path, docker, api)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, _prebuilt_deploy_task())
    assert result["status"] == "running"


# ---------------------------------------------------------------------------
# 10. task-id path traversal in log filenames (verify Phase 2 fix)
# ---------------------------------------------------------------------------
def test_sanitize_log_id_strips_traversal():
    assert sanitize_log_id("../../etc/passwd") == "etcpasswd"
    assert sanitize_log_id("a/b\\c") == "abc"
    assert sanitize_log_id("") == "unknown"


def test_task_log_path_cannot_escape(tmp_path):
    store = LogStore(str(tmp_path))
    evil = "../../../../tmp/evil"
    path = store.task_log_path(evil)
    assert path.parent == store.tasks_dir
    assert path.resolve().parent == store.tasks_dir.resolve()


# ---------------------------------------------------------------------------
# 9. --self-check + post-update health gate
# ---------------------------------------------------------------------------
def _write_worker_env(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "WORKER_CONTROL_PLANE_URL=https://cp.example\n"
        "WORKER_HOST_NAME=test-host\n"
        "WORKER_HOST_TOKEN=tok\n"
        "WORKER_HOST_ID=host-1\n"
        "WORKER_WORK_DIR=" + str(path.parent.parent) + "\n"
    )


def test_self_check_ok(tmp_path):
    env_path = tmp_path / "config" / "worker.env"
    _write_worker_env(env_path)
    cfg = WorkerConfig.load(config_path=str(env_path))
    ok, report = worker_main.run_self_check(cfg)
    assert ok is True
    assert report["checks"]["config"] == "ok"
    assert report["checks"]["state"].startswith("ok")
    assert "docker" in report["checks"]


def test_self_check_bad_config():
    cfg = WorkerConfig(control_plane_url="", host_name="",
                       host_token="", host_id="")
    ok, report = worker_main.run_self_check(cfg)
    assert ok is False
    assert report["checks"]["config"].startswith("FAIL")


def test_main_self_check_exit_codes(tmp_path):
    env_path = tmp_path / "config" / "worker.env"
    _write_worker_env(env_path)
    assert worker_main.main(["--config", str(env_path), "--self-check"]) == 0


def test_write_and_read_status_file(tmp_path):
    p = self_update.write_status_file(str(tmp_path), "1.2.3")
    assert p.name == self_update.STATUS_FILENAME
    marker = self_update.read_status_file(str(tmp_path))
    assert marker["version"] == "1.2.3"
    assert marker["boot_ts"] > 0
    assert self_update.read_status_file(str(tmp_path / "missing")) is None


def test_wait_for_healthy_success(tmp_path):
    import time
    pre_ts = time.time() - 1
    self_update.write_status_file(str(tmp_path), "2.0.0")
    ok = self_update.wait_for_healthy(
        str(tmp_path), "2.0.0", pre_ts, timeout_s=30,
        is_active_fn=lambda: True, sleep_fn=lambda s: None)
    assert ok is True


def test_wait_for_healthy_rejects_stale_marker(tmp_path):
    import time
    self_update.write_status_file(str(tmp_path), "2.0.0")
    # pre_ts AFTER the marker: the marker is from the old run
    ok = self_update.wait_for_healthy(
        str(tmp_path), "2.0.0", time.time() + 3600, timeout_s=1,
        is_active_fn=lambda: True, sleep_fn=lambda s: None)
    assert ok is False


def test_wait_for_healthy_rejects_wrong_version(tmp_path):
    import time
    self_update.write_status_file(str(tmp_path), "1.9.9")
    ok = self_update.wait_for_healthy(
        str(tmp_path), "2.0.0", time.time() - 1, timeout_s=1,
        is_active_fn=lambda: True, sleep_fn=lambda s: None)
    assert ok is False


def test_wait_for_healthy_requires_active_unit(tmp_path):
    import time
    self_update.write_status_file(str(tmp_path), "2.0.0")
    ok = self_update.wait_for_healthy(
        str(tmp_path), "2.0.0", time.time() - 1, timeout_s=1,
        is_active_fn=lambda: False, sleep_fn=lambda s: None)
    assert ok is False
