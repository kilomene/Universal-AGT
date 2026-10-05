"""W13b §41 — multi-application isolation on a single host.

Deploys three apps (A, B, C) through the REAL deployment pipeline
(deployments.pipeline.deploy) against thread-backed fake containers, then
verifies the isolation contract:

  * separate containers, separate deployment IDs, separate host ports
    (from the worker's port registry view: DeploymentStore.used_host_ports)
  * separate log streams (per-deployment log files never interleave)
  * separate domains (ingress.build_routes maps each hostname to its own
    app's host port)
  * independent restart: restarting B leaves A and C serving
  * independent rollback: rolling back B leaves A and C untouched
  * failure isolation: killing A's container (or failing A's healthcheck)
    leaves B and C running; A's failure is reported via the healthcheck
    task path without touching the others.

Determinism: no sleeps-as-synchronization. The fake containers are real
HTTP servers on threads; the production health checker polls them.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent.context import WorkerContext
from deployments import pipeline
from deployments.state import DeploymentStore
from executor import handlers
from ingress import build_routes
from logs.store import LogStore

from fake_thread_docker import ThreadDockerClient


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------
@dataclass
class _Config:
    work_dir: str
    apps_dir: str
    host_token: str = "host-test-token-not-real"


class _NoApi:
    """No artifact / secrets calls on the §41 path (no get_project_secrets
    attribute either, so the pipeline skips the fetch branch)."""


@pytest.fixture
def rig(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    docker = ThreadDockerClient()
    config = _Config(work_dir=str(work), apps_dir=str(tmp_path / "apps"))
    ctx = WorkerContext(
        config=config,
        api=_NoApi(),
        docker=docker,
        log_store=LogStore(str(tmp_path / "logs")),
        deployment_store=DeploymentStore(str(work)),
    )
    yield ctx, docker
    docker.shutdown_all()


APPS = {
    "a": {"project_id": "11111111-1111-4111-8111-111111111111",
          "project_name": "app-a", "domain": "a.example-app.net"},
    "b": {"project_id": "22222222-2222-4222-8222-222222222222",
          "project_name": "app-b", "domain": "b.example-app.net"},
    "c": {"project_id": "33333333-3333-4333-8333-333333333333",
          "project_name": "app-c", "domain": "c.example-app.net"},
}


def _deploy_task(key, version="1.0.0", deployment_id=None, task_id=None,
                 env_extra=None, health_fail=False):
    app = APPS[key]
    env = {"APP_NAME": app["project_name"], "APP_VERSION": version}
    if health_fail:
        env["HEALTH_FAIL"] = "1"
    if env_extra:
        env.update(env_extra)
    manifest = {
        "name": app["project_name"],
        "runtime": "docker",
        "service": {"port": 3000, "healthcheck": "/health"},
        "env": env,
        "domains": [app["domain"]],
    }
    return {
        "id": task_id or f"task-{key}-{uuid.uuid4().hex[:8]}",
        "type": "deploy",
        "payload": {
            "project_id": app["project_id"],
            "project_name": app["project_name"],
            "version": version,
            "deployment_id": deployment_id or f"dep-{key}-{uuid.uuid4().hex[:8]}",
            "manifest": manifest,
            "image": f"prebuilt/{app['project_name']}:{version}",
            "healthcheck_timeout": 20,
        },
    }


def _deploy(rig, key, **kw):
    ctx, _docker = rig
    task = _deploy_task(key, **kw)
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"
    return task["payload"]["deployment_id"], result


def _identity(docker, port):
    status, body = docker.http_get(port, "/")
    assert status == 200
    return json.loads(body)["app"]


def _states_by_key(rig, deps):
    ctx, _ = rig
    return {k: ctx.deployment_store.load(d) for k, d in deps.items()}


# ---------------------------------------------------------------------------
# Separate containers / IDs / ports / logs
# ---------------------------------------------------------------------------
def test_three_apps_get_separate_containers_ports_ids_and_logs(rig):
    ctx, docker = rig
    deps = {}
    for key in ("a", "b", "c"):
        dep_id, _ = _deploy(rig, key)
        deps[key] = dep_id

    states = _states_by_key(rig, deps)
    assert all(s["status"] == "running" for s in states.values())

    # Separate deployment IDs, container names, host ports.
    assert len({deps[k] for k in deps}) == 3
    names = [states[k]["container_name"] for k in deps]
    assert len(set(names)) == 3
    ports = [states[k]["host_port"] for k in deps]
    assert len(set(ports)) == 3
    assert set(ports) == ctx.deployment_store.used_host_ports()

    # Each app serves its own identity on its own port.
    for key in ("a", "b", "c"):
        assert _identity(docker, states[key]["host_port"]) == APPS[key]["project_name"]

    # Separate log streams: each deployment log mentions only its own app.
    for key in ("a", "b", "c"):
        text = Path(ctx.log_store.deployment_log_path(deps[key])).read_text()
        assert APPS[key]["project_name"] in text
        for other in ("a", "b", "c"):
            if other != key:
                assert APPS[other]["project_name"] not in text, \
                    f"log stream for {key} interleaved with {other}"


def test_domain_mapping_per_app(rig):
    deps = {k: _deploy(rig, k)[0] for k in ("a", "b", "c")}
    states = _states_by_key(rig, deps)

    entries = [{"deployment_id": deps[k], "hostname": APPS[k]["domain"],
                "ingress": "tunnel"} for k in ("a", "b", "c")]
    routes, skipped = build_routes(list(states.values()), entries)

    assert skipped == []
    by_host = {r["hostname"]: r for r in routes}
    assert set(by_host) == {APPS[k]["domain"] for k in ("a", "b", "c")}
    for key in ("a", "b", "c"):
        route = by_host[APPS[key]["domain"]]
        assert route["target_host"] == "127.0.0.1"
        assert route["target_port"] == states[key]["host_port"]

    # A 'direct' entry is the operator's own ingress: never a tunnel route.
    entries.append({"deployment_id": deps["a"],
                    "hostname": "direct.example-app.net", "ingress": "direct"})
    routes2, skipped2 = build_routes(list(states.values()), entries)
    assert {r["hostname"] for r in routes2} == set(by_host)
    assert any(s["hostname"] == "direct.example-app.net" for s in skipped2)


# ---------------------------------------------------------------------------
# Independent restart
# ---------------------------------------------------------------------------
def test_restart_b_leaves_a_and_c_serving(rig):
    ctx, docker = rig
    deps = {k: _deploy(rig, k)[0] for k in ("a", "b", "c")}
    states = _states_by_key(rig, deps)

    out = handlers.handle_restart(
        ctx, {"id": "task-restart-b",
              "payload": {"deployment_id": deps["b"]}})
    assert out["status"] == "restarted"
    assert out["container"] == states["b"]["container_name"]
    assert states["b"]["container_name"] in docker.restart_calls

    # A and C were never restarted and still serve their own identity.
    assert states["a"]["container_name"] not in docker.restart_calls
    assert states["c"]["container_name"] not in docker.restart_calls
    for key in ("a", "b", "c"):
        assert _identity(docker, states[key]["host_port"]) == APPS[key]["project_name"]
    # Registry view unchanged.
    assert ctx.deployment_store.used_host_ports() == \
        {states[k]["host_port"] for k in ("a", "b", "c")}


# ---------------------------------------------------------------------------
# Independent rollback
# ---------------------------------------------------------------------------
def test_rollback_b_leaves_a_and_c_untouched(rig):
    ctx, docker = rig
    deps = {k: _deploy(rig, k)[0] for k in ("a", "b", "c")}

    # B v2 (healthy) supersedes B v1.
    dep_b2, _ = _deploy(rig, "b", version="2.0.0")
    st_b1 = ctx.deployment_store.load(deps["b"])
    st_b2 = ctx.deployment_store.load(dep_b2)
    assert st_b1["status"] == "superseded"
    assert st_b2["status"] == "running"
    assert _identity(docker, st_b2["host_port"]) == "app-b"

    restarts_before = list(docker.restart_calls)
    stops_before = list(docker.stop_calls)
    starts_before = list(docker.start_calls)

    out = handlers.handle_rollback(
        ctx, {"id": "task-rollback-b",
              "payload": {"deployment_id": dep_b2,
                          "target_deployment_id": deps["b"]}})
    assert out["status"] == "rolled_back"
    assert out["rolled_back_to"] == deps["b"]

    # B is back on v1 and serving; A and C untouched.
    assert _identity(docker, st_b1["host_port"]) == "app-b"
    assert ctx.deployment_store.load(dep_b2)["status"] == "rolled_back"
    assert ctx.deployment_store.load(deps["b"])["status"] == "running"
    # The rollback tore down exactly B v2's container (stop+rm) and
    # restarted B v1's own container — nothing of A or C was stopped or
    # restarted.
    assert docker.stop_calls[len(stops_before):] == [st_b2["container_name"]]
    assert docker.restart_calls[len(restarts_before):] == []
    # B v1's own container was started again (rollback restore path).
    assert docker.start_calls[len(starts_before):] == [st_b1["container_name"]]
    for key in ("a", "c"):
        st = ctx.deployment_store.load(deps[key])
        assert st["status"] == "running"
        assert _identity(docker, st["host_port"]) == APPS[key]["project_name"]
        name = st["container_name"]
        assert name not in docker.stop_calls[len(stops_before):]
        assert name not in docker.restart_calls


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------
def test_killed_container_isolated_and_reported(rig):
    ctx, docker = rig
    deps = {k: _deploy(rig, k)[0] for k in ("a", "b", "c")}
    states = _states_by_key(rig, deps)

    # Kill A's container outright (simulates a crash / OOM / docker kill).
    docker.rm(states["a"]["container_name"], force=True)
    assert docker.container_status(states["a"]["container_name"]) is None

    # B and C keep serving, unaffected.
    for key in ("b", "c"):
        assert _identity(docker, states[key]["host_port"]) == APPS[key]["project_name"]
        out = handlers.handle_healthcheck(
            ctx, {"id": f"task-hc-{key}",
                  "payload": {"deployment_id": deps[key], "timeout": 3}})
        assert out["health_status"] == "healthy"

    # A's failure is reported through the healthcheck task path.
    out = handlers.handle_healthcheck(
        ctx, {"id": "task-hc-a",
              "payload": {"deployment_id": deps["a"], "timeout": 3}})
    assert out["health_status"] == "unhealthy"
    # A's deployment record is still there (nothing was torn down for B/C).
    assert ctx.deployment_store.load(deps["a"])["status"] == "running"


def test_unhealthy_container_isolated_and_reported(rig):
    ctx, docker = rig
    deps = {k: _deploy(rig, k)[0] for k in ("a", "b", "c")}
    states = _states_by_key(rig, deps)

    # A's app starts failing its healthcheck while the container lives.
    docker.set_healthy(states["a"]["container_name"], False)

    for key in ("b", "c"):
        out = handlers.handle_healthcheck(
            ctx, {"id": f"task-hc-{key}",
                  "payload": {"deployment_id": deps[key], "timeout": 3}})
        assert out["health_status"] == "healthy"
    out = handlers.handle_healthcheck(
        ctx, {"id": "task-hc-a",
              "payload": {"deployment_id": deps["a"], "timeout": 3}})
    assert out["health_status"] == "unhealthy"


def test_failed_deploy_of_a_leaves_b_and_c_untouched(rig):
    ctx, docker = rig
    deps = {k: _deploy(rig, k)[0] for k in ("a", "b", "c")}
    states = _states_by_key(rig, deps)
    ports_before = {states[k]["host_port"] for k in deps}

    # A v2 whose /health never passes: the pipeline must fail it honestly
    # (and roll back to A v1) without touching B or C.
    bad_task = _deploy_task("a", version="2.0.0", health_fail=True)
    with pytest.raises(pipeline.DeployError):
        pipeline.deploy(ctx, bad_task)

    # B and C still serve; A v1 still serves on its original port.
    for key in ("a", "b", "c"):
        assert _identity(docker, states[key]["host_port"]) == APPS[key]["project_name"]
    assert ctx.deployment_store.used_host_ports() == ports_before

    # The failed deployment is recorded as such and holds no port.
    failed = ctx.deployment_store.load(bad_task["payload"]["deployment_id"])
    assert failed["status"] in ("failed", "rolled_back")
    assert failed["host_port"] is None
    # A v1 is still the live deployment.
    assert ctx.deployment_store.load(deps["a"])["status"] == "running"
