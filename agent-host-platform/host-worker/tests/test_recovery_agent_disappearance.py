"""W13a §38 — agent disappearance: the worker outlives the agent's session.

Scenario (run for BOTH agent types, Muse and Instinct — the protocol is
agent-neutral by design, so both flow through the identical code path):

  1. agent registers (type "Muse" / "instinct") and creates a deployment
     task via its API key;
  2. mid-flow the agent "disappears": its credentials are dropped
     server-side (revoked), so the original session is dead (401);
  3. the worker — holding only its OWN host token — still claims the task
     and drives it to completion (real ControlPlaneClient + real
     TaskDispatcher over real HTTP);
  4. a NEW agent session (different token, SAME agent identity, mirroring
     POST /v1/agents/me/rotate semantics) retrieves the final task AND
     deployment state.

What is REAL: ControlPlaneClient, TaskDispatcher, the task state machine
and lease/ownership checks in the harness plane (mirroring routes/worker.ts
and agents.ts). The plane itself is the sanctioned test double (see
recovery_harness.py).
"""
from __future__ import annotations

import pytest
import requests

from agent.api import ControlPlaneClient, WorkerAPIError
from executor.dispatcher import TaskDispatcher

from recovery_harness import (
    FakeDocker,
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
    docker = FakeDocker()
    yield docker
    docker.shutdown_all()


def _agent_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _register_agent(plane: RecoveryPlane, name: str, agent_type: str):
    resp = requests.post(f"{plane.url}/v1/agents/register",
                         json={"name": name, "type": agent_type}, timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["agent"], body["api_key"]


def _register_host(plane: RecoveryPlane):
    resp = requests.post(
        f"{plane.url}/v1/hosts/register",
        json={"name": "w13a-host", "capabilities": ["docker"],
              "worker_version": "test"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["host"], body["host_token"]


@pytest.mark.parametrize("agent_type", ["Muse", "instinct"])
def test_agent_disappearance_worker_completes_and_new_session_reads_state(
        plane, docker, tmp_path, agent_type):
    """§38: agent vanishes mid-flow; worker completes; new session reads."""
    agent, api_key = _register_agent(plane, f"w13a-agent-{agent_type}", agent_type)
    assert agent["type"] == agent_type
    host, host_token = _register_host(plane)

    # 1. The agent creates a deployment (which mints a deploy task).
    resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers=_agent_headers(api_key),
        json={"project_id": "proj-web", "project_name": "web",
              "version": "1.0.0", "host_id": host["id"]},
        timeout=5)
    assert resp.status_code == 201, resp.text
    deployment = resp.json()["deployment"]
    task = resp.json()["task"]
    assert task["created_by"] == agent["id"]  # task is owned by the agent row

    # 2. The agent disappears: credentials dropped mid-flow.
    plane.revoke_agent(agent["id"])
    gone = requests.get(f"{plane.url}/v1/tasks/{task['id']}",
                        headers=_agent_headers(api_key), timeout=5)
    assert gone.status_code == 401  # the old session is truly dead

    # 3. The worker (host token only — never the agent's) claims and
    #    completes the task: real client + real dispatcher over real HTTP.
    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task["id"]

    # system-info runs with no docker dependency and completes fast; the
    # claim/progress/state-machine path is what this scenario exercises.
    claimed["type"] = "system-info"
    with plane.lock:
        plane.tasks[task["id"]]["type"] = "system-info"
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed"

    wait_until(lambda: plane.tasks[task["id"]]["status"] == "completed",
               what="task reaches completed")
    assert plane.deployments[deployment["id"]]["status"] == "running"

    # 4. A NEW agent session — different token, SAME agent identity —
    #    retrieves the final task and deployment state.
    new_key = plane.reprovision_agent(agent["id"])
    assert new_key != api_key
    task_view = requests.get(f"{plane.url}/v1/tasks/{task['id']}",
                             headers=_agent_headers(new_key), timeout=5)
    assert task_view.status_code == 200
    final_task = task_view.json()["task"]
    assert final_task["id"] == task["id"]
    assert final_task["status"] == "completed"
    assert final_task["result"] is not None

    dep_view = requests.get(f"{plane.url}/v1/deployments/{deployment['id']}",
                            headers=_agent_headers(new_key), timeout=5)
    assert dep_view.status_code == 200
    assert dep_view.json()["deployment"]["status"] == "running"

    # The agent identity survived: same row, same name/type.
    assert plane.agents[agent["id"]]["name"] == f"w13a-agent-{agent_type}"
    assert plane.agents[agent["id"]]["type"] == agent_type

    # Event trail is intact across the disappearance.
    types = [e["type"] for e in plane.events]
    assert "task.claimed" in types
    assert "task.completed" in types
    assert "agent.disappeared" in types
    assert "agent.reprovisioned" in types


@pytest.mark.parametrize("agent_type", ["Muse", "instinct"])
def test_agent_key_rotation_midflight_keeps_task_visible(plane, agent_type):
    """§38 variant: the production /me/rotate path — old key dies, new key
    (same identity) keeps full visibility of in-flight work."""
    agent, api_key = _register_agent(plane, f"w13a-rot-{agent_type}", agent_type)

    resp = requests.post(
        f"{plane.url}/v1/tasks", headers=_agent_headers(api_key),
        json={"type": "system-info", "payload": {}}, timeout=5)
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task"]["id"]

    # Rotate mid-flight (mirrors POST /v1/agents/me/rotate).
    rot = requests.post(f"{plane.url}/v1/agents/me/rotate",
                        headers=_agent_headers(api_key), timeout=5)
    assert rot.status_code == 200
    new_key = rot.json()["api_key"]
    assert new_key != api_key

    # Old key is dead everywhere.
    assert requests.get(
        f"{plane.url}/v1/tasks/{task_id}",
        headers=_agent_headers(api_key), timeout=5).status_code == 401

    # New key — same agent identity — sees the task.
    view = requests.get(f"{plane.url}/v1/tasks/{task_id}",
                        headers=_agent_headers(new_key), timeout=5)
    assert view.status_code == 200
    assert view.json()["task"]["id"] == task_id

    # A different agent's key cannot see it either (401 unknown key).
    other, other_key = _register_agent(plane, f"w13a-other-{agent_type}",
                                       agent_type)
    plane.revoke_agent(other["id"])
    assert requests.get(
        f"{plane.url}/v1/tasks/{task_id}",
        headers=_agent_headers(other_key), timeout=5).status_code == 401
