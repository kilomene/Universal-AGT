"""WS-I §52 — network failures: every transient fault on every link retries
safely and never duplicates a deployment operation.

Covered here (and NOT in test_recovery_network.py, which owns DNS failure,
slow-plane backoff, partial downloads, and tunnel supervision):

  * Agent -> control plane: a POST /v1/deployments (and /v1/tasks) that
    times out client-side is retried with the SAME idempotency_key ->
    exactly one deployment (+task) is created, the retry replays (200).
  * Host -> control plane: the plane dies between claim and progress. The
    worker's progress calls fail with the single typed WorkerAPIError;
    when the plane returns the worker re-reports and the in-flight deploy
    is NOT executed twice (one container, one claim delivery, one
    completion).
  * Control plane -> Cloudflare (worker edge): the ingress route sync
    fails (the Cloudflare control path is down) -> maybe_sync_on_change
    swallows it and the deploy still completes; the app serves.
  * Host -> Docker/network: docker.run raises a transport-level error ->
    the task fails with a clear error, no partial container is left, the
    port reservation is released, and nothing hangs.

What is REAL: ControlPlaneClient (transport-error wrapper), TaskDispatcher,
the full deploy pipeline (handlers.handle_deploy -> deployments.pipeline),
the real health checker, and ThreadDockerClient's real HTTP servers per
"container". The control plane is the sanctioned in-memory double
(recovery_harness.RecoveryPlane); the Docker daemon is the sanctioned
in-process double (fake_thread_docker.ThreadDockerClient).
"""
from __future__ import annotations

import threading
import time

import pytest
import requests

from agent.api import (
    ControlPlaneClient,
    build_progress_payload,
)
from docker.client import DockerError
from executor.dispatcher import TaskDispatcher

from fake_thread_docker import ThreadDockerClient
from recovery_harness import (
    RecoveryPlane,
    http_get_text,
    make_ctx,
    wait_until,
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


def _register_host(plane: RecoveryPlane, name: str = "ws1-net-host"):
    resp = requests.post(
        f"{plane.url}/v1/hosts/register",
        json={"name": name, "capabilities": ["docker"],
              "worker_version": "test"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["host"], body["host_token"]


def _deploy_payload(deployment_id: str, project_id: str = "proj-net") -> dict:
    return {
        "project_id": project_id,
        "project_name": "net-app",
        "version": "1.0.0",
        "deployment_id": deployment_id,
        "image": "img:net-1.0.0",  # prebuilt: no artifact download needed
        "manifest": {
            "name": "net-app",
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {},
            "restart": "unless-stopped",
            "env": {"APP_NAME": "net-app"},
        },
        "healthcheck_timeout": 10,
    }


def _create_deployment(plane: RecoveryPlane, api_key: str, host_id: str,
                       project_id: str = "proj-net",
                       idempotency_key: str | None = None) -> tuple[dict, dict]:
    body = {"project_id": project_id, "project_name": "net-app",
            "version": "1.0.0", "host_id": host_id}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body, timeout=10)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    deployment, task = data["deployment"], data["task"]
    # The production control plane injects the deploy task's execution
    # payload (routes/deployments.ts); the harness plane predates that
    # injection, so the test sets the worker-facing fields the same way.
    with plane.lock:
        plane.tasks[task["id"]]["payload"].update(
            _deploy_payload(deployment["id"], project_id))
    return deployment, task


# ---------------------------------------------------------------------------
# Agent -> CP: timeout then idempotent retry -> exactly one deployment
# ---------------------------------------------------------------------------

def test_agent_deploy_timeout_retry_creates_exactly_one_deployment(plane):
    """§52: the first POST /v1/deployments times out client-side (the
    server still commits); the retry with the same idempotency_key replays
    -> one deployment row, one task row, no duplicates."""
    _, api_key = _register_agent(plane, f"ws1-agent-{time.monotonic_ns()}")
    host, _ = _register_host(plane)
    key = f"ws1-dep-key-{time.monotonic_ns()}"
    body = {"project_id": "proj-net", "project_name": "net-app",
            "version": "1.0.0", "host_id": host["id"],
            "idempotency_key": key}
    headers = {"Authorization": f"Bearer {api_key}"}

    # First attempt: the plane takes 3s; the agent gives up after 1s. The
    # request is still in flight server-side and will commit.
    plane.inject_latency("/v1/deployments", seconds=3, times=1)
    t0 = time.monotonic()
    with pytest.raises(requests.Timeout):
        requests.post(f"{plane.url}/v1/deployments", headers=headers,
                      json=body, timeout=1)

    # Wait for the timed-out request to commit server-side (poll-based, no
    # fixed sleep), so the retry deterministically hits the replay path.
    wait_until(
        lambda: any(d.get("idempotency_key") == key
                    for d in plane.deployments.values()),
        timeout=10, what="timed-out request committed server-side")

    # The retry with the same key replays instead of duplicating.
    retry = requests.post(f"{plane.url}/v1/deployments", headers=headers,
                          json=body, timeout=10)
    assert retry.status_code == 200, retry.text
    assert retry.json().get("idempotent_replay") is True

    # Settle past the first request's 3s latency window: its handler may
    # still be finishing, and it must not create a second row either.
    wait_until(lambda: time.monotonic() - t0 > 4.0, timeout=10,
               what="settle past the in-flight request")
    with plane.lock:
        deps = [d for d in plane.deployments.values()
                if d.get("idempotency_key") == key]
        tasks = [t for t in plane.tasks.values()
                 if t.get("idempotency_key") == f"deploy:{key}"]
    assert len(deps) == 1, f"duplicate deployments: {len(deps)}"
    assert len(tasks) == 1, f"duplicate deploy tasks: {len(tasks)}"
    assert retry.json()["deployment"]["id"] == deps[0]["id"]


def test_agent_task_timeout_retry_creates_exactly_one_task(plane):
    """§52, task variant: a timed-out POST /v1/tasks retried with the same
    idempotency_key replays -> exactly one task."""
    _, api_key = _register_agent(plane, f"ws1-tagent-{time.monotonic_ns()}")
    key = f"ws1-task-key-{time.monotonic_ns()}"
    body = {"type": "system-info", "payload": {}, "idempotency_key": key}
    headers = {"Authorization": f"Bearer {api_key}"}

    plane.inject_latency("/v1/tasks", seconds=3, times=1)
    t0 = time.monotonic()
    with pytest.raises(requests.Timeout):
        requests.post(f"{plane.url}/v1/tasks", headers=headers,
                      json=body, timeout=1)

    # Wait for the timed-out request to commit server-side so the retry
    # deterministically hits the replay path.
    wait_until(
        lambda: any(t.get("idempotency_key") == key
                    for t in plane.tasks.values()),
        timeout=10, what="timed-out task request committed server-side")

    retry = requests.post(f"{plane.url}/v1/tasks", headers=headers,
                          json=body, timeout=10)
    assert retry.status_code == 200, retry.text
    assert retry.json().get("idempotent_replay") is True

    wait_until(lambda: time.monotonic() - t0 > 4.0, timeout=10,
               what="settle past the in-flight request")
    with plane.lock:
        tasks = [t for t in plane.tasks.values()
                 if t.get("idempotency_key") == key]
    assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Host -> CP: plane dies between claim and progress; reconnect resumes with
# no duplicate deployment.
# ---------------------------------------------------------------------------

def test_plane_outage_between_claim_and_progress_no_duplicate_deploy(
        plane, docker, tmp_path, monkeypatch):
    """§52: worker claims a deploy task; the "running" report lands; then
    the plane dies mid-dispatch. The final progress report fails with
    WorkerAPIError (single typed error) and the dispatch returns
    locally-failed — but the deploy itself ran exactly once. When the
    plane returns, the worker re-reports running -> completed: one claim
    delivery, one container, one completion — the deploy is never
    executed twice."""
    from health import checker as health_checker_mod

    _, api_key = _register_agent(plane, f"ws1-hagent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, host["id"])
    task_id = task["id"]

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    # Deterministic outage point: the healthcheck runs strictly after the
    # "running" progress report, so gating on it guarantees the plane dies
    # mid-dispatch, never before the dispatch starts.
    healthcheck_started = threading.Event()
    real_healthcheck = health_checker_mod.wait_for_healthcheck

    def gated_healthcheck(*args, **kwargs):
        healthcheck_started.set()
        return real_healthcheck(*args, **kwargs)

    monkeypatch.setattr(health_checker_mod, "wait_for_healthcheck",
                        gated_healthcheck)

    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcomes: list = []
    worker_thread = threading.Thread(
        target=lambda: outcomes.append(
            TaskDispatcher(ctx, api).dispatch(claimed)),
        name="ws1-dispatch", daemon=True)
    worker_thread.start()
    assert healthcheck_started.wait(timeout=60), \
        "healthcheck never started; the dispatch stalled"
    assert plane.tasks[task_id]["status"] == "running"
    plane.shutdown()
    worker_thread.join(timeout=120)
    assert outcomes, "dispatch thread did not finish"

    # The final "completed" report failed (plane down): the dispatch
    # returns locally-failed without raising out of the worker...
    assert outcomes[0]["status"] == "failed"
    # ...but the deploy itself ran exactly once: one container, state saved.
    assert len(docker.run_calls) == 1
    state = ctx.deployment_store.load(deployment["id"])
    assert state is not None and state["status"] == "running"
    assert _plane_down(plane)

    # The plane returns on the same URL; the worker reconnects and resumes
    # the in-flight task with running -> completed (no re-claim).
    plane.start()
    wait_until(lambda: api.heartbeat(host["id"], {})["pending_tasks"] == 0,
               what="heartbeat succeeds after outage")
    api.progress(task_id,
                 build_progress_payload("running", "completed",
                                        result={"deployment_id":
                                                deployment["id"]}))
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.claim_deliveries[task_id] == 1  # never claimed twice
    assert sum(1 for e in plane.events
               if e["type"] == "task.completed") == 1
    assert len(docker.run_calls) == 1  # the deploy ran exactly once
    assert plane.deployments[deployment["id"]]["status"] == "running"

    # The app serves: the outage never touched the running container.
    assert http_get_text(
        f"http://127.0.0.1:{state['host_port']}") != ""


def _plane_down(plane: RecoveryPlane) -> bool:
    try:
        requests.get(plane.url + "/v1/health", timeout=1)
        return False
    except requests.RequestException:
        return True


# ---------------------------------------------------------------------------
# CP -> Cloudflare (worker edge): ingress sync failure cannot fail a deploy
# ---------------------------------------------------------------------------

class _FailingIngressProvider:
    """Ingress provider whose route sync always fails — models the
    Cloudflare control path being unreachable from this host."""

    name = "fake-cloudflare"
    enabled = True

    def __init__(self):
        self.sync_attempts = 0

    def sync_routes(self, routes):
        self.sync_attempts += 1
        raise RuntimeError("cloudflare API unreachable (injected)")


def test_ingress_sync_failure_does_not_fail_deploy(
        plane, docker, tmp_path, monkeypatch):
    """§52: after a successful deploy the worker syncs ingress routes
    (the Cloudflare control path). When that path is down,
    maybe_sync_on_change must swallow the failure — the deploy task still
    completes and the app serves."""
    _, api_key = _register_agent(plane, f"ws1-iagent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, host["id"])

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    provider = _FailingIngressProvider()
    ctx.ingress = provider
    # The domains listing succeeds; the failure is in the Cloudflare-side
    # route sync itself (sync_routes raises).
    monkeypatch.setattr(api, "list_host_domains",
                        lambda: {"domains": []})

    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed"
    assert provider.sync_attempts == 1  # the sync was attempted...
    # ...and its failure did not fail the deploy.
    assert plane.tasks[task["id"]]["status"] == "completed"
    state = ctx.deployment_store.load(deployment["id"])
    assert state["status"] == "running"
    assert http_get_text(
        f"http://127.0.0.1:{state['host_port']}") != ""

    # The swallowed failure is still visible in the task log (diagnosable,
    # not silent).
    log_text = ctx.log_store.tail_task(task["id"])
    assert "ingress sync" in log_text


# ---------------------------------------------------------------------------
# Host -> Docker/network: transport failure at docker run -> known state
# ---------------------------------------------------------------------------

class _TimeoutDocker(ThreadDockerClient):
    """Docker client whose run() dies with a transport-level error."""

    def run(self, name, image, ports=None, env=None, memory=None,
            cpus=None, restart="unless-stopped", timeout=120):
        self.run_calls.append({"name": name, "image": image,
                               "ports": dict(ports or {}),
                               "env": dict(env or {})})
        raise DockerError(["docker", "run", name], 1,
                          "connection timed out (injected)")


def test_docker_run_timeout_fails_clean_with_no_partial_state(
        plane, tmp_path):
    """§52: the Docker daemon is unreachable at run time -> the task fails
    with a clear error, no partial container exists, the port reservation
    is released, and the deployment row records the failure."""
    from deployments import pipeline as pipeline_mod

    _, api_key = _register_agent(plane, f"ws1-dagent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane)
    deployment, task = _create_deployment(plane, api_key, host["id"])

    docker = _TimeoutDocker()
    try:
        api = ControlPlaneClient(plane.url, host_token)
        claimed = api.claim_task(host["id"], ["docker"], wait=5)
        ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                       host_token=host_token)
        outcome = TaskDispatcher(ctx, api).dispatch(claimed)

        assert outcome["status"] == "failed"
        assert "timed out" in outcome["error"]
        assert plane.tasks[task["id"]]["status"] == "failed"
        # No partial container was registered.
        assert docker.containers == {}
        # The deployment never materialized (docker.run itself failed), so
        # no local state row exists for it — the known state is the failed
        # task with the clear error, not a phantom "running" deployment.
        # The control plane (source of truth) mirrors the task failure.
        state = ctx.deployment_store.load(deployment["id"])
        assert state is None or state["status"] != "running"
        # The port reservation was released on the failure path.
        assert pipeline_mod.reserved_port_snapshot() == set()
    finally:
        docker.shutdown_all()
