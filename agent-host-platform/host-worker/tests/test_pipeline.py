"""Deploy pipeline tests with a FAKE docker client.

The fake is a test double implementing the DockerClient interface,
injected through the WorkerContext — the real docker module is never
stubbed or monkeypatched. Scenarios:

  * healthcheck failure -> new container removed, previous version restored
  * artifact checksum mismatch -> deploy aborts before ANY docker call
  * decide_rollback pure decision logic
"""
import hashlib
import time
from pathlib import Path

import pytest

from deployments import pipeline
from deployments.state import DeploymentStore
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes (dependency injection, not stubs of the real modules)
# ---------------------------------------------------------------------------
class FakeDockerClient:
    """In-memory stand-in for docker.client.DockerClient."""

    def __init__(self):
        self.calls = []          # every method call, in order
        self.containers = {}     # name -> {"image":..., "running": bool}
        self.build_calls = []
        self.run_calls = []
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []

    def _record(self, name, *args):
        self.calls.append((name, args))

    # -- interface used by the pipeline ------------------------------------
    def version(self):
        self._record("version")
        return "99.0-fake"

    def compose_available(self):
        self._record("compose_available")
        return True

    def build(self, context_dir, dockerfile, tag, build_args=None, timeout=1200):
        self._record("build", tag)
        self.build_calls.append(tag)
        return "fake build output"

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", extra_args=None, timeout=120):
        self._record("run", name)
        self.run_calls.append({"name": name, "image": image, "ports": ports})
        self.containers[name] = {"image": image, "running": True,
                                 "ports": ports or {}}
        return "fake-container-id-" + name

    def start(self, name, timeout=120):
        self._record("start", name)
        self.start_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = True

    def stop(self, name, timeout_secs=10, timeout=120):
        self._record("stop", name)
        self.stop_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self._record("rm", name)
        self.rm_calls.append(name)
        self.containers.pop(name, None)

    def container_exists(self, name):
        self._record("container_exists", name)
        return name in self.containers

    def logs(self, name, tail=500):
        self._record("logs", name)
        return "fake logs\n"

    def inspect(self, name):
        self._record("inspect", name)
        c = self.containers.get(name, {})
        return [{"Config": {"Image": c.get("image")},
                 "State": {"Status": "running" if c.get("running") else "exited"},
                 "NetworkSettings": {"Ports": {}}}]


class FakeAPI:
    """Serves artifact bytes for download_artifact."""

    def __init__(self, artifact_bytes: bytes):
        self.artifact_bytes = artifact_bytes
        self.downloads = []

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        self.downloads.append(artifact_id)
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(self.artifact_bytes)
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


def _deploy_task(**overrides):
    payload = {
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "2.0.0",
        "deployment_id": "dep-v2",
        "image": "img:2.0.0",  # prebuilt: no artifact download, no build
        "manifest": {
            "name": "my-api",
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {"memory": "256m", "cpu": 1},
            "restart": "unless-stopped",
            "env": {"NODE_ENV": "production"},
        },
        "secrets": {"API_KEY": "s3cr3t-value"},
        "healthcheck_timeout": 3,
    }
    payload.update(overrides)
    return {"id": "task-1", "type": "deploy", "payload": payload}


def _seed_previous(ctx):
    ctx.deployment_store.save({
        "deployment_id": "dep-v1",
        "task_id": "task-0",
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "1.0.0",
        "container_name": "uaht-my-api-1.0.0",
        "image": "img:1.0.0",
        "runtime": "docker",
        "host_port": 8001,
        "container_port": 3000,
        "healthcheck_path": "/health",
        "env": {"NODE_ENV": "production"},
        "status": "running",
        "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_healthcheck_failure_restores_previous_version(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                             "running": True}
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    _seed_previous(ctx)

    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: False)

    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task())

    # new container was started, then stopped + removed
    assert len(docker.run_calls) == 1
    new_name = docker.run_calls[0]["name"]
    assert new_name == "uaht-my-api-2.0.0"
    assert new_name in docker.stop_calls
    assert new_name in docker.rm_calls
    assert new_name not in docker.containers

    # previous version restored
    assert "uaht-my-api-1.0.0" in docker.start_calls
    assert docker.containers["uaht-my-api-1.0.0"]["running"] is True

    # state recorded
    new_state = ctx.deployment_store.load("dep-v2")
    assert new_state["status"] == "rolled_back"
    assert new_state["rollback_of"] == "dep-v1"
    prev_state = ctx.deployment_store.load("dep-v1")
    assert prev_state["status"] == "running"


def test_healthcheck_failure_without_previous_fails_clean(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))

    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: False)

    with pytest.raises(pipeline.DeployError, match="no previous healthy"):
        pipeline.deploy(ctx, _deploy_task())

    new_state = ctx.deployment_store.load("dep-v2")
    assert new_state["status"] == "failed"
    assert new_state["rollback_of"] is None


def test_checksum_mismatch_aborts_before_docker(tmp_path):
    good = b"correct artifact bytes"
    expected = "sha256:" + hashlib.sha256(good).hexdigest()
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b"tampered bytes"))

    task = _deploy_task(artifact_id="art-1",
                        artifact_checksum=expected,
                        artifact_size=len(good))
    del task["payload"]["image"]  # force the artifact path

    with pytest.raises(pipeline.DeployError, match="checksum mismatch"):
        pipeline.deploy(ctx, task)

    # zero docker interaction
    assert docker.calls == []
    assert docker.build_calls == []
    assert docker.run_calls == []

    # artifact quarantined, original removed from the artifacts dir
    quarantine = Path(ctx.config.work_dir) / "quarantine"
    assert any(quarantine.iterdir())
    assert not list((Path(ctx.config.work_dir) / "artifacts").iterdir())


def test_checksum_mismatch_mentions_quarantine(tmp_path):
    expected = "sha256:" + hashlib.sha256(b"good").hexdigest()
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI(b"bad"))
    task = _deploy_task(artifact_id="art-9", artifact_checksum=expected)
    del task["payload"]["image"]
    with pytest.raises(pipeline.DeployError, match="quarantined"):
        pipeline.deploy(ctx, task)


def test_missing_artifact_checksum_refuses(tmp_path):
    ctx = FakeCtx(tmp_path, FakeDockerClient(), FakeAPI(b"data"))
    task = _deploy_task(artifact_id="art-1")
    del task["payload"]["image"]
    with pytest.raises(pipeline.DeployError, match="no artifact_checksum"):
        pipeline.deploy(ctx, task)


@pytest.mark.parametrize("healthy,previous,expected", [
    (True, {"container_name": "old"}, "keep_new"),
    (True, None, "keep_new"),
    (False, {"container_name": "old"}, "rollback_to_previous"),
    (False, None, "fail_clean"),
    (False, {}, "fail_clean"),
])
def test_decide_rollback(healthy, previous, expected):
    assert pipeline.decide_rollback(healthy, previous) == expected


def test_verify_checksum(tmp_path):
    data = b"hello worker"
    path = tmp_path / "a.bin"
    path.write_bytes(data)
    good = "sha256:" + hashlib.sha256(data).hexdigest()
    assert pipeline.verify_checksum(str(path), good) is True
    assert pipeline.verify_checksum(str(path), "sha256:" + "0" * 64) is False
    with pytest.raises(pipeline.DeployError):
        pipeline.verify_checksum(str(path), "md5:abc")


def test_successful_deploy_marks_running(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                             "running": True}
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    _seed_previous(ctx)

    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    result = pipeline.deploy(ctx, _deploy_task())
    assert result["status"] == "running"
    assert result["health_status"] == "healthy"
    assert result["deployment_id"] == "dep-v2"

    # previous container stopped but kept for rollback
    assert "uaht-my-api-1.0.0" in docker.stop_calls
    assert "uaht-my-api-1.0.0" in docker.containers
    prev_state = ctx.deployment_store.load("dep-v1")
    assert prev_state["status"] == "superseded"

    # secrets never persisted to the state file
    new_state = ctx.deployment_store.load("dep-v2")
    assert "API_KEY" not in str(new_state.get("env"))
