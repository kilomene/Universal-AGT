"""W13a §39 — host failure: leases, daemon restarts, and host reboots.

  (a) Worker crash mid-task: worker A claims a task and dies (no more
      progress). The claim lease expires, the sweeper safely requeues the
      task, worker B claims it — and when worker A comes back its stale
      progress is rejected (403) so it cannot corrupt B's task. No
      duplicate execution: the handler runs exactly once.
  (b) Docker daemon restart: containers die but the daemon still knows
      them -> reconcile() restarts them and traffic resumes. Daemon state
      loss (containers vanish entirely) -> reconcile() recreates them from
      the persisted deployment contract. Either way the SAME spec
      (image, ports, env) comes back and the app serves traffic again.
  (c) Simulated host reboot: a brand-new worker (fresh DeploymentStore
      reloaded from disk, empty docker view) reconciles desired vs actual
      and restores the required services.

What is REAL: ControlPlaneClient claim/progress, the sweeper semantics in
the harness plane (mirroring lib/taskSweeper.ts), deployments.reconcile,
DeploymentStore disk persistence, and — via FakeDocker — real HTTP server
subprocesses per container, so "serves traffic" is a socket-level fact.
"""
from __future__ import annotations

import pytest
import requests

from agent.api import ControlPlaneClient, WorkerAPIError
from deployments import reconcile as reconcile_mod
from deployments.state import DeploymentStore
from executor.dispatcher import TaskDispatcher

from recovery_harness import (
    FakeDocker,
    RecoveryPlane,
    free_port,
    http_get_text,
    make_ctx,
    wait_until,
)


@pytest.fixture()
def plane():
    plane = RecoveryPlane(lease_s=600).start()
    yield plane
    plane.shutdown()


@pytest.fixture()
def docker():
    docker = FakeDocker()
    yield docker
    docker.shutdown_all()


def _register_host(plane: RecoveryPlane, name: str):
    resp = requests.post(
        f"{plane.url}/v1/hosts/register",
        json={"name": name, "capabilities": ["docker"],
              "worker_version": "test"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["host"], body["host_token"]


def _seed_running_deployment(store: DeploymentStore, deployment_id: str,
                             port: int) -> dict:
    state = {
        "deployment_id": deployment_id,
        "project_id": "proj-web",
        "project_name": "web",
        "version": "1.0.0",
        "container_name": f"uaht-web-{deployment_id}",
        "image": "uaht-web:1.0.0",
        "runtime": "docker",
        "host_port": port,
        "container_port": 3000,
        "ports": {str(port): 3000},
        "resources": {},
        "restart": "unless-stopped",
        "env": {"APP_ENV": "test"},
        "status": "running",
    }
    store.save(state)
    return state


# ---------------------------------------------------------------------------
# (a) lease expiry: crash -> requeue -> reclaim; stale worker cannot corrupt
# ---------------------------------------------------------------------------

def test_crashed_worker_task_requeued_and_stale_progress_rejected(
        plane, docker, tmp_path, monkeypatch):
    host_a, token_a = _register_host(plane, "worker-a")
    host_b, token_b = _register_host(plane, "worker-b")

    # An agent creates a task; worker A claims it, then "crashes".
    agent_resp = requests.post(
        f"{plane.url}/v1/agents/register",
        json={"name": "w13a-lease-agent", "type": "Muse"}, timeout=5)
    agent_key = agent_resp.json()["api_key"]
    task_resp = requests.post(
        f"{plane.url}/v1/tasks",
        headers={"Authorization": f"Bearer {agent_key}"},
        json={"type": "system-info", "payload": {}, "max_attempts": 10},
        timeout=5)
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["task"]["id"]

    api_a = ControlPlaneClient(plane.url, token_a)
    claimed_a = api_a.claim_task(host_a["id"], ["docker"], wait=5)
    assert claimed_a is not None and claimed_a["id"] == task_id

    # Worker A dies without another progress report. Force the lease into
    # the past (deterministic — no wall-clock waiting) and run the sweeper.
    plane.expire_all_leases()
    requeued = plane.run_sweep()
    assert requeued == [task_id]
    with plane.lock:
        swept = dict(plane.tasks[task_id])
    assert swept["status"] == "queued"
    assert swept["claimed_by"] is None
    assert swept["lease_expires_at"] is None
    assert any(e["type"] == "task.requeued" and e["task_id"] == task_id
               for e in plane.events)

    # Worker B claims the requeued task.
    api_b = ControlPlaneClient(plane.url, token_b)
    claimed_b = api_b.claim_task(host_b["id"], ["docker"], wait=5)
    assert claimed_b is not None and claimed_b["id"] == task_id

    # Worker A comes back and tries to report on B's task: rejected, and
    # the task is untouched by the stale write.
    before = dict(plane.tasks[task_id])
    with pytest.raises(WorkerAPIError) as excinfo:
        api_a.progress(task_id, {"status": "running"})
    assert "403" in str(excinfo.value)
    after = dict(plane.tasks[task_id])
    assert after["status"] == before["status"] == "claimed"
    assert after["claimed_by"] == host_b["id"]
    assert not any(e["type"] == "task.running" and e.get("host_id") == host_a["id"]
                   for e in plane.events)

    # Worker B runs it to completion — the handler executes exactly once.
    executions = []
    orig_dispatch = TaskDispatcher.dispatch

    def counting(self, task):
        executions.append(task["id"])
        return orig_dispatch(self, task)

    monkeypatch.setattr(TaskDispatcher, "dispatch", counting)
    ctx_b = make_ctx(str(tmp_path), api_b, docker, host_id=host_b["id"],
                     host_token=token_b)
    outcome = TaskDispatcher(ctx_b, api_b).dispatch(claimed_b)
    assert outcome["status"] == "completed"
    assert executions == [task_id]  # no duplicate execution
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.tasks[task_id]["claimed_by"] == host_b["id"]
    assert plane.claim_deliveries[task_id] == 2  # handed out twice, ran once


def test_lease_expiry_with_exhausted_budget_fails_task(plane):
    """A task that already burned its retry budget fails instead of
    requeueing forever (mirrors the sweeper's max_attempts path)."""
    host, _ = _register_host(plane, "worker-c")
    agent_resp = requests.post(
        f"{plane.url}/v1/agents/register",
        json={"name": "w13a-budget-agent", "type": "instinct"}, timeout=5)
    agent_key = agent_resp.json()["api_key"]
    task_resp = requests.post(
        f"{plane.url}/v1/tasks",
        headers={"Authorization": f"Bearer {agent_key}"},
        json={"type": "system-info", "payload": {}, "max_attempts": 1},
        timeout=5)
    task_id = task_resp.json()["task"]["id"]
    with plane.lock:
        plane.tasks[task_id].update(
            status="claimed", claimed_by=host["id"],
            lease_expires_at=__import__("time").time() - 1.0, attempts=1)
    requeued = plane.run_sweep()
    assert requeued == []
    assert plane.tasks[task_id]["status"] == "failed"
    assert "budget" in (plane.tasks[task_id]["error"] or "")


# ---------------------------------------------------------------------------
# (b) docker daemon restart / state loss
# ---------------------------------------------------------------------------

def test_docker_daemon_restart_recovers_managed_apps(docker, tmp_path):
    port = free_port()
    store = DeploymentStore(str(tmp_path))
    state = _seed_running_deployment(store, "dep-restart-1", port)
    name = state["container_name"]

    docker.run(name, state["image"], ports={port: 3000},
               env=state["env"], restart=state["restart"])
    assert http_get_text(f"http://127.0.0.1:{port}") == f"fake-app:{name}"

    # The daemon restarts: every container process dies, records remain.
    docker.daemon_restart()
    assert docker.container_status(name) == "exited"
    with pytest.raises(Exception):
        http_get_text(f"http://127.0.0.1:{port}", timeout=2)

    ctx = make_ctx(str(tmp_path), api=None, docker=docker)
    runs_before = len(docker.run_calls)
    summary = reconcile_mod.reconcile(ctx)
    assert summary["reconciled"] == 1
    assert docker.start_calls == [name]
    assert len(docker.run_calls) == runs_before  # restarted, not recreated
    wait_until(lambda: http_get_text(f"http://127.0.0.1:{port}") == f"fake-app:{name}",
               what="app serving again after daemon restart")
    assert "restart" in " ".join(summary["actions"])


def test_docker_daemon_state_loss_recreates_containers(docker, tmp_path):
    port = free_port()
    store = DeploymentStore(str(tmp_path))
    state = _seed_running_deployment(store, "dep-nuke-1", port)
    name = state["container_name"]

    docker.run(name, state["image"], ports={port: 3000},
               env=state["env"], restart=state["restart"])
    assert http_get_text(f"http://127.0.0.1:{port}") == f"fake-app:{name}"

    # Catastrophic daemon state loss: containers vanish entirely.
    docker.daemon_nuke()
    assert docker.ps(all=True) == []

    ctx = make_ctx(str(tmp_path), api=None, docker=docker)
    runs_before = len(docker.run_calls)
    summary = reconcile_mod.reconcile(ctx)
    assert summary["reconciled"] == 1
    # Recreated from the persisted contract — same image, same ports.
    assert len(docker.run_calls) == runs_before + 1
    spec = docker.run_calls[-1]
    assert spec["name"] == name
    assert spec["image"] == state["image"]
    assert spec["ports"] == {port: 3000}
    wait_until(lambda: http_get_text(f"http://127.0.0.1:{port}") == f"fake-app:{name}",
               what="app serving again after daemon state loss")


# ---------------------------------------------------------------------------
# (c) simulated host reboot
# ---------------------------------------------------------------------------

def test_host_reboot_restores_services_from_persisted_state(tmp_path):
    port = free_port()
    work_dir = str(tmp_path)

    # "Before the reboot": a running deployment with a live container.
    docker1 = FakeDocker()
    try:
        store1 = DeploymentStore(work_dir)
        state = _seed_running_deployment(store1, "dep-reboot-1", port)
        name = state["container_name"]
        docker1.run(name, state["image"], ports={port: 3000},
                    env=state["env"], restart=state["restart"])
        assert http_get_text(f"http://127.0.0.1:{port}") == f"fake-app:{name}"
    finally:
        docker1.shutdown_all()

    # "The reboot": everything in memory is gone. A fresh worker boots with
    # a fresh docker view; only the work_dir survived.
    docker2 = FakeDocker()
    try:
        assert docker2.ps(all=True) == []
        store2 = DeploymentStore(work_dir)  # reloaded from disk
        assert [s["deployment_id"] for s in store2.list_all()] == ["dep-reboot-1"]
        assert store2.list_all()[0]["status"] == "running"

        ctx2 = make_ctx(work_dir, api=None, docker=docker2)
        summary = reconcile_mod.reconcile(ctx2)
        assert summary["reconciled"] == 1
        assert summary["already_running"] == 0
        spec = docker2.run_calls[0]
        assert spec["image"] == state["image"]
        assert spec["ports"] == {port: 3000}
        wait_until(
            lambda: http_get_text(f"http://127.0.0.1:{port}") == f"fake-app:{name}",
            what="app serving again after host reboot")
    finally:
        docker2.shutdown_all()
