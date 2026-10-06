"""WS-I §53 — control-plane outage: the CP is not a runtime dependency.

Proves the full outage arc, beyond test_recovery_controlplane.py (which
owns the mid-task outage, claim/heartbeat backoff loops, and concurrent
claims):

  * apps running -> CP unavailable -> apps remain running AND the worker's
    own reconcile does not destroy healthy apps while the CP is down
    (no stop/remove calls; both apps keep serving);
  * a container that died during the outage is still restored by
    reconcile with no control plane involved;
  * work the worker completed while the CP was down reconciles on return:
    the in-flight task is re-reported running -> completed exactly once,
    never re-executed;
  * after a clean reconnect the worker picks up work that was queued
    before the outage — nothing is lost, nothing is duplicated.

What is REAL: the deploy pipeline, deployments.reconcile, the real
health checker, ControlPlaneClient, and ThreadDockerClient's real HTTP
servers per "container". The control plane is the sanctioned in-memory
double (recovery_harness.RecoveryPlane).
"""
from __future__ import annotations

import time

import pytest
import requests

from agent.api import (
    ControlPlaneClient,
    WorkerAPIError,
    build_progress_payload,
)
from deployments import pipeline as pipeline_mod
from deployments import reconcile as reconcile_mod
from executor import handlers as handlers_mod
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


def _register_host(plane: RecoveryPlane, name: str):
    resp = requests.post(
        f"{plane.url}/v1/hosts/register",
        json={"name": name, "capabilities": ["docker"],
              "worker_version": "test"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["host"], body["host_token"]


def _deploy_payload(deployment_id: str, app: str) -> dict:
    return {
        "project_id": f"proj-{app}",
        "project_name": app,
        "version": "1.0.0",
        "deployment_id": deployment_id,
        "image": f"img:{app}-1.0.0",
        "manifest": {
            "name": app,
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {},
            "restart": "unless-stopped",
            "env": {"APP_NAME": app},
        },
        "healthcheck_timeout": 10,
    }


def _deploy_app(ctx, docker, app: str) -> dict:
    """Run the REAL deploy pipeline for one app; return the saved state."""
    task = {"id": f"task-{app}", "type": "deploy",
            "payload": _deploy_payload(f"dep-{app}", app)}
    result = pipeline_mod.deploy(ctx, task)
    assert result["status"] == "running"
    state = ctx.deployment_store.load(f"dep-{app}")
    assert state is not None and state["status"] == "running"
    assert http_get_text(
        f"http://127.0.0.1:{state['host_port']}/health") == "ok"
    return state


# ---------------------------------------------------------------------------
# apps running -> CP down -> reconcile destroys nothing, apps keep serving
# ---------------------------------------------------------------------------

def test_extended_outage_worker_does_not_destroy_healthy_apps(
        plane, docker, tmp_path):
    """§53: two apps running; the control plane goes down for an extended
    period. The worker's reconcile (which runs at every worker restart)
    must not stop, remove, or otherwise disturb healthy apps while the CP
    is unreachable — and both apps keep serving traffic throughout."""
    host, host_token = _register_host(plane, "ws2-outage-host")
    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    state_a = _deploy_app(ctx, docker, "app-a")
    state_b = _deploy_app(ctx, docker, "app-b")

    # The control plane disappears for an extended period.
    plane.shutdown()

    # A worker (re)start during the outage runs reconcile with no control
    # plane available at all.
    offline_ctx = make_ctx(str(tmp_path), None, docker,
                           host_id=host["id"], host_token=host_token)
    summary = reconcile_mod.reconcile(offline_ctx)

    assert summary["reconciled"] == 0
    assert summary["already_running"] == 2
    assert summary["missing"] == 0
    # The worker never touched the healthy containers: no stop, no remove,
    # no restart — the outage alone must never tear apps down.
    assert docker.stop_calls == []
    assert docker.rm_calls == []
    assert docker.start_calls == []

    # Both apps kept serving through the whole outage.
    assert http_get_text(
        f"http://127.0.0.1:{state_a['host_port']}/health") == "ok"
    assert http_get_text(
        f"http://127.0.0.1:{state_b['host_port']}/health") == "ok"

    plane.start()
    wait_until(lambda: api.heartbeat(host["id"], {})["pending_tasks"] == 0,
               what="heartbeat succeeds after extended outage")


def test_container_death_during_outage_restored_without_control_plane(
        plane, docker, tmp_path):
    """§53: a container dies while the CP is down. Reconcile restores it
    from the local deployment contract alone — the control plane is not
    needed to bring apps back."""
    host, host_token = _register_host(plane, "ws2-restore-host")
    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    state = _deploy_app(ctx, docker, "app-c")
    name = state["container_name"]
    port = state["host_port"]

    plane.shutdown()

    # The container dies mid-outage (host-level failure, CP unreachable).
    docker.stop(name)
    assert docker.container_status(name) == "exited"
    with pytest.raises(Exception):
        http_get_text(f"http://127.0.0.1:{port}/health", timeout=2)

    offline_ctx = make_ctx(str(tmp_path), None, docker,
                           host_id=host["id"], host_token=host_token)
    summary = reconcile_mod.reconcile(offline_ctx)
    assert summary["reconciled"] == 1
    assert docker.start_calls == [name]
    wait_until(
        lambda: http_get_text(f"http://127.0.0.1:{port}/health") == "ok",
        what="app serving again after restore during outage")
    plane.start()


# ---------------------------------------------------------------------------
# work completed during the outage reconciles exactly once on return
# ---------------------------------------------------------------------------

def test_work_completed_during_outage_reconciles_on_return(
        plane, docker, tmp_path):
    """§53: the worker claims a deploy task, then the CP dies. The worker
    still runs the deploy locally (the handler needs no control plane);
    its progress report fails with WorkerAPIError. When the CP returns,
    the worker re-reports running -> completed: the task completes exactly
    once and the deploy is never re-executed."""
    _, api_key = _register_agent(plane, f"ws2-agent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane, "ws2-reconcile-host")

    dep_resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"project_id": "proj-d", "project_name": "app-d",
              "version": "1.0.0", "host_id": host["id"]},
        timeout=5)
    assert dep_resp.status_code == 201, dep_resp.text
    deployment = dep_resp.json()["deployment"]
    task_id = dep_resp.json()["task"]["id"]
    with plane.lock:
        plane.tasks[task_id]["payload"].update(
            _deploy_payload(deployment["id"], "app-d"))

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)

    # The CP dies. The deploy still runs locally to completion — the
    # handler's only CP touch (project secrets) is non-fatal.
    plane.shutdown()
    result = handlers_mod.handle_deploy(ctx, claimed)
    assert result["status"] == "running"
    state = ctx.deployment_store.load(deployment["id"])
    assert state["status"] == "running"

    # ...but the progress report cannot be delivered: single typed error.
    with pytest.raises(WorkerAPIError):
        api.progress(task_id, build_progress_payload("claimed", "running"))
    assert plane_down(plane)
    # The app is already serving even though the CP knows nothing yet.
    assert http_get_text(
        f"http://127.0.0.1:{state['host_port']}/health") == "ok"

    # The CP returns; the worker reconnects and reconciles the in-flight
    # task: running -> completed, exactly once, no re-execution.
    plane.start()
    wait_until(lambda: api.heartbeat(host["id"], {})["pending_tasks"] == 0,
               what="heartbeat succeeds after outage")
    api.progress(task_id, build_progress_payload("claimed", "running"))
    api.progress(task_id,
                 build_progress_payload("running", "completed", result=result))
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.claim_deliveries[task_id] == 1
    assert sum(1 for e in plane.events
               if e["type"] == "task.completed") == 1
    assert len(docker.run_calls) == 1  # executed exactly once
    assert plane.deployments[deployment["id"]]["status"] == "running"


def plane_down(plane: RecoveryPlane) -> bool:
    try:
        requests.get(plane.url + "/v1/health", timeout=1)
        return False
    except requests.RequestException:
        return True


# ---------------------------------------------------------------------------
# reconnect picks up work queued before the outage
# ---------------------------------------------------------------------------

def test_reconnect_picks_up_work_queued_before_outage(
        plane, docker, tmp_path):
    """§53: a task queued before the outage is still there after it. On
    reconnect the worker's heartbeat sees it pending, claims it, and
    completes it — queued work is never lost or duplicated by an outage."""
    _, api_key = _register_agent(plane, f"ws2-qagent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane, "ws2-queue-host")

    task_resp = requests.post(
        f"{plane.url}/v1/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"type": "system-info", "payload": {}}, timeout=5)
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["task"]["id"]

    # Outage with the task still queued.
    plane.shutdown()
    assert plane_down(plane)
    plane.start()

    api = ControlPlaneClient(plane.url, host_token)
    assert api.heartbeat(host["id"], {})["pending_tasks"] == 1
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed"
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.claim_deliveries[task_id] == 1
