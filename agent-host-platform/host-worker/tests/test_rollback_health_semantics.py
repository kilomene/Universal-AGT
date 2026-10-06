"""Part 2 regression tests: rollback health semantics.

Acceptance set for the "fix rollback health semantics" pass. The core
rule pinned here: a rollback succeeds ONLY when the restored target's
health is PROVEN healthy (``target_health_status == "healthy"``).
``unhealthy`` -> failed. ``unknown`` -> failed (the pre-existing
``rollback_failed`` state — no new state invented). Port settlement
runs ONLY after the target is proven healthy; a settle failure is a
durable failure, never reported as success.

The 15 tests (spec Part 2H):
 1. healthy -> success
 2. unhealthy -> fail
 3. unknown -> fail
 4. no-healthcheck-port -> fail (+ no secrets in the failure record)
 5. same-port -> success
 6. third-party port collision -> safe fail
 7. missing target -> fail
 8. missing image -> fail
 9. port-settlement-failure -> durable fail
10. worker-restart-during-rollback -> safe recovery
11. repeated rollback -> idempotent
12. rollback-then-deploy -> works
13. failed rollback doesn't corrupt port registry
14. failed rollback doesn't falsely mark target current
15. event history accuracy (the 2C event shape)
"""
import socket

import pytest

from deployments import reconcile as reconcile_mod
from deployments.state import DeploymentStore
from executor import handlers
from executor.handlers import HandlerError
from health import checker as health_checker
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.images = set()
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []
        self.run_calls = []
        self.ops = []

    def container_exists(self, name):
        return name in self.containers

    def container_status(self, name):
        info = self.containers.get(name)
        if info is None:
            return None
        return "running" if info.get("running") else "exited"

    def image_exists(self, tag):
        return tag in self.images

    def ps(self, all=False):
        rows = []
        for name, info in self.containers.items():
            running = bool(info.get("running"))
            if all or running:
                rows.append({"Names": "/" + name,
                             "State": "running" if running else "exited",
                             "Ports": info.get("ports_str", "")})
        return rows

    def start(self, name, timeout=120):
        self.ops.append(("start", name))
        self.start_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = True

    def stop(self, name, timeout_secs=10, timeout=120):
        self.ops.append(("stop", name))
        self.stop_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self.ops.append(("rm", name))
        self.rm_calls.append(name)
        self.containers.pop(name, None)

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart=None, timeout=120):
        self.ops.append(("run", name))
        self.run_calls.append({"name": name, "image": image, "ports": ports,
                               "env": env, "memory": memory, "cpus": cpus,
                               "restart": restart})
        self.containers[name] = {"image": image, "running": True}


class FakeSession:
    """Programmable control-plane session for the settle-rollback-ports
    POST."""

    def __init__(self, fail_with=None, status=200, body=None):
        self.posts = []
        self.fail_with = fail_with
        self.status = status
        self.body = body if body is not None else {
            "released": 1, "restored": 1, "skipped": 0}

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self.fail_with is not None:
            raise self.fail_with
        session = self

        class Resp:
            status_code = session.status

            def json(self):
                return dict(session.body)

        return Resp()


class FakeAPI:
    def __init__(self, session=None):
        if session is not None:
            self.base_url = "https://control-plane.example.com"
            self.session = session


class FakeConfig:
    def __init__(self, work_dir):
        self.work_dir = str(work_dir)
        self.apps_dir = str(work_dir / "apps")


class FakeCtx:
    def __init__(self, tmp_path, docker, api=None):
        self.config = FakeConfig(tmp_path)
        self.api = api or FakeAPI()
        self.docker = docker
        self.log_store = LogStore(str(tmp_path / "logs"))
        self.deployment_store = DeploymentStore(str(tmp_path))
        self.scrub = lambda s: s
        self.logged = []

    def log(self, task_id, line):
        line = self.scrub(line)
        self.logged.append((task_id, line))
        return line

    def require_docker(self):
        return self.docker


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _hold_port():
    """A real listening socket: makes the OS-bind probe genuinely fail."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _hc(monkeypatch, healthy_ports):
    """wait_for_healthcheck fake: healthy only on the given ports."""
    seen = []

    def _fake(port, path="/", timeout_secs=60, log=None):
        seen.append((int(port), path))
        return int(port) in healthy_ports

    monkeypatch.setattr(health_checker, "wait_for_healthcheck", _fake)
    return seen


def _state(dep_id, name, project="proj-1", status="running", **overrides):
    state = {
        "deployment_id": dep_id,
        "task_id": "task-0",
        "project_id": project,
        "project_name": "my-api",
        "version": "9.9.9",
        "container_name": name,
        "image": "img:x",
        "runtime": "docker",
        "status": status,
        "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    }
    state.update(overrides)
    return state


def _state_with_port(dep_id, name, port, status="superseded"):
    return _state(dep_id, name, status=status, host_port=port,
                  container_port=3000, ports={str(port): 3000},
                  healthcheck_path="/health", health_status="unknown")


def _rollback_task(dep_id, target_id):
    return {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": dep_id,
                        "target_deployment_id": target_id}}


# ---------------------------------------------------------------------------
# 1. healthy -> success
# ---------------------------------------------------------------------------
def test_healthy_target_rollback_succeeds(tmp_path, monkeypatch):
    port = _free_port()
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state_with_port("dep-target", "c-target",
                                               port))
    _hc(monkeypatch, {port})

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    assert result["status"] == "rolled_back"
    assert result["rollback_status"] == "succeeded"
    assert result["target_health_status"] == "healthy"
    assert result["rolled_back_to"] == "dep-target"
    assert result["ports_settled"] is True
    assert len(session.posts) == 1
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["status"] == "running"
    assert tgt["health_status"] == "healthy"
    assert tgt["rollback_status"] == "succeeded"
    assert ctx.deployment_store.used_host_ports() == {port}


# ---------------------------------------------------------------------------
# 2. unhealthy -> fail
# ---------------------------------------------------------------------------
def test_unhealthy_target_rollback_fails(tmp_path, monkeypatch):
    port = _free_port()
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state_with_port("dep-target", "c-target",
                                               port))
    _hc(monkeypatch, set())  # the restored target fails its health check

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    assert result["status"] == "rollback_failed"
    assert result["rollback_status"] == "failed"
    assert result["target_health_status"] == "unhealthy"
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["health_status"] == "unhealthy"
    assert tgt["rollback_status"] == "failed"
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "rollback_failed"
    assert cur["rollback_status"] == "failed"
    assert "health check failed" in cur["rollback_error"]
    # Part 2E: no port settlement for an unhealthy target
    assert session.posts == []
    assert cur["port_settle"]["settled"] is False
    assert "not verified" in cur["port_settle"]["reason"]


# ---------------------------------------------------------------------------
# 3. unknown -> fail
# ---------------------------------------------------------------------------
def test_unknown_target_rollback_fails(tmp_path, monkeypatch):
    """The target restores but has no host port, so its health cannot be
    verified — unknown is never a success."""
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    # no host_port / ports at all: nothing health-checkable
    ctx.deployment_store.save(_state("dep-target", "c-target",
                                     status="superseded",
                                     health_status="unknown"))
    _hc(monkeypatch, set())

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    assert result["status"] == "rollback_failed"
    assert result["rollback_status"] == "failed"
    assert result["target_health_status"] == "unknown"
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["health_status"] == "unknown"
    assert tgt["rollback_status"] == "failed"
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "rollback_failed"
    assert cur["rollback_status"] == "failed"
    assert session.posts == []  # no settle for an unverified target


# ---------------------------------------------------------------------------
# 4. no-healthcheck-port -> fail (+ no secrets in the failure record)
# ---------------------------------------------------------------------------
def test_target_with_no_healthcheckable_port_fails(tmp_path, monkeypatch):
    """Part 2C's example: V1 is restored but exposes no health-checkable
    endpoint. The rollback persists rollback_failed with the exact event
    shape — and no secrets leak into it."""
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state(
        "dep-target", "c-target", status="superseded",
        health_status="unknown",
        env={"DB_PASSWORD": "supersecret-xyz-123"}))
    _hc(monkeypatch, set())

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    # the restore itself worked...
    assert "c-target" in docker.start_calls
    assert docker.containers["c-target"]["running"] is True
    # ...but with nothing to check against the rollback FAILS
    assert result["status"] == "rollback_failed"
    assert result["rollback_status"] == "failed"
    assert result["target_health_status"] == "unknown"
    # the 2C event shape, carried on the result + durable row
    assert result["target_health_status"] == "unknown"
    assert result["rollback_status"] == "failed"
    cur = ctx.deployment_store.load("dep-cur")
    assert "target health could not be verified" in cur["rollback_error"]
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["health_status"] == "unknown"
    assert tgt["rollback_status"] == "failed"
    # no secrets anywhere in the failure record or the logs
    assert "supersecret-xyz-123" not in cur["rollback_error"]
    assert "supersecret-xyz-123" not in result.get("rollback_error", "")
    for _, line in ctx.logged:
        assert "supersecret-xyz-123" not in line


# ---------------------------------------------------------------------------
# 5. same-port -> success
# ---------------------------------------------------------------------------
def test_same_port_rollback_succeeds(tmp_path, monkeypatch):
    """V1 and V2 both on port P: V2 (running) holds P until its teardown.
    The rollback must NOT reject this — V2's port claim is expected —
    and registry P -> V1 afterwards."""
    squat, port = _hold_port()
    try:
        docker = FakeDocker()
        docker.containers["c-v1"] = {"running": False,
                                     "ports_str": f"0.0.0.0:{port}->3000/tcp"}
        docker.containers["c-v2"] = {"running": True,
                                     "ports_str": f"0.0.0.0:{port}->3000/tcp"}
        session = FakeSession()
        ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
        ctx.deployment_store.save(_state_with_port("dep-v1", "c-v1", port))
        ctx.deployment_store.save(_state("dep-v2", "c-v2",
                                         host_port=port, container_port=3000,
                                         ports={str(port): 3000},
                                         healthcheck_path="/health"))
        _hc(monkeypatch, {port})

        result = handlers.handle_rollback(ctx, _rollback_task("dep-v2",
                                                              "dep-v1"))

        assert result["status"] == "rolled_back"
        assert result["rollback_status"] == "succeeded"
        assert result["rolled_back_to"] == "dep-v1"
        # teardown strictly before restore: two containers never hold P
        assert docker.ops.index(("rm", "c-v2")) < \
            docker.ops.index(("start", "c-v1"))
        assert docker.containers["c-v1"]["running"] is True
        assert "c-v2" not in docker.containers
        # registry: P -> V1, never still attributed to V2
        assert ctx.deployment_store.used_host_ports() == {port}
        assert ctx.deployment_store.load("dep-v1")["health_status"] == \
            "healthy"
        assert len(session.posts) == 1
        assert session.posts[0]["json"] == {"target_deployment_id": "dep-v1"}
    finally:
        squat.close()


# ---------------------------------------------------------------------------
# 6. third-party port collision -> safe fail
# ---------------------------------------------------------------------------
def test_third_party_port_collision_safe_fail(tmp_path, monkeypatch):
    """An UNRELATED listener holds the target's port: the rollback is
    refused BEFORE the current deployment is touched, recorded durably
    as a failure — the unrelated application is never overwritten."""
    squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squat.bind(("0.0.0.0", 0))
    squat.listen(1)
    port = squat.getsockname()[1]
    try:
        docker = FakeDocker()
        docker.containers["c-cur"] = {"running": True}
        docker.containers["c-target"] = {"running": False}
        ctx = FakeCtx(tmp_path, docker)
        ctx.deployment_store.save(_state("dep-cur", "c-cur"))
        ctx.deployment_store.save(_state_with_port("dep-target", "c-target",
                                                   port))
        _hc(monkeypatch, {port})

        with pytest.raises(HandlerError, match="not free"):
            handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                         "dep-target"))

        # the healthy current deployment was never touched...
        assert docker.stop_calls == []
        assert docker.rm_calls == []
        assert docker.start_calls == []
        assert docker.containers["c-cur"]["running"] is True
        # ...the failed attempt is recorded durably...
        cur = ctx.deployment_store.load("dep-cur")
        assert cur["status"] == "running"
        assert cur["rollback_status"] == "failed"
        assert "not free" in cur["rollback_error"]
        # ...and the target row is untouched (verify runs before any
        # transient phase marker is written)
        tgt = ctx.deployment_store.load("dep-target")
        assert tgt["status"] == "superseded"
        assert "rollback_status" not in tgt
    finally:
        squat.close()


# ---------------------------------------------------------------------------
# 7. missing target -> fail
# ---------------------------------------------------------------------------
def test_missing_target_fails(tmp_path):
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))

    with pytest.raises(HandlerError, match="not found in the local "
                                          "registry"):
        handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                    "dep-missing"))

    assert docker.stop_calls == []
    assert docker.rm_calls == []
    assert docker.start_calls == []
    assert ctx.deployment_store.load("dep-cur")["status"] == "running"


# ---------------------------------------------------------------------------
# 8. missing image -> fail
# ---------------------------------------------------------------------------
def test_missing_image_fails(tmp_path, monkeypatch):
    """The target's container was GC'd and its image is unavailable: the
    rollback is refused before teardown, recorded durably."""
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    # c-target is gone and img:gone is not in docker.images
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state("dep-target", "c-gone",
                                     status="superseded",
                                     image="img:gone",
                                     health_status="unknown"))
    _hc(monkeypatch, set())

    with pytest.raises(HandlerError, match="cannot be restored"):
        handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                     "dep-target"))

    assert docker.stop_calls == []
    assert docker.rm_calls == []
    assert docker.start_calls == []
    assert docker.run_calls == []
    assert docker.containers["c-cur"]["running"] is True
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "running"
    assert cur["rollback_status"] == "failed"
    assert "cannot be restored" in cur["rollback_error"]


# ---------------------------------------------------------------------------
# 9. port-settlement-failure -> durable fail
# ---------------------------------------------------------------------------
def test_port_settlement_failure_is_durable_failure(tmp_path, monkeypatch):
    """Part 2E: the target is proven healthy but the port-registry settle
    fails — durable rollback_failed (partially_reconciled), never a
    success; the settle was attempted only AFTER health was proven."""
    port = _free_port()
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    session = FakeSession(fail_with=ConnectionError("control plane down"))
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state_with_port("dep-target", "c-target",
                                               port))
    _hc(monkeypatch, {port})

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    assert result["status"] == "rollback_failed"
    assert result["rollback_status"] == "partially_reconciled"
    assert result["target_health_status"] == "healthy"
    assert result["ports_settled"] is False
    # the settle ran exactly once — and only after the target was proven
    # healthy (the health verdict is logged before the settle attempt)
    assert len(session.posts) == 1
    lines = [line for _, line in ctx.logged]
    healthy_at = next(i for i, l in enumerate(lines)
                      if "rollback target health: healthy" in l)
    settle_at = next(i for i, l in enumerate(lines)
                     if "port settle failed" in l)
    assert healthy_at < settle_at
    # durable: the row records the failure + everything needed to retry
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "rollback_failed"
    assert cur["rollback_status"] == "partially_reconciled"
    assert "control plane down" in cur["rollback_error"]
    assert cur["port_settle"] == {"settled": False,
                                  "reason": "POST failed: control plane down"}
    # the physical restore still happened; Docker/OS == deployment state
    assert "c-target" in docker.start_calls
    assert ctx.deployment_store.used_host_ports() == {port}


# ---------------------------------------------------------------------------
# 10. worker-restart-during-rollback -> safe recovery
# ---------------------------------------------------------------------------
def test_worker_restart_during_rollback_recovers_safely(tmp_path):
    """A transient rollback phase on the rows (the worker died
    mid-rollback) is detected by reconcile: marked failed loudly, never
    silently completed, and the torn-down source is NOT resurrected."""
    docker = FakeDocker()
    docker.containers["c-target"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    # source: teardown already happened (container gone), the worker died
    # before the restore
    ctx.deployment_store.save(_state(
        "dep-cur", "c-cur", status="running", host_port=18002,
        ports={"18002": 3000}, rollback_status="restoring",
        rollback_target_deployment_id="dep-target"))
    # target: died mid health-check
    ctx.deployment_store.save(_state(
        "dep-target", "c-target", status="superseded",
        health_status="unknown", rollback_status="health-checking"))

    summary = reconcile_mod.reconcile(ctx)

    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "rollback_failed"  # DB must not claim running
    assert cur["rollback_status"] == "failed"
    assert "c-cur" not in docker.containers  # NOT resurrected
    assert docker.run_calls == []
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["rollback_status"] == "failed"  # stale marker cleared loudly
    assert any("interrupted" in a for a in summary["actions"])


# ---------------------------------------------------------------------------
# 11. repeated rollback -> idempotent
# ---------------------------------------------------------------------------
def test_repeated_rollback_is_idempotent(tmp_path, monkeypatch):
    """Running the same rollback twice converges on the same end state —
    no duplicate containers, no registry corruption."""
    port = _free_port()
    docker = FakeDocker()
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state_with_port("dep-v1", "c-v1", port))
    ctx.deployment_store.save(_state("dep-v2", "c-v2",
                                     host_port=port, container_port=3000,
                                     ports={str(port): 3000},
                                     healthcheck_path="/health"))
    _hc(monkeypatch, {port})

    r1 = handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))
    assert r1["status"] == "rolled_back"
    assert "c-v2" not in docker.containers

    r2 = handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))
    assert r2["status"] == "rolled_back"
    assert r2["rollback_status"] == "succeeded"
    assert r2["rolled_back_to"] == "dep-v1"

    # same end state: exactly one c-v1, registry still P -> V1
    assert list(docker.containers) == ["c-v1"]
    assert docker.containers["c-v1"]["running"] is True
    assert ctx.deployment_store.used_host_ports() == {port}
    assert ctx.deployment_store.load("dep-v1")["health_status"] == "healthy"
    assert len(session.posts) == 2
    assert all(p["json"] == {"target_deployment_id": "dep-v1"}
               for p in session.posts)


# ---------------------------------------------------------------------------
# 12. rollback-then-deploy -> works
# ---------------------------------------------------------------------------
def test_rollback_then_deploy_works(tmp_path, monkeypatch):
    """After a successful rollback V2->V1, a fresh deploy V3 builds on the
    restored generation: V1 becomes the rollback target (previous), V1 is
    superseded on success, and the registry converges on V3."""
    from deployments import pipeline

    p1 = _free_port()
    docker = FakeDocker()
    docker.images.add("img:3.0.0")
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state_with_port("dep-v1", "c-v1", p1))
    ctx.deployment_store.save(_state("dep-v2", "c-v2", version="2.0.0",
                                     host_port=p1, container_port=3000,
                                     ports={str(p1): 3000},
                                     healthcheck_path="/health"))
    _hc(monkeypatch, {p1})

    r = handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))
    assert r["status"] == "rolled_back"

    # every port is healthy for the fresh deploy (incl. its new port)
    monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, {
        "id": "task-3", "type": "deploy",
        "payload": {
            "project_id": "proj-1", "project_name": "my-api",
            "version": "3.0.0", "deployment_id": "dep-v3",
            "image": "img:3.0.0",  # prebuilt: no artifact download/build
            "manifest": {
                "name": "my-api", "runtime": "docker",
                "service": {"port": 3000, "healthcheck": "/health"},
                "resources": {"memory": "256m", "cpu": 1},
                "restart": "unless-stopped",
            },
            "healthcheck_timeout": 3,
        },
    })

    assert result["status"] == "running"
    v3 = ctx.deployment_store.load("dep-v3")
    assert v3["status"] == "running"
    assert v3["health_status"] == "healthy"
    assert v3["previous_deployment_id"] == "dep-v1"
    v1 = ctx.deployment_store.load("dep-v1")
    assert v1["status"] == "superseded"
    assert ctx.deployment_store.load("dep-v2")["status"] == "rolled_back"
    assert ctx.deployment_store.used_host_ports() == {v3["host_port"]}


# ---------------------------------------------------------------------------
# 13. failed rollback doesn't corrupt port registry
# ---------------------------------------------------------------------------
def test_failed_rollback_does_not_corrupt_port_registry(tmp_path,
                                                        monkeypatch):
    """After a failed rollback (unhealthy target), the registry matches
    physical truth: the restored target's port is live; the failed
    deployment's port is not claimed by anything."""
    p1, p2 = _free_port(), _free_port()
    docker = FakeDocker()
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save(_state_with_port("dep-v1", "c-v1", p1))
    ctx.deployment_store.save(_state("dep-v2", "c-v2", version="2.0.0",
                                     host_port=p2, container_port=3000,
                                     ports={str(p2): 3000},
                                     healthcheck_path="/health"))
    _hc(monkeypatch, set())  # target unhealthy -> rollback fails

    result = handlers.handle_rollback(ctx, _rollback_task("dep-v2",
                                                          "dep-v1"))

    assert result["status"] == "rollback_failed"
    # V2 was torn down (its port P2 is free); V1 is up on P1
    assert "c-v2" not in docker.containers
    assert docker.containers["c-v1"]["running"] is True
    # registry == physical truth: only P1 is live
    assert ctx.deployment_store.used_host_ports() == {p1}
    # the failed deployment's port was never re-registered anywhere
    assert p2 not in ctx.deployment_store.used_host_ports()
    # and no settle was attempted that could have blessed a broken state
    assert session.posts == []


# ---------------------------------------------------------------------------
# 14. failed rollback doesn't falsely mark target current
# ---------------------------------------------------------------------------
def test_failed_rollback_does_not_falsely_mark_target_current(tmp_path,
                                                              monkeypatch):
    """An unknown-health rollback must not present the target as the
    sanctioned current generation: no row claims rolled_back, the source
    row is rollback_failed, and the target row is failed — not
    succeeded."""
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state("dep-target", "c-target",
                                     status="superseded",
                                     health_status="unknown"))
    _hc(monkeypatch, set())

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    assert result["status"] == "rollback_failed"
    rows = ctx.deployment_store.list_all()
    assert all(r["status"] != "rolled_back" for r in rows)
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "rollback_failed"
    assert cur["rollback_status"] == "failed"
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["status"] == "running"  # it IS up...
    assert tgt["rollback_status"] == "failed"  # ...but not sanctioned
    assert tgt["health_status"] == "unknown"


# ---------------------------------------------------------------------------
# 15. event history accuracy
# ---------------------------------------------------------------------------
def test_rollback_failure_event_history_accuracy(tmp_path, monkeypatch):
    """Part 2C: the unknown-health case persists rollback_failed with the
    exact event shape {"target_health_status": "unknown",
    "rollback_status": "failed", "reason": "target health could not be
    verified"} — status strings only, no secrets."""
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state(
        "dep-target", "c-target", status="superseded",
        health_status="unknown",
        env={"API_TOKEN": "tok-live-abcdef-123456"}))
    _hc(monkeypatch, set())

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    # the exact 2C shape, as the worker reports it to the control plane
    # (which persists it as the deployment.rollback_failed event)
    assert result["status"] == "rollback_failed"
    event_shape = {
        "target_health_status": result["target_health_status"],
        "rollback_status": result["rollback_status"],
        "reason": "target health could not be verified",
    }
    assert event_shape == {
        "target_health_status": "unknown",
        "rollback_status": "failed",
        "reason": "target health could not be verified",
    }
    # ...and the durable row carries the same verdict + reason
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["rollback_status"] == "failed"
    assert "target health could not be verified" in cur["rollback_error"]
    tgt = ctx.deployment_store.load("dep-target")
    assert (tgt["health_status"], tgt["rollback_status"]) == ("unknown",
                                                              "failed")
    # no secrets in the record or the logs
    assert "tok-live-abcdef-123456" not in cur["rollback_error"]
    assert "tok-live-abcdef-123456" not in str(result)
    for _, line in ctx.logged:
        assert "tok-live-abcdef-123456" not in line


def test_unhealthy_target_event_shape(tmp_path, monkeypatch):
    """The unhealthy twin of the 2C shape: the failure record carries the
    health verdict honestly."""
    port = _free_port()
    docker = FakeDocker()
    docker.containers["c-cur"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-cur"))
    ctx.deployment_store.save(_state_with_port("dep-target", "c-target",
                                               port))
    _hc(monkeypatch, set())

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur",
                                                          "dep-target"))

    assert result["status"] == "rollback_failed"
    assert result["target_health_status"] == "unhealthy"
    assert result["rollback_status"] == "failed"
    cur = ctx.deployment_store.load("dep-cur")
    assert "health check failed" in cur["rollback_error"]
    assert "unhealthy" in cur["rollback_error"]
