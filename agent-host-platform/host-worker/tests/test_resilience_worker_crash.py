"""WS-I §54 — host failure: the worker dies and the machine reboots.

Beyond test_recovery_host.py (which owns lease-expiry reclaim, docker
daemon restart/state loss, and reboot of a RUNNING deployment):

  * worker killed mid-deploy (after the container started, before the
    state was saved): no progress is ever reported, the claim lease
    expires, the sweeper requeues, a rebooted worker reclaims and the
    redeploy cleans up the half-deployed container BY NAME (container
    names are deterministic per deployment attempt) — no orphan, exactly
    one container serving, task completed exactly once;
  * VM reboot mid-artifact-download: a torn artifacts/<id>.bin left by
    the dead worker is re-downloaded clean by the rebooted worker
    (checksum-verified, quarantined on mismatch) — a partial download can
    never become a deployment;
  * worker crash between claim and dispatch: the lease alone recovers the
    task and the handler still runs exactly once.

What is REAL: the deploy pipeline, TaskDispatcher, ControlPlaneClient,
the real health checker, DeploymentStore disk persistence, and
ThreadDockerClient's real HTTP servers per "container". The control plane
is the sanctioned in-memory double (recovery_harness.RecoveryPlane).
"Reboot" = a brand-new WorkerContext (fresh DeploymentStore reloaded from
disk) against the surviving plane + docker daemon — the closest faithful
model where a real reboot is impossible.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time

import pytest
import requests

from agent.api import ControlPlaneClient
from deployments import pipeline as pipeline_mod
from deployments import reconcile as reconcile_mod
from executor.dispatcher import TaskDispatcher
from health import checker as health_checker_mod

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


def _deploy_payload(deployment_id: str, app: str, **overrides) -> dict:
    payload = {
        "project_id": f"proj-{app}",
        "project_name": app,
        "version": "1.0.0",
        "deployment_id": deployment_id,
        "image": f"img:{app}-1.0.0",  # prebuilt: no artifact download
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
    payload.update(overrides)
    return payload


def _create_deployment(plane: RecoveryPlane, api_key: str, host_id: str,
                       app: str, **payload_overrides) -> tuple[dict, dict]:
    resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"project_id": f"proj-{app}", "project_name": app,
              "version": "1.0.0", "host_id": host_id},
        timeout=5)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    deployment, task = data["deployment"], data["task"]
    with plane.lock:
        plane.tasks[task["id"]]["payload"].update(
            _deploy_payload(deployment["id"], app, **payload_overrides))
    return deployment, task


# ---------------------------------------------------------------------------
# worker killed mid-deploy: after docker.run, before state save
# ---------------------------------------------------------------------------

def test_worker_killed_mid_deploy_recovers_without_orphans(
        plane, docker, tmp_path, monkeypatch):
    """§54: the worker is SIGKILLed after the new container started but
    before the deployment state was saved. No progress is reported (the
    task stays in-flight), the lease expires, the sweeper requeues, and a
    rebooted worker reclaims the task. The redeploy finds the half-deployed
    container by its deterministic per-attempt name, removes it, and runs
    fresh: exactly one container, no orphan, task completed exactly once."""
    _, api_key = _register_agent(plane, f"ws3-agent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane, "ws3-crash-host")
    deployment, task = _create_deployment(plane, api_key, host["id"],
                                          "crash-app")
    task_id, dep_id = task["id"], deployment["id"]

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    # SIGKILL right after the container started: the healthcheck — the
    # first pipeline step after docker.run — raises SystemExit, which no
    # `except Exception` in the dispatcher catches, exactly like a killed
    # process: no progress report, no state save, container left running.
    real_healthcheck = health_checker_mod.wait_for_healthcheck

    def kill_during_healthcheck(*args, **kwargs):
        raise SystemExit("SIGKILL (injected)")

    monkeypatch.setattr(health_checker_mod, "wait_for_healthcheck",
                        kill_during_healthcheck)

    work_dir = str(tmp_path)
    ctx = make_ctx(work_dir, api, docker, host_id=host["id"],
                   host_token=host_token)
    killed: list = []

    def _run():
        try:
            TaskDispatcher(ctx, api).dispatch(claimed)
        except SystemExit as exc:
            killed.append(exc)

    worker_thread = threading.Thread(target=_run, name="ws3-victim",
                                     daemon=True)
    worker_thread.start()
    worker_thread.join(timeout=120)
    assert killed, "the worker was not killed mid-deploy"

    # The kill left: one running container, NO state row, the task still
    # in-flight (the "running" report landed before the handler ran), and
    # no dangling port reservation (the pipeline's finally released it).
    assert len(docker.run_calls) == 1
    orphan_name = docker.run_calls[0]["name"]
    assert docker.container_status(orphan_name) == "running"
    assert ctx.deployment_store.load(dep_id) is None
    assert plane.tasks[task_id]["status"] == "running"
    assert pipeline_mod.reserved_port_snapshot() == set()

    # "Reboot": a brand-new worker (fresh store reloaded from disk).
    # Reconcile finds no running deployments — nothing to disturb.
    ctx2 = make_ctx(work_dir, api, docker, host_id=host["id"],
                    host_token=host_token)
    summary = reconcile_mod.reconcile(ctx2)
    assert summary["reconciled"] == 0
    assert summary["already_running"] == 0

    # The claim lease expires; the sweeper requeues; the rebooted worker
    # reclaims the same task.
    plane.expire_all_leases()
    assert plane.run_sweep() == [task_id]
    claimed2 = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed2 is not None and claimed2["id"] == task_id
    assert plane.claim_deliveries[task_id] == 2

    # Redeploy with the healthcheck restored: the pipeline sees the
    # half-deployed container under the deterministic per-attempt name and
    # removes it before running fresh.
    monkeypatch.setattr(health_checker_mod, "wait_for_healthcheck",
                        real_healthcheck)
    outcome = TaskDispatcher(ctx2, api).dispatch(claimed2)
    assert outcome["status"] == "completed"

    assert orphan_name in docker.rm_calls  # the half-deploy was cleaned up
    assert list(docker.containers) == [orphan_name]  # exactly one container
    assert docker.container_status(orphan_name) == "running"
    state = ctx2.deployment_store.load(dep_id)
    assert state is not None and state["status"] == "running"
    assert state["container_name"] == orphan_name
    wait_until(
        lambda: http_get_text(
            f"http://127.0.0.1:{state['host_port']}/health") == "ok",
        what="redeployed app serving after crash recovery")
    assert plane.tasks[task_id]["status"] == "completed"
    assert sum(1 for e in plane.events
               if e["type"] == "task.completed") == 1


# ---------------------------------------------------------------------------
# VM reboot mid-artifact-download: torn bytes are never trusted
# ---------------------------------------------------------------------------

def _make_artifact(app: str) -> bytes:
    """A real artifact tarball: Dockerfile + agent.deploy.json manifest."""
    manifest = {
        "name": app,
        "runtime": "docker",
        "service": {"port": 3000, "healthcheck": "/health"},
        "resources": {},
        "restart": "unless-stopped",
        "env": {"APP_NAME": app},
    }
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


def test_vm_reboot_mid_download_redownloads_clean(
        plane, docker, tmp_path):
    """§54: the worker dies mid-artifact-download, leaving a torn
    artifacts/<id>.bin. After the reboot the worker re-downloads from
    scratch (the torn bytes are overwritten, never trusted), the SHA-256
    verifies, and the deploy completes — the app serves."""
    app = "dl-app"
    _, api_key = _register_agent(plane, f"ws3-dl-agent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane, "ws3-dl-host")

    artifact_bytes = _make_artifact(app)
    checksum = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    up = requests.post(
        f"{plane.url}/v1/test/artifacts", data=artifact_bytes,
        headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    assert up.status_code == 201, up.text
    artifact = up.json()["artifact"]
    assert artifact["checksum"] == checksum

    resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"project_id": f"proj-{app}", "project_name": app,
              "version": "1.0.0", "host_id": host["id"]},
        timeout=5)
    assert resp.status_code == 201, resp.text
    deployment = resp.json()["deployment"]
    task_id = resp.json()["task"]["id"]
    # What the production control plane injects into the deploy task
    # payload (routes/deployments.ts); the harness predates it, so the
    # test sets the same fields. No payload manifest/image: the worker
    # must take the full artifact path (download -> verify -> extract).
    with plane.lock:
        plane.tasks[task_id]["payload"].update({
            "artifact_id": artifact["id"],
            "artifact_checksum": artifact["checksum"],
            "artifact_size": artifact["size"],
            "healthcheck_timeout": 10,
        })

    work_dir = str(tmp_path)
    # The dead worker's torn download: half the bytes, then death.
    torn = (work_dir + f"/artifacts/{artifact['id']}.bin")
    import os
    os.makedirs(work_dir + "/artifacts", exist_ok=True)
    with open(torn, "wb") as fh:
        fh.write(artifact_bytes[:len(artifact_bytes) // 3])

    # "Reboot": fresh worker claims and dispatches the deploy task.
    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id
    ctx = make_ctx(work_dir, api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed", outcome

    # The torn bytes were replaced by a clean, verified download...
    with open(torn, "rb") as fh:
        assert fh.read() == artifact_bytes
    # ...nothing was quarantined (the bytes verified first try)...
    quarantine = os.path.join(work_dir, "quarantine")
    assert not os.path.isdir(quarantine) or not os.listdir(quarantine)
    # ...and the deploy completed end to end: image built, container
    # running, app serving.
    assert len(docker.images) == 1
    state = ctx.deployment_store.load(deployment["id"])
    assert state is not None and state["status"] == "running"
    wait_until(
        lambda: http_get_text(
            f"http://127.0.0.1:{state['host_port']}/health") == "ok",
        what="artifact-path app serving after reboot")
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.deployments[deployment["id"]]["status"] == "running"


# ---------------------------------------------------------------------------
# crash between claim and dispatch: the lease alone recovers the task
# ---------------------------------------------------------------------------

def test_worker_crash_between_claim_and_dispatch_runs_handler_once(
        plane, docker, tmp_path):
    """§54: the worker claims a deploy task and dies before dispatching
    anything (no progress at all). The lease expires, the sweeper requeues,
    another worker claims it and the handler runs exactly once — one
    container, one completion."""
    _, api_key = _register_agent(plane, f"ws3-cagent-{time.monotonic_ns()}")
    host, host_token = _register_host(plane, "ws3-claim-host")
    deployment, task = _create_deployment(plane, api_key, host["id"],
                                          "claim-app")
    task_id = task["id"]

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id
    # The worker dies here: nothing dispatched, no progress reported.
    assert plane.tasks[task_id]["status"] == "claimed"

    plane.expire_all_leases()
    assert plane.run_sweep() == [task_id]

    api2 = ControlPlaneClient(plane.url, host_token)
    claimed2 = api2.claim_task(host["id"], ["docker"], wait=5)
    assert claimed2 is not None and claimed2["id"] == task_id
    assert plane.claim_deliveries[task_id] == 2

    ctx = make_ctx(str(tmp_path), api2, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = TaskDispatcher(ctx, api2).dispatch(claimed2)
    assert outcome["status"] == "completed"
    assert len(docker.run_calls) == 1  # handler ran exactly once
    state = ctx.deployment_store.load(deployment["id"])
    assert state["status"] == "running"
    assert plane.tasks[task_id]["status"] == "completed"
