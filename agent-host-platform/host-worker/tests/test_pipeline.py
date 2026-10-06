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
        self.ps_rows = []        # injected `docker ps` rows for port checks

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
            restart="unless-stopped", timeout=120):
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

    def ps(self, all=False):
        self._record("ps", all)
        return list(self.ps_rows)

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

    # new container was started, then stopped + removed. Its name is unique
    # per deployment attempt (deployment_id suffix), so it never collides
    # with the live container of the same version.
    assert len(docker.run_calls) == 1
    new_name = docker.run_calls[0]["name"]
    assert new_name == "uaht-my-api-2.0.0-depv2"
    assert new_name in docker.stop_calls
    assert new_name in docker.rm_calls
    assert new_name not in docker.containers

    # previous version restored
    assert "uaht-my-api-1.0.0" in docker.start_calls
    assert docker.containers["uaht-my-api-1.0.0"]["running"] is True

    # state recorded. The healthcheck fake fails for the restored
    # previous version too, so per spec §6 the failed deployment persists
    # rollback_failed (never a false "rolled_back").
    new_state = ctx.deployment_store.load("dep-v2")
    assert new_state["status"] == "rollback_failed"
    assert new_state["rollback_status"] == "failed"
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


# ---------------------------------------------------------------------------
# Same-version redeploy: the new container gets a unique name per attempt,
# so the live container of the same version survives until health passes.
# ---------------------------------------------------------------------------
def _seed_same_version_previous(ctx):
    old_name = "uaht-my-api-2.0.0-depv2a"  # an earlier attempt's container
    ctx.deployment_store.save({
        "deployment_id": "dep-v2a",
        "task_id": "task-0",
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "2.0.0",
        "container_name": old_name,
        "image": "img:2.0.0",
        "runtime": "docker",
        "host_port": 18001,
        "container_port": 3000,
        "healthcheck_path": "/health",
        "env": {"NODE_ENV": "production"},
        "status": "running",
        "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })
    return old_name


def test_same_version_redeploy_keeps_old_container_until_health_passes(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    old_name = _seed_same_version_previous(ctx)
    docker.containers[old_name] = {"image": "img:2.0.0", "running": True}

    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    result = pipeline.deploy(ctx, _deploy_task())  # same version 2.0.0, new attempt dep-v2

    new_name = docker.run_calls[0]["name"]
    assert new_name == "uaht-my-api-2.0.0-depv2"
    assert new_name != old_name
    # the live container was never removed (no downtime before health passed)
    assert old_name not in docker.rm_calls
    assert old_name in docker.containers
    # ...then stopped (kept, not removed) once the new one passed health
    assert old_name in docker.stop_calls
    # state.json records the ACTUAL container name (reconcile reads this)
    new_state = ctx.deployment_store.load("dep-v2")
    assert new_state["container_name"] == new_name
    assert result["status"] == "running"


def test_same_version_redeploy_failed_health_keeps_old_running(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    old_name = _seed_same_version_previous(ctx)
    docker.containers[old_name] = {"image": "img:2.0.0", "running": True}

    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: False)

    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task())

    # the old container of the same version was never touched by the failed attempt
    assert old_name not in docker.stop_calls
    assert old_name not in docker.rm_calls
    assert docker.containers[old_name]["running"] is True


# ---------------------------------------------------------------------------
# Port verification: OS-level bind test + docker ps published-port scan,
# run before every `docker run`. Collisions fail the task with a clear error.
# ---------------------------------------------------------------------------
def test_published_host_ports_parses_ps_cells():
    assert pipeline.published_host_ports("0.0.0.0:8080->3000/tcp") == [8080]
    assert pipeline.published_host_ports("0.0.0.0:8080->3000/tcp, :::8080->3000/tcp") == [8080, 8080]
    assert pipeline.published_host_ports(":::9090->90/tcp") == [9090]
    assert pipeline.published_host_ports("") == []
    assert pipeline.published_host_ports(None) == []


def test_verify_host_port_free_passes_on_free_port(tmp_path):
    import socket as _socket
    docker = FakeDockerClient()
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    # socket closed again -> port free
    pipeline.verify_host_port_free(docker, free)  # must not raise


def test_verify_host_port_free_fails_on_os_bind_collision():
    import socket as _socket
    docker = FakeDockerClient()
    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    taken = holder.getsockname()[1]
    try:
        with pytest.raises(pipeline.DeployError, match="already in use"):
            pipeline.verify_host_port_free(docker, taken)
    finally:
        holder.close()


def test_verify_host_port_free_fails_on_docker_published_port(tmp_path):
    import socket as _socket
    docker = FakeDockerClient()
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    docker.ps_rows = [{"Names": "/some-other-app", "Ports": f"0.0.0.0:{free}->3000/tcp"}]
    with pytest.raises(pipeline.DeployError, match="already published"):
        pipeline.verify_host_port_free(docker, free)


def _grab_free_port():
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_requested_host_port_used_after_verification(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    wanted = _grab_free_port()
    result = pipeline.deploy(ctx, _deploy_task(requested_host_port=wanted))
    assert docker.run_calls[0]["ports"] == {wanted: 3000}
    assert result["ports"] == {str(wanted): 3000}


def test_requested_host_port_collision_fails_before_docker_run(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    wanted = _grab_free_port()
    docker.ps_rows = [{"Names": "/some-other-app",
                       "Ports": f"0.0.0.0:{wanted}->3000/tcp"}]
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    with pytest.raises(pipeline.DeployError, match="already published"):
        pipeline.deploy(ctx, _deploy_task(requested_host_port=wanted))
    # no container was ever started for the colliding port
    assert docker.run_calls == []


def test_automatic_rollback_unknown_target_health_is_failed_not_success(
    tmp_path, monkeypatch
):
    """Regression (W2 follow-up): the automatic-rollback path in
    pipeline.deploy() must apply the same rule as the explicit path —
    success ONLY when the target health check positively verifies
    "healthy". An "unknown" target (e.g. no health-checkable endpoint)
    must persist rollback_failed with "target health could not be
    verified" (never a false "rolled_back"), and port settlement must be
    skipped — settling the registry for an unverified target would bless a
    broken promotion."""
    docker = FakeDockerClient()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                             "running": True}
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    _seed_previous(ctx)

    # The new version fails its healthcheck -> automatic rollback runs.
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: False)
    # The restored target exposes no health-checkable endpoint: its health
    # comes back UNKNOWN rather than healthy/unhealthy.
    monkeypatch.setattr(
        pipeline.rollback_mod,
        "perform_rollback",
        lambda *a, **k: {"rolled_back_to": "dep-v1",
                         "target_health_status": "unknown",
                         "restored_via": "started"},
    )
    settle_calls = []

    def _spy_settle(*a, **k):
        settle_calls.append((a, k))
        return {"settled": True, "reason": None}

    monkeypatch.setattr(pipeline.rollback_mod, "settle_rollback_ports",
                        _spy_settle)

    with pytest.raises(pipeline.DeployError, match="could not be verified"):
        pipeline.deploy(ctx, _deploy_task())

    # Port settlement must NOT run for a target whose health is unverified.
    assert settle_calls == []

    new_state = ctx.deployment_store.load("dep-v2")
    assert new_state["status"] == "rollback_failed"
    assert new_state["rollback_status"] == "failed"
    assert "could not be verified" in new_state["rollback_error"]
    assert new_state["rollback_of"] == "dep-v1"
    # The skipped settle is recorded honestly on the row for
    # reconciliation/retry.
    assert new_state["port_settle"]["settled"] is False
    assert (new_state["port_settle"]["reason"]
            == "settle skipped: target health not verified")
