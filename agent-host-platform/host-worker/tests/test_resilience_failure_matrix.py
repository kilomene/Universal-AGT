"""WS-I §69 — failure-injection matrix: every failure lands in a known
state, and nothing hangs indefinitely.

Each test injects ONE fault into the real deploy path (real dispatcher,
real pipeline, real health checker, real client over real HTTP) and
asserts the terminal state:

  * artifact download timeout ......... task failed, no .part left behind
  * artifact checksum mismatch ........ task failed, bytes quarantined,
                                        zero docker interaction
  * docker build failure .............. task failed, no container
  * docker run failure ................ task failed, no partial container,
                                        port reservation released
  * healthcheck failure (previous) .... rolled back, previous restored +
                                        serving, failed container removed
  * healthcheck failure (no previous) .. task failed, failed container
                                        removed (fails clean)
  * invalid manifest .................. task failed before any docker call
  * resource exhaustion ............... task failed at admission, no
                                        docker interaction
  * port exhaustion ................... task failed, no container,
                                        reservation released
  * ingress sync failure .............. ingress-sync task failed honestly
                                        (Cloudflare-side errors surface,
                                        never hang)
  * progress-report timeout ........... dispatcher returns locally-failed
                                        (a value, never a raise); the task
                                        stays in-flight on the plane
  * claim timeout ..................... single typed WorkerAPIError

Covered elsewhere (not repeated): task lease expiration
(test_recovery_host.py, control-plane/api/test/recoveryLeases.test.ts),
worker crash (§54 file), CP restart (§53 file), network partitions (§52
file), concurrent claims (test_recovery_controlplane.py).

What is REAL: everything except the injected fault itself and the two
sanctioned doubles (RecoveryPlane for the control plane,
ThreadDockerClient for the Docker daemon). Faults are injected through
the sanctioned seams: plane latency/failure hooks, docker-client
subclasses that raise at one method, and monkeypatched capacity.
"""
from __future__ import annotations

import io
import json
import tarfile
import time

import pytest
import requests

from agent import api as api_mod
from agent.api import ControlPlaneClient, WorkerAPIError
from deployments import pipeline as pipeline_mod
from docker.client import DockerError
from executor.dispatcher import TaskDispatcher

from fake_thread_docker import ThreadDockerClient
from recovery_harness import (
    RecoveryPlane,
    http_get_text,
    make_ctx,
)


@pytest.fixture()
def plane():
    plane = RecoveryPlane().start()
    yield plane
    plane.shutdown()


@pytest.fixture()
def docker():
    docker = ThreadDockerClient()
    yield docker
    docker.shutdown_all()


def _register_agent(plane: RecoveryPlane, name: str):
    resp = requests.post(f"{plane.url}/v1/agents/register",
                         json={"name": name, "type": "Muse"}, timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["agent"], body["api_key"]


def _register_host(plane: RecoveryPlane, name: str = "ws6-matrix-host"):
    resp = requests.post(
        f"{plane.url}/v1/hosts/register",
        json={"name": name, "capabilities": ["docker"],
              "worker_version": "test"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["host"], body["host_token"]


def _manifest(app: str, **overrides) -> dict:
    m = {
        "name": app,
        "runtime": "docker",
        "service": {"port": 3000, "healthcheck": "/health"},
        "resources": {},
        "restart": "unless-stopped",
        "env": {"APP_NAME": app},
    }
    m.update(overrides)
    return m


def _deploy_payload(deployment_id: str, app: str, version: str = "1.0.0",
                    **overrides) -> dict:
    payload = {
        "project_id": f"proj-{app}",
        "project_name": app,
        "version": version,
        "deployment_id": deployment_id,
        "image": f"img:{app}-{version}",
        "manifest": _manifest(app),
        "healthcheck_timeout": 10,
    }
    payload.update(overrides)
    return payload


def _make_artifact(app: str, manifest_overrides: dict | None = None) -> bytes:
    manifest = _manifest(app, **(manifest_overrides or {}))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for arcname, data in (
                ("Dockerfile", b"FROM scratch\n"),
                ("agent.deploy.json",
                 json.dumps(manifest).encode())):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _create_deployment(plane: RecoveryPlane, api_key: str, app: str,
                       with_artifact: bool = False,
                       manifest_overrides: dict | None = None,
                       version: str = "1.0.0",
                       **payload_overrides) -> tuple[dict, dict]:
    """Create a deployment; optionally upload an artifact and wire the
    artifact fields the production control plane injects."""
    artifact = None
    if with_artifact:
        data = _make_artifact(app, manifest_overrides)
        up = requests.post(
            f"{plane.url}/v1/test/artifacts", data=data,
            headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        assert up.status_code == 201, up.text
        artifact = up.json()["artifact"]
    resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"project_id": f"proj-{app}", "project_name": app,
              "version": version},
        timeout=5)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    deployment, task = data["deployment"], data["task"]
    fields = dict(payload_overrides)
    if with_artifact:
        # No payload manifest/image: the worker takes the full artifact
        # path (download -> verify -> extract -> validate).
        fields.update({
            "artifact_id": artifact["id"],
            "artifact_checksum": artifact["checksum"],
            "artifact_size": artifact["size"],
        })
        fields.pop("manifest", None)
        fields.pop("image", None)
    with plane.lock:
        plane.tasks[task["id"]]["payload"].update(
            _deploy_payload(deployment["id"], app, version, **fields))
        if with_artifact:
            # _deploy_payload re-added manifest/image; drop them again.
            plane.tasks[task["id"]]["payload"].pop("manifest", None)
            plane.tasks[task["id"]]["payload"].pop("image", None)
    return deployment, task


def _claim_and_dispatch(plane, api, host_id, ctx, docker, task_id,
                        timeout: float = 120) -> dict:
    claimed = api.claim_task(host_id, ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id
    return TaskDispatcher(ctx, api).dispatch(claimed)


# ---------------------------------------------------------------------------
# artifact download timeout: failed, no .part left behind
# ---------------------------------------------------------------------------

def test_matrix_artifact_download_timeout(plane, docker, tmp_path,
                                         monkeypatch):
    """§69: the artifact download stalls -> the client times out with a
    typed error, no .part file is left behind, the task fails clean."""
    monkeypatch.setattr(api_mod, "REQUEST_TIMEOUT", 1)
    # Every artifact download stalls 3s (> the 1s client timeout).
    plane.inject_latency("/v1/artifacts/", seconds=3, times=99)

    _, api_key = _register_agent(plane, f"ws6-a-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, "dl-tmo",
                                          with_artifact=True)

    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task["id"])

    assert outcome["status"] == "failed"
    assert "transport error" in outcome["error"] or "timed out" in \
        outcome["error"]
    assert plane.tasks[task["id"]]["status"] == "failed"
    # No partial file: neither the promoted dest nor the .part remains.
    artifacts_dir = tmp_path / "artifacts"
    leftovers = list(artifacts_dir.iterdir()) if artifacts_dir.exists() \
        else []
    assert leftovers == [], f"partial download left behind: {leftovers}"
    assert docker.run_calls == []


# ---------------------------------------------------------------------------
# checksum mismatch: failed, quarantined, zero docker interaction
# ---------------------------------------------------------------------------

def test_matrix_checksum_mismatch_quarantines(plane, docker, tmp_path):
    """§69: tampered artifact bytes -> checksum gate fails BEFORE any
    docker call; the bytes are quarantined; the task fails."""
    _, api_key = _register_agent(plane, f"ws6-c-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, "dl-sum",
                                          with_artifact=True)
    # Tamper with the EXPECTED checksum (bytes on the plane are honest).
    with plane.lock:
        plane.tasks[task["id"]]["payload"]["artifact_checksum"] = \
            "sha256:" + "0" * 64

    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task["id"])

    assert outcome["status"] == "failed"
    assert "checksum mismatch" in outcome["error"]
    assert plane.tasks[task["id"]]["status"] == "failed"
    assert docker.run_calls == [] and docker.images == set()
    quarantine = tmp_path / "quarantine"
    assert quarantine.is_dir() and any(quarantine.iterdir()), \
        "tampered bytes were not quarantined"


# ---------------------------------------------------------------------------
# docker build failure / docker run failure
# ---------------------------------------------------------------------------

class _BuildFailDocker(ThreadDockerClient):
    def build(self, context_dir, dockerfile, tag, build_args=None,
              timeout=1200):
        raise DockerError(["docker", "build", "-t", tag], 1,
                          "injected build error")


class _RunFailDocker(ThreadDockerClient):
    def run(self, name, image, ports=None, env=None, memory=None,
            cpus=None, restart="unless-stopped", timeout=120):
        raise DockerError(["docker", "run", name], 125,
                          "injected start error")


def test_matrix_docker_build_failure(plane, tmp_path):
    """§69: the image build fails -> the task fails with the build error;
    no container is ever created, and the §3 resource reservation is
    released (a build failure must not leak admission capacity for the
    life of the worker process)."""
    docker = _BuildFailDocker()
    try:
        _, api_key = _register_agent(plane, f"ws6-b-{time.monotonic_ns()}")
        host, host_token = _register_host(plane)
        # The manifest requests memory so the §3 admission reservation is
        # taken BEFORE the build fails — the leak assertion below is only
        # meaningful when a reservation existed.
        deployment, task = _create_deployment(
            plane, api_key, "build-fail", with_artifact=True,
            manifest_overrides={"resources": {"memory": "256m"}})
        api = ControlPlaneClient(plane.url, host_token)
        ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                       host_token=host_token)
        outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                      task["id"])
        assert outcome["status"] == "failed"
        assert "injected build error" in outcome["error"]
        assert plane.tasks[task["id"]]["status"] == "failed"
        assert docker.containers == {}
        assert ctx.deployment_store.load(deployment["id"]) is None or \
            ctx.deployment_store.load(deployment["id"])["status"] != "running"
        # The §3 admission reservation must not leak on the build-failure
        # path (spec §11: §§4-5 run under a try/except that releases it).
        assert pipeline_mod.reserved_resources_snapshot() == {}
        assert pipeline_mod.reserved_port_snapshot() == set()
    finally:
        docker.shutdown_all()


def test_matrix_docker_run_failure(plane, docker, tmp_path):
    """§69: the container fails to start -> the task fails; no partial
    container remains and the port reservation is released."""
    flaky = _RunFailDocker()
    try:
        _, api_key = _register_agent(plane, f"ws6-r-{time.monotonic_ns()}")
        host, host_token = _register_host(plane)
        deployment, task = _create_deployment(plane, api_key, "run-fail")
        api = ControlPlaneClient(plane.url, host_token)
        ctx = make_ctx(str(tmp_path), api, flaky, host_id=host["id"],
                       host_token=host_token)
        outcome = _claim_and_dispatch(plane, api, host["id"], ctx, flaky,
                                      task["id"])
        assert outcome["status"] == "failed"
        assert "injected start error" in outcome["error"]
        assert plane.tasks[task["id"]]["status"] == "failed"
        assert flaky.containers == {}
        assert pipeline_mod.reserved_port_snapshot() == set()
        assert pipeline_mod.reserved_resources_snapshot() == {}
    finally:
        flaky.shutdown_all()


# ---------------------------------------------------------------------------
# healthcheck failure: rollback with previous / fail clean without
# ---------------------------------------------------------------------------

def _deploy_ok(plane, api, host_id, ctx, docker, app, **overrides):
    _, api_key = _register_agent(plane, f"ws6-ok-{app}-{time.monotonic_ns()}")
    deployment, task = _create_deployment(plane, api_key, app, **overrides)
    outcome = _claim_and_dispatch(plane, api, host_id, ctx, docker,
                                  task["id"])
    assert outcome["status"] == "completed", outcome.get("error")
    return deployment


def test_matrix_healthcheck_failure_rolls_back(plane, docker, tmp_path):
    """§69: the new container never passes health -> it is stopped and
    removed, the previous healthy deployment is restored and keeps
    serving; the failed deployment is recorded as rolled_back."""
    _, api_key = _register_agent(plane, f"ws6-h-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)

    prev = _deploy_ok(plane, api, host["id"], ctx, docker, "rb-app")
    prev_state = ctx.deployment_store.load(prev["id"])
    prev_port = prev_state["host_port"]
    assert http_get_text(
        f"http://127.0.0.1:{prev_port}/health") == "ok"

    # Same project, NEW VERSION whose container is sick (the faithful
    # rollback scenario: v1 healthy, v2 fails health).
    _, key2 = _register_agent(plane, f"ws6-h2-{time.monotonic_ns()}")
    dep2, task2 = _create_deployment(plane, key2, "rb-app", version="2.0.0")
    with plane.lock:
        plane.tasks[task2["id"]]["payload"]["manifest"]["env"][
            "HEALTH_FAIL"] = "1"
        plane.tasks[task2["id"]]["payload"]["healthcheck_timeout"] = 3
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task2["id"])

    assert outcome["status"] == "failed"
    assert "healthcheck failed" in outcome["error"]
    assert "rolled back" in outcome["error"]
    assert plane.tasks[task2["id"]]["status"] == "failed"

    # The failed container is gone; the previous one is back and serving.
    new_state = ctx.deployment_store.load(dep2["id"])
    assert new_state["status"] == "rolled_back"
    assert new_state["rollback_of"] == prev["id"]
    restored = ctx.deployment_store.load(prev["id"])
    assert restored["status"] == "running"
    assert len(docker.containers) == 1
    assert http_get_text(
        f"http://127.0.0.1:{prev_port}/health") == "ok"


def test_matrix_healthcheck_failure_no_previous_fails_clean(
        plane, docker, tmp_path):
    """§69: healthcheck fails with no previous deployment -> the failed
    container is stopped + removed (nothing half-running is left) and the
    deployment is recorded as failed."""
    _, api_key = _register_agent(plane, f"ws6-hc-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, "lonely-app")
    with plane.lock:
        plane.tasks[task["id"]]["payload"]["manifest"]["env"][
            "HEALTH_FAIL"] = "1"
        plane.tasks[task["id"]]["payload"]["healthcheck_timeout"] = 3

    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task["id"])

    assert outcome["status"] == "failed"
    assert "no previous healthy deployment" in outcome["error"]
    assert docker.containers == {}  # the failed container was removed
    state = ctx.deployment_store.load(deployment["id"])
    assert state is not None and state["status"] == "failed"


# ---------------------------------------------------------------------------
# invalid manifest / resource exhaustion / port exhaustion
# ---------------------------------------------------------------------------

def test_matrix_invalid_manifest_rejected_before_docker(
        plane, docker, tmp_path):
    """§69: a manifest that fails validation -> DeployError before any
    docker interaction; the task fails with the validation errors."""
    _, api_key = _register_agent(plane, f"ws6-m-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, "bad-manifest")
    with plane.lock:
        plane.tasks[task["id"]]["payload"]["manifest"] = {
            "name": "bad-manifest", "runtime": "bogus-runtime"}

    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task["id"])

    assert outcome["status"] == "failed"
    assert "manifest validation failed" in outcome["error"]
    assert plane.tasks[task["id"]]["status"] == "failed"
    assert docker.run_calls == [] and docker.images == set()


def test_matrix_resource_exhaustion_refused_at_admission(
        plane, docker, tmp_path):
    """§69: a deployment requesting more memory than the host has is
    refused at admission — before any docker interaction — with a clear
    error."""
    _, api_key = _register_agent(plane, f"ws6-x-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, "huge-app")
    with plane.lock:
        plane.tasks[task["id"]]["payload"]["manifest"]["resources"] = \
            {"memory": "999999g"}

    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task["id"])

    assert outcome["status"] == "failed"
    assert "insufficient reservable memory" in outcome["error"]
    assert plane.tasks[task["id"]]["status"] == "failed"
    assert docker.run_calls == []
    assert pipeline_mod.reserved_resources_snapshot() == {}


def test_matrix_port_exhaustion_fails_clean(plane, docker, tmp_path,
                                           monkeypatch):
    """§69: no free host port available -> the task fails with a clear
    error; no container is created and the (never-taken) reservation is
    not leaked."""
    def _no_ports(used, log=None):
        raise pipeline_mod.DeployError(
            "could not find a free host port after 20 attempts")

    monkeypatch.setattr(pipeline_mod, "pick_free_port", _no_ports)

    _, api_key = _register_agent(plane, f"ws6-p-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, "noport-app")

    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = _claim_and_dispatch(plane, api, host["id"], ctx, docker,
                                  task["id"])

    assert outcome["status"] == "failed"
    assert "free host port" in outcome["error"]
    assert plane.tasks[task["id"]]["status"] == "failed"
    assert docker.containers == {}
    assert pipeline_mod.reserved_port_snapshot() == set()


# ---------------------------------------------------------------------------
# ingress sync failure: honest task failure, never a hang
# ---------------------------------------------------------------------------

class _FailingIngressProvider:
    name = "fake-cloudflare"
    enabled = True

    def sync_routes(self, routes):
        raise RuntimeError("cloudflare API unreachable (injected)")


def test_matrix_ingress_sync_failure_is_honest(plane, docker, tmp_path,
                                              monkeypatch):
    """§69: the Cloudflare-side route sync fails -> the ingress-sync task
    is reported failed with the error (honest), not stuck or successful."""
    _, api_key = _register_agent(plane, f"ws6-g-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)

    task_resp = requests.post(
        f"{plane.url}/v1/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"type": "ingress-sync", "payload": {}}, timeout=5)
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["task"]["id"]

    api = ControlPlaneClient(plane.url, host_token)
    monkeypatch.setattr(api, "list_host_domains",
                        lambda: {"domains": []})
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    ctx.ingress = _FailingIngressProvider()

    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)

    assert outcome["status"] == "failed"
    assert "cloudflare API unreachable" in outcome["error"]
    assert plane.tasks[task_id]["status"] == "failed"


# ---------------------------------------------------------------------------
# progress-report timeout: local failure value, never a raise, no hang
# ---------------------------------------------------------------------------

def test_matrix_progress_timeout_returns_local_failure(
        plane, docker, tmp_path, monkeypatch):
    """§69: every progress report times out -> the dispatcher still
    returns a local failure dict (it never raises out of the worker);
    the task stays in-flight on the plane — a known state, not a hang."""
    monkeypatch.setattr(api_mod, "REQUEST_TIMEOUT", 1)
    # Progress calls stall 3s (> the 1s client timeout). The claim
    # long-poll itself allows wait+10s, so claiming still works.
    plane.inject_latency("/v1/worker/tasks/", seconds=3, times=99)

    _, api_key = _register_agent(plane, f"ws6-t-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    task_resp = requests.post(
        f"{plane.url}/v1/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"type": "system-info", "payload": {}}, timeout=5)
    task_id = task_resp.json()["task"]["id"]

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=1)
    assert claimed is not None and claimed["id"] == task_id

    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    # Must not raise, must not hang: bounded by the client timeout.
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "failed"
    # The plane never saw a report: the task is still in-flight there
    # (the lease/sweeper path owns recovery from here).
    assert plane.tasks[task_id]["status"] == "claimed"


def test_matrix_claim_timeout_is_typed_error(plane, monkeypatch):
    """§69: the claim long-poll stalls past the client timeout -> a single
    typed WorkerAPIError naming the action (not a raw requests error)."""
    monkeypatch.setattr(api_mod, "CLAIM_TIMEOUT_PAD", 0)
    plane.inject_latency("/v1/worker/tasks/claim", seconds=3, times=1)

    host, host_token = _register_host(plane)
    api = ControlPlaneClient(plane.url, host_token)
    with pytest.raises(WorkerAPIError) as excinfo:
        api.claim_task(host["id"], ["docker"], wait=1)
    assert "task claim" in str(excinfo.value)
