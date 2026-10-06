"""WS-I §55 — agent failure: the agent is gone; the deployment completes.

MANDATORY scenario, run for BOTH agent types (Muse and Instinct — the
protocol is agent-neutral, so both flow through the identical code path):

  1. the agent registers, uploads a REAL artifact tarball and creates a
     deployment (the task row is the durable source of truth);
  2. the agent's process is killed mid-flow: its credentials are revoked
     server-side and the key is dropped — the session is dead (401);
  3. a host — holding only its OWN host token, never the agent's — claims
     the deploy task and runs the FULL deploy pipeline (download, SHA-256
     verify, extract, manifest validation, build, run, healthcheck) to
     completion;
  4. the deployed app serves real traffic with no agent anywhere in the
     loop;
  5. a NEW agent session (different token, SAME agent identity) reads back
     the final task and deployment state.

A second test proves the same durability when the host itself only
appears AFTER the agent vanished: the task waits in the queue and the
late host still completes the full deploy.

What is REAL: ControlPlaneClient, TaskDispatcher, the full deploy
pipeline, the real health checker, and ThreadDockerClient's real HTTP
servers per "container". The control plane is the sanctioned in-memory
double (recovery_harness.RecoveryPlane).
"""
from __future__ import annotations

import io
import json
import tarfile
import time

import pytest
import requests

from agent.api import ControlPlaneClient
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


def _agent_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_agent(plane: RecoveryPlane, name: str, agent_type: str):
    resp = requests.post(f"{plane.url}/v1/agents/register",
                         json={"name": name, "type": agent_type}, timeout=5)
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


def _agent_creates_deployment(plane: RecoveryPlane, api_key: str,
                              host_id: str, app: str,
                              idempotency_key: str) -> tuple[dict, dict, dict]:
    """The agent's full durable act: upload artifact, create deployment."""
    artifact_bytes = _make_artifact(app)
    up = requests.post(
        f"{plane.url}/v1/test/artifacts", data=artifact_bytes,
        headers=_agent_headers(api_key), timeout=10)
    assert up.status_code == 201, up.text
    artifact = up.json()["artifact"]

    resp = requests.post(
        f"{plane.url}/v1/deployments", headers=_agent_headers(api_key),
        json={"project_id": f"proj-{app}", "project_name": app,
              "version": "1.0.0", "host_id": host_id,
              "idempotency_key": idempotency_key},
        timeout=5)
    assert resp.status_code == 201, resp.text
    deployment = resp.json()["deployment"]
    task = resp.json()["task"]
    # What the production control plane injects into the deploy task
    # payload (routes/deployments.ts); the harness predates it, so the
    # test sets the same fields.
    with plane.lock:
        plane.tasks[task["id"]]["payload"].update({
            "artifact_id": artifact["id"],
            "artifact_checksum": artifact["checksum"],
            "artifact_size": artifact["size"],
            "healthcheck_timeout": 10,
        })
    return deployment, task, artifact


@pytest.mark.parametrize("agent_type", ["Muse", "instinct"])
def test_agent_killed_full_deploy_completes_and_serves(
        plane, docker, tmp_path, agent_type):
    """§55: the agent uploads, creates the deployment, and is killed. The
    host claims the orphaned task and drives the FULL deploy pipeline to
    completion; the app serves with no agent alive; a new agent session
    reads the final state."""
    app = f"gone-{agent_type}"
    agent, api_key = _register_agent(
        plane, f"ws4-agent-{agent_type}-{time.monotonic_ns()}", agent_type)
    host, host_token = _register_host(plane, f"ws4-host-{agent_type}")

    # 1. The agent does its durable act: artifact + deployment + task row.
    deployment, task, _ = _agent_creates_deployment(
        plane, api_key, host["id"], app,
        f"ws4-dep-{agent_type}-{time.monotonic_ns()}")
    task_id, dep_id = task["id"], deployment["id"]
    assert task["created_by"] == agent["id"]
    assert plane.tasks[task_id]["status"] == "queued"

    # 2. The agent's process is killed: credentials revoked server-side,
    #    the key dropped. The old session is truly dead.
    plane.revoke_agent(agent["id"])
    del api_key
    gone = requests.get(f"{plane.url}/v1/tasks/{task_id}",
                        headers=_agent_headers("agt-dead-key"), timeout=5)
    assert gone.status_code == 401

    # 3. The host (host token only — never the agent's) claims the
    #    orphaned task and runs the FULL deploy pipeline.
    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed", outcome.get("error")
    assert outcome["result"]["deployment_id"] == dep_id

    # The full pipeline ran: artifact downloaded + verified, image built,
    # exactly one container started.
    assert len(docker.images) == 1
    assert len(docker.run_calls) == 1
    assert plane.claim_deliveries[task_id] == 1
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.deployments[dep_id]["status"] == "running"

    # 4. The app serves real traffic with no agent anywhere in the loop.
    state = ctx.deployment_store.load(dep_id)
    assert state is not None and state["status"] == "running"
    wait_until(
        lambda: http_get_text(
            f"http://127.0.0.1:{state['host_port']}/health") == "ok",
        what="app serving after the agent is gone")

    # 5. A NEW agent session — different token, SAME agent identity —
    #    reads back the final task and deployment state.
    new_key = plane.reprovision_agent(agent["id"])
    task_view = requests.get(
        f"{plane.url}/v1/tasks/{task_id}",
        headers=_agent_headers(new_key), timeout=5)
    assert task_view.status_code == 200
    final_task = task_view.json()["task"]
    assert final_task["status"] == "completed"
    assert final_task["result"]["deployment_id"] == dep_id

    dep_view = requests.get(
        f"{plane.url}/v1/deployments/{dep_id}",
        headers=_agent_headers(new_key), timeout=5)
    assert dep_view.status_code == 200
    assert dep_view.json()["deployment"]["status"] == "running"

    # The app still serves at the very end — the final artifact never
    # depended on the agent staying alive.
    assert http_get_text(
        f"http://127.0.0.1:{state['host_port']}/health") == "ok"

    # The event trail records the whole arc.
    types = [e["type"] for e in plane.events]
    assert "agent.disappeared" in types
    assert "task.claimed" in types
    assert "task.completed" in types
    assert "agent.reprovisioned" in types


def test_agent_gone_before_host_exists_late_host_completes_deploy(
        plane, docker, tmp_path):
    """§55 variant: the agent vanishes before any host exists. The task
    waits durably in the queue; a host that registers LATER still claims
    it and completes the full deploy."""
    app = "late-host-app"
    agent, api_key = _register_agent(
        plane, f"ws4-early-{time.monotonic_ns()}", "Muse")

    # The agent creates the deployment with no host assigned...
    deployment, task, _ = _agent_creates_deployment(
        plane, api_key, None, app, f"ws4-late-{time.monotonic_ns()}")
    task_id, dep_id = task["id"], deployment["id"]

    # ...and is killed before any host ever registers.
    plane.revoke_agent(agent["id"])
    del api_key
    assert plane.tasks[task_id]["status"] == "queued"

    # A host appears much later and picks up the orphaned work.
    host, host_token = _register_host(plane, "ws4-late-host")
    api = ControlPlaneClient(plane.url, host_token)
    assert api.heartbeat(host["id"], {})["pending_tasks"] == 1
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed", outcome.get("error")

    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.deployments[dep_id]["status"] == "running"
    assert plane.claim_deliveries[task_id] == 1
    state = ctx.deployment_store.load(dep_id)
    wait_until(
        lambda: http_get_text(
            f"http://127.0.0.1:{state['host_port']}/health") == "ok",
        what="late-host app serving")
