"""WS-I §56 — concurrent deployments: four apps at once, no interference.

Beyond test_concurrent_deploy.py (W13b §42: port reservation atomicity,
artifact-download integrity, secret isolation) and test_multiapp.py, this
file proves the §56 acceptance criteria through the REAL claim path
(RecoveryPlane over real HTTP, racing hosts) with the REAL dispatcher,
pipeline, health checker and container servers:

  * App A/B/C/D deployed concurrently: no duplicate ports, no duplicate
    containers, no duplicate tasks; every app serves its own traffic;
    correct ownership (claimed_by == completing host), correct terminal
    statuses, and domains recorded on each deployment;
  * concurrent resource requests cannot over-allocate: two simultaneous
    deploys each wanting 768MB on a 1024MB host -> exactly one admitted,
    the other refused with a clear error (the admission reservation is
    atomic under the process-wide resource lock — spec §11);
  * sequential over-allocation is refused the same way: a second deploy
    that does not fit beside an already-running deployment fails clean;
  * concurrent duplicate submissions (the same idempotency_key posted
    twice at once) still yield exactly one deployment + one task per key.

What is REAL: ControlPlaneClient, TaskDispatcher, the deploy pipeline,
deployments.reconcile's inputs (DeploymentStore), the real health
checker, and ThreadDockerClient's real HTTP servers per "container".
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from agent.api import ControlPlaneClient
from deployments import pipeline as pipeline_mod
from executor.dispatcher import TaskDispatcher
from health import collector as health_collector_mod

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
        "image": f"img:{app}-1.0.0",
        "manifest": {
            "name": app,
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {},
            "restart": "unless-stopped",
            "env": {"APP_NAME": app},
            "domains": [f"{app}.example.test"],
        },
        "healthcheck_timeout": 15,
    }
    payload.update(overrides)
    return payload


def _create_deployment(plane: RecoveryPlane, api_key: str, app: str,
                       **payload_overrides) -> tuple[dict, dict]:
    """Agent creates a deployment (no host pinning: any host may claim)."""
    resp = requests.post(
        f"{plane.url}/v1/deployments",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"project_id": f"proj-{app}", "project_name": app,
              "version": "1.0.0",
              "idempotency_key": f"ws5-{app}-{time.monotonic_ns()}"},
        timeout=5)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    deployment, task = data["deployment"], data["task"]
    with plane.lock:
        plane.tasks[task["id"]]["payload"].update(
            _deploy_payload(deployment["id"], app, **payload_overrides))
    return deployment, task


# ---------------------------------------------------------------------------
# four apps, two racing hosts: no duplicates, correct ownership/status
# ---------------------------------------------------------------------------

def test_four_apps_concurrent_no_duplicates_correct_ownership(
        plane, docker, tmp_path):
    """§56: agents create deployments for apps A/B/C/D; two hosts race to
    claim; all four deploy concurrently. Exactly one task, one container
    and one port per app; ownership, terminal statuses and domains are
    all correct; every app serves its own traffic."""
    apps = ["app-a", "app-b", "app-c", "app-d"]
    created: dict = {}
    for app in apps:
        _, api_key = _register_agent(plane, f"ws5-agent-{app}")
        deployment, task = _create_deployment(plane, api_key, app)
        created[app] = (deployment, task, api_key)

    hosts = {}
    for hname in ("ws5-host-1", "ws5-host-2"):
        host, token = _register_host(plane, hname)
        api = ControlPlaneClient(plane.url, token)
        ctx = make_ctx(str(tmp_path / hname), api, docker,
                       host_id=host["id"], host_token=token)
        hosts[host["id"]] = (host, api, ctx)

    # Two hosts race claims until all four tasks are taken.
    claimed: list = []  # (host_id, task)
    claimed_lock = threading.Lock()
    deadline = time.monotonic() + 60

    def claim_loop(host_id: str, api: ControlPlaneClient):
        while time.monotonic() < deadline:
            with claimed_lock:
                if len(claimed) >= 4:
                    return
            task = api.claim_task(host_id, ["docker"], wait=2)
            if task is not None:
                with claimed_lock:
                    claimed.append((host_id, task))

    threads = [threading.Thread(target=claim_loop, args=(hid, api),
                                name=f"ws5-claimer-{hid[:8]}", daemon=True)
               for hid, (_, api, _) in hosts.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=70)
    assert len(claimed) == 4, f"only {len(claimed)} tasks claimed"

    # All four deploy concurrently — each on its claiming host's context,
    # mirroring the production claim loop's shared-ctx thread pool.
    def _dispatch(item):
        host_id, task = item
        _, api, ctx = hosts[host_id]
        return host_id, TaskDispatcher(ctx, api).dispatch(task)

    outcomes: dict = {}
    with ThreadPoolExecutor(max_workers=4,
                            thread_name_prefix="ws5-deploy") as ex:
        for host_id, outcome in ex.map(_dispatch, list(claimed)):
            outcomes[outcome["task_id"]] = (host_id, outcome)
    assert all(o["status"] == "completed" for _, o in outcomes.values()), \
        {tid: o.get("error") for tid, (_, o) in outcomes.items()}

    # No duplicate tasks: each task delivered exactly once, completed once.
    for app in apps:
        _, task, _ = created[app]
        assert plane.claim_deliveries[task["id"]] == 1
        assert plane.tasks[task["id"]]["status"] == "completed"
    assert sum(1 for e in plane.events
               if e["type"] == "task.completed") == 4

    # No duplicate containers or ports: four distinct names, four distinct
    # host ports, four live containers.
    assert len(docker.run_calls) == 4
    names = [c["name"] for c in docker.run_calls]
    assert len(set(names)) == 4, f"duplicate container names: {names}"
    assert len(docker.containers) == 4

    # Correct ownership: the host that claimed each task completed it.
    for host_id, task in claimed:
        assert plane.tasks[task["id"]]["claimed_by"] == host_id
        assert outcomes[task["id"]][0] == host_id

    # Correct terminal statuses on the plane and per-app traffic + domains.
    ports = []
    for app in apps:
        deployment, task, _ = created[app]
        assert plane.deployments[deployment["id"]]["status"] == "running"
        host_id, _ = outcomes[task["id"]]
        _, _, ctx = hosts[host_id]
        state = ctx.deployment_store.load(deployment["id"])
        assert state is not None and state["status"] == "running"
        assert state["domains"] == [f"{app}.example.test"]
        ports.append(state["host_port"])
        body = http_get_text(f"http://127.0.0.1:{state['host_port']}/")
        assert json.loads(body)["app"] == app  # serves its OWN traffic
    assert len(set(ports)) == 4, f"duplicate host ports: {ports}"

    # No leaked reservations after the batch.
    assert pipeline_mod.reserved_port_snapshot() == set()


# ---------------------------------------------------------------------------
# resource over-allocation: concurrent and sequential
# ---------------------------------------------------------------------------

def _resource_payload_overrides(app: str) -> dict:
    return {"manifest": {
        "name": app,
        "runtime": "docker",
        "service": {"port": 3000, "healthcheck": "/health"},
        "resources": {"memory": "768m"},
        "restart": "unless-stopped",
        "env": {"APP_NAME": app},
    }}


def test_concurrent_resource_requests_exactly_one_admitted(
        plane, docker, tmp_path, monkeypatch):
    """§56: two simultaneous deploys each requesting 768MB on a 1024MB
    host. Admission is atomic (check + reserve under one lock), so exactly
    one is admitted and the other is refused with a clear error — the host
    is never over-allocated. The loser leaves no container and no state."""
    monkeypatch.setattr(health_collector_mod, "total_ram_mb", lambda: 1024)
    host, host_token = _register_host(plane, "ws5-res-host")
    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)

    deps_tasks = []
    for i in ("r1", "r2"):
        _, api_key = _register_agent(plane, f"ws5-res-agent-{i}")
        deployment, task = _create_deployment(
            plane, api_key, f"res-{i}",
            **_resource_payload_overrides(f"res-{i}"))
        deps_tasks.append((deployment, task))

    claimed_tasks = []
    for _, task in deps_tasks:
        c = api.claim_task(host["id"], ["docker"], wait=5)
        assert c is not None and c["id"] == task["id"]
        claimed_tasks.append(c)

    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def _run(claimed_task):
        barrier.wait(timeout=30)  # release both dispatches simultaneously
        outcomes[claimed_task["id"]] = \
            TaskDispatcher(ctx, api).dispatch(claimed_task)

    threads = [threading.Thread(target=_run, args=(c,),
                                name=f"ws5-res-{i}", daemon=True)
               for i, c in enumerate(claimed_tasks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert len(outcomes) == 2

    statuses = {tid: o["status"] for tid, o in outcomes.items()}
    assert sorted(statuses.values()) == ["completed", "failed"], statuses
    loser_id = next(tid for tid, s in statuses.items() if s == "failed")
    assert "insufficient reservable memory" in outcomes[loser_id]["error"]
    # The loser never got a container and never wrote a running state.
    assert len(docker.run_calls) == 1
    for deployment, task in deps_tasks:
        state = ctx.deployment_store.load(deployment["id"])
        if task["id"] == loser_id:
            assert state is None or state["status"] != "running"
            assert plane.tasks[task["id"]]["status"] == "failed"
        else:
            assert state is not None and state["status"] == "running"
            wait_until(
                lambda: http_get_text(
                    f"http://127.0.0.1:{state['host_port']}/health") == "ok",
                what="admitted app serving")
    # The refused deploy holds no reservation afterwards.
    assert pipeline_mod.reserved_resources_snapshot() == {}


def test_sequential_resource_over_allocation_refused(
        plane, docker, tmp_path, monkeypatch):
    """§56: a 768MB deployment is running on a 1024MB host; a second 768MB
    deployment is refused cleanly — total reservations never exceed the
    host, and the running app is untouched."""
    monkeypatch.setattr(health_collector_mod, "total_ram_mb", lambda: 1024)
    host, host_token = _register_host(plane, "ws5-seq-host")
    api = ControlPlaneClient(plane.url, host_token)
    ctx = make_ctx(str(tmp_path), api, docker, host_id=host["id"],
                   host_token=host_token)

    _, key_a = _register_agent(plane, "ws5-seq-agent-a")
    dep_a, task_a = _create_deployment(plane, key_a, "seq-a",
                                       **_resource_payload_overrides("seq-a"))
    claimed_a = api.claim_task(host["id"], ["docker"], wait=5)
    assert TaskDispatcher(ctx, api).dispatch(claimed_a)["status"] == \
        "completed"
    state_a = ctx.deployment_store.load(dep_a["id"])
    assert state_a["status"] == "running"

    _, key_b = _register_agent(plane, "ws5-seq-agent-b")
    dep_b, task_b = _create_deployment(plane, key_b, "seq-b",
                                       **_resource_payload_overrides("seq-b"))
    claimed_b = api.claim_task(host["id"], ["docker"], wait=5)
    outcome_b = TaskDispatcher(ctx, api).dispatch(claimed_b)
    assert outcome_b["status"] == "failed"
    assert "insufficient reservable memory" in outcome_b["error"]
    assert plane.tasks[task_b["id"]]["status"] == "failed"

    # The first app is untouched and still serving; no second container.
    assert len(docker.run_calls) == 1
    assert http_get_text(
        f"http://127.0.0.1:{state_a['host_port']}/health") == "ok"
    assert pipeline_mod.reserved_resources_snapshot() == {}


# ---------------------------------------------------------------------------
# concurrent duplicate submissions: one deployment + one task per key
# ---------------------------------------------------------------------------

def test_concurrent_duplicate_submissions_no_duplicate_tasks(plane):
    """§56: the same idempotency_key POSTed twice concurrently (timeout
    retry arriving while the original is in flight) still yields exactly
    one deployment and one task per key — one 201, one 200 replay."""
    _, api_key = _register_agent(plane, f"ws5-dup-{time.monotonic_ns()}")
    headers = {"Authorization": f"Bearer {api_key}"}
    keys = [f"ws5-dup-key-{i}-{time.monotonic_ns()}" for i in range(4)]
    barrier = threading.Barrier(8)
    results: dict = {}

    def _post(key, slot):
        barrier.wait(timeout=30)  # all eight POSTs hit at once
        resp = requests.post(
            f"{plane.url}/v1/deployments", headers=headers,
            json={"project_id": f"proj-dup-{slot}",
                  "project_name": f"dup-{slot}", "version": "1.0.0",
                  "idempotency_key": key},
            timeout=15)
        results.setdefault(key, []).append(resp.status_code)

    threads = [threading.Thread(target=_post, args=(key, i),
                                name=f"ws5-dup-{i}-{j}", daemon=True)
               for i, key in enumerate(keys) for j in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    for key in keys:
        assert sorted(results[key]) == [200, 201], \
            f"key {key}: expected one 201 + one 200 replay, got {results[key]}"
        with plane.lock:
            deps = [d for d in plane.deployments.values()
                    if d.get("idempotency_key") == key]
            tasks = [t for t in plane.tasks.values()
                     if t.get("idempotency_key") == f"deploy:{key}"]
        assert len(deps) == 1, f"duplicate deployments for {key}"
        assert len(tasks) == 1, f"duplicate tasks for {key}"
