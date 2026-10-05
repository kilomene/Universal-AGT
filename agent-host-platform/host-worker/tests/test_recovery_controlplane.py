"""W13a §40 — control-plane outage: the worker rides it out.

  * The API dies mid-task: the already-deployed app keeps serving traffic
    (the worker never needs the control plane to keep containers alive),
    the worker's claim/progress calls fail with WorkerAPIError, and the
    loops back off instead of hot-spinning.
  * The API returns: the worker reconnects, long-polling resumes, and the
    in-flight task is NOT claimed twice — state resynchronizes with
    exactly one claim delivery and one completion.
  * claim_loop / heartbeat_loop consult agent.backoff.backoff_delay with a
    growing failure count and reset on success (asserted via a recording
    spy; real exponential values asserted, waits clamped for speed).

What is REAL: ControlPlaneClient, agent.main.claim_loop/heartbeat_loop,
agent.backoff, TaskDispatcher, and FakeDocker's real server subprocesses.
The plane is the sanctioned test double (see recovery_harness.py).
"""
from __future__ import annotations

import threading
import time

import pytest
import requests

from agent import backoff as backoff_mod
from agent import main as main_mod
from agent.api import ControlPlaneClient, WorkerAPIError, build_progress_payload
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
    plane = RecoveryPlane().start()
    yield plane
    plane.shutdown()


@pytest.fixture()
def docker():
    docker = FakeDocker()
    yield docker
    docker.shutdown_all()


def _register_host(plane: RecoveryPlane, name: str = "w13a-cp-host"):
    resp = requests.post(
        f"{plane.url}/v1/hosts/register",
        json={"name": name, "capabilities": ["docker"],
              "worker_version": "test"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["host"], body["host_token"]


def _agent_task(plane: RecoveryPlane, task_type: str = "system-info") -> str:
    agent_resp = requests.post(
        f"{plane.url}/v1/agents/register",
        json={"name": f"w13a-cp-agent-{time.monotonic_ns()}", "type": "Muse"},
        timeout=5)
    agent_key = agent_resp.json()["api_key"]
    task_resp = requests.post(
        f"{plane.url}/v1/tasks",
        headers={"Authorization": f"Bearer {agent_key}"},
        json={"type": task_type, "payload": {}}, timeout=5)
    assert task_resp.status_code == 201, task_resp.text
    return task_resp.json()["task"]["id"]


class _RecEvent(threading.Event):
    """threading.Event that records wait() timeouts and clamps them so
    backoff-driven loops run fast while still receiving REAL delays."""

    def __init__(self):
        super().__init__()
        self.waits: list = []

    def wait(self, timeout=None):  # noqa: D102
        self.waits.append(timeout)
        return super().wait(min(timeout, 0.05) if timeout else 0)


def _spy_backoff(monkeypatch):
    """Record backoff_delay inputs/outputs; return real (growing) values but
    the loops only ever sleep the clamped _RecEvent waits."""
    calls: list = []
    real = backoff_mod.backoff_delay

    def spy(failures: int, **kw):
        delay = real(failures, **kw)
        calls.append((failures, delay))
        return delay

    monkeypatch.setattr(backoff_mod, "backoff_delay", spy)
    return calls


# ---------------------------------------------------------------------------
# API dies mid-task -> app keeps running -> reconnect -> no duplicate claim
# ---------------------------------------------------------------------------

def test_controlplane_outage_midtask_app_survives_and_state_resyncs(
        plane, docker, tmp_path):
    host, host_token = _register_host(plane)
    task_id = _agent_task(plane)

    # A deployed app is serving traffic (independent of the control plane).
    app_port = free_port()
    docker.run("uaht-outage-app", "uaht-web:1.0.0", ports={app_port: 3000})
    assert http_get_text(f"http://127.0.0.1:{app_port}") == \
        "fake-app:uaht-outage-app"

    api = ControlPlaneClient(plane.url, host_token)
    claimed = api.claim_task(host["id"], ["docker"], wait=5)
    assert claimed is not None and claimed["id"] == task_id

    # The control plane dies mid-task.
    plane.shutdown()
    with pytest.raises(WorkerAPIError):
        api.progress(task_id, build_progress_payload("claimed", "running"))

    # The deployed app is unaffected: still serving while the plane is down.
    assert http_get_text(f"http://127.0.0.1:{app_port}") == \
        "fake-app:uaht-outage-app"

    # The control plane returns (same URL — the worker never reconfigures).
    plane.start()
    wait_until(lambda: api.heartbeat(host["id"], {})["pending_tasks"] == 0,
               what="heartbeat succeeds after outage")

    # Reconnect: the in-flight task is still ours (lease intact), so a fresh
    # claim long-poll finds nothing new — it is NOT handed out again.
    again = api.claim_task(host["id"], ["docker"], wait=1)
    assert again is None
    assert plane.claim_deliveries[task_id] == 1

    # The worker resumes exactly where it left off and completes the task.
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)
    outcome = TaskDispatcher(ctx, api).dispatch(claimed)
    assert outcome["status"] == "completed"
    assert plane.tasks[task_id]["status"] == "completed"
    assert plane.claim_deliveries[task_id] == 1  # claimed exactly once
    assert sum(1 for e in plane.events if e["type"] == "task.completed") == 1

    # And the app never stopped serving through the whole outage.
    assert http_get_text(f"http://127.0.0.1:{app_port}") == \
        "fake-app:uaht-outage-app"


# ---------------------------------------------------------------------------
# claim_loop: backoff consulted with growing failures, reset on success
# ---------------------------------------------------------------------------

def test_claim_loop_backs_off_with_growing_delays_and_resets(plane, tmp_path,
                                                             monkeypatch):
    host, host_token = _register_host(plane)
    backoff_calls = _spy_backoff(monkeypatch)

    attempts: list = []

    class FlakyAPI:
        def claim_task(self, host_id, capabilities, wait):
            attempts.append(time.monotonic())
            if len(attempts) <= 3:
                raise WorkerAPIError("control plane down")
            return None  # reachable again: 204, resets the failure counter

    api = FlakyAPI()
    ctx = make_ctx(str(tmp_path), api, docker=None, host_id=host["id"],
                   host_token=host_token)
    stop = _RecEvent()
    thread = threading.Thread(target=main_mod.claim_loop, args=(ctx, stop),
                              name="test-claim-loop", daemon=True)
    thread.start()
    try:
        wait_until(lambda: len(attempts) >= 4, timeout=15,
                   what="4 claim attempts")
        wait_until(lambda: len(backoff_calls) >= 3, timeout=5,
                   what="3 backoff computations")
    finally:
        stop.set()
        thread.join(timeout=10)

    # The loop consulted backoff with the consecutive-failure count 1,2,3.
    failures = [f for f, _ in backoff_calls]
    assert failures == [1, 2, 3]
    # Real exponential growth (base 5s, jitter cannot reorder the doublings).
    d1, d2, d3 = [d for _, d in backoff_calls]
    assert d1 < d2 < d3
    assert 3.0 <= d1 <= 7.0 and 7.0 <= d2 <= 14.0 and 14.0 <= d3 <= 28.0
    # The waits the loop actually took were the backoff values (no hot spin:
    # every recorded wait is a real multi-second backoff, clamped only by
    # the test event).
    assert stop.waits[:3] == [d1, d2, d3]
    # After the 4th attempt succeeded the counter reset: no 4th backoff.
    assert len(backoff_calls) == 3


# ---------------------------------------------------------------------------
# heartbeat_loop: backoff on failure, reconnect resets
# ---------------------------------------------------------------------------

def test_heartbeat_loop_backs_off_and_reconnects(plane, tmp_path, monkeypatch):
    host, host_token = _register_host(plane)
    backoff_calls = _spy_backoff(monkeypatch)

    calls: list = []

    class FlakyAPI:
        def heartbeat(self, host_id, payload):
            calls.append(time.monotonic())
            if len(calls) <= 2:
                raise WorkerAPIError("control plane down")
            return {"pending_tasks": 0}

    api = FlakyAPI()
    ctx = make_ctx(str(tmp_path), api, docker=None, host_id=host["id"],
                   host_token=host_token)
    stop = _RecEvent()
    thread = threading.Thread(target=main_mod.heartbeat_loop,
                              args=(ctx, stop),
                              name="test-heartbeat-loop", daemon=True)
    thread.start()
    try:
        wait_until(lambda: len(calls) >= 3, timeout=15,
                   what="3 heartbeat attempts")
        wait_until(lambda: len(backoff_calls) >= 2, timeout=5,
                   what="2 heartbeat backoffs")
    finally:
        stop.set()
        thread.join(timeout=10)

    assert [f for f, _ in backoff_calls] == [1, 2]
    d1, d2 = [d for _, d in backoff_calls]
    assert d1 < d2  # exponential growth, not a fixed retry sleep
    assert stop.waits[:2] == [d1, d2]
    # The third heartbeat succeeded — the loop survived the outage and the
    # failure counter reset (no further backoff was computed).
    assert len(backoff_calls) == 2


# ---------------------------------------------------------------------------
# concurrent claims: exactly one winner (§45, via the sanctioned harness —
# the production FOR UPDATE SKIP LOCKED cannot run under pg-mem, so the
# atomic-claim contract is covered here, as in test_e2e.py)
# ---------------------------------------------------------------------------

def test_concurrent_claims_exactly_one_winner(plane):
    host, host_token = _register_host(plane)
    task_id = _agent_task(plane)

    results: list = []
    errors: list = []

    def worker():
        try:
            api = ControlPlaneClient(plane.url, host_token)
            results.append(api.claim_task(host["id"], ["docker"], wait=0))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, name=f"claimer-{i}")
               for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors
    winners = [r for r in results if r is not None]
    assert len(results) == 16
    assert len(winners) == 1  # exactly one winner
    assert winners[0]["id"] == task_id
    assert plane.claim_deliveries[task_id] == 1
    assert plane.tasks[task_id]["claimed_by"] == host["id"]
