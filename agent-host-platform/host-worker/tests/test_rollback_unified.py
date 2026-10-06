"""Unified rollback regression tests (spec §21).

Both rollback paths — automatic rollback in ``pipeline.deploy`` and
explicit rollback via ``handlers.handle_rollback`` — call
``deployments.rollback.perform_rollback``. These tests pin the shared
behavior through both callers plus the common module directly:

  * automatic rollback: the restored previous deployment gets a REAL
    health check and its health_status records the result honestly
    (healthy AND unhealthy);
  * rollback port restoration: the control-plane settle API is called on
    the automatic path too, and a settle failure is recorded (row +
    log + error), never silently claimed;
  * Docker/OS state == deployment state: after rollback the restored
    target's port is the one in the registry (V1->18001, V2->18002
    scenario);
  * rollback port collision / failed target restoration /
    verify-before-destroy: a bad target raises without touching the
    healthy deployment — in both paths;
  * GC'd rollback target on the automatic path is rebuilt from the
    persisted contract, exactly like the explicit path.
"""
import socket
from pathlib import Path

import pytest

from deployments import pipeline
from deployments import rollback as rollback_mod
from deployments.state import DeploymentStore
from executor import handlers
from executor.handlers import HandlerError
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes (dependency injection — the real docker/health modules are never
# stubbed; the health check itself is monkeypatched per test)
# ---------------------------------------------------------------------------
class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.images = set()
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []
        self.run_calls = []
        self.compose_up_calls = []
        self.compose_down_calls = []
        self.compose_projects = set()

    def container_exists(self, name):
        return name in self.containers

    def image_exists(self, tag):
        return tag in self.images

    def ps(self, all=False):
        return []

    def start(self, name, timeout=120):
        self.start_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = True

    def stop(self, name, timeout_secs=10, timeout=120):
        self.stop_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self.rm_calls.append(name)
        self.containers.pop(name, None)

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart=None, timeout=120):
        self.run_calls.append({"name": name, "image": image, "ports": ports,
                               "env": env, "memory": memory, "cpus": cpus,
                               "restart": restart})
        self.containers[name] = {"image": image, "running": True}

    def logs(self, name, tail=500):
        return "fake logs\n"

    def compose_up(self, compose_file, project_name=None, build=False,
                   timeout=300):
        self.compose_up_calls.append((compose_file, project_name, build))
        self.compose_projects.add(project_name)

    def compose_down(self, compose_file, project_name=None, timeout=120):
        self.compose_down_calls.append((compose_file, project_name))
        self.compose_projects.discard(project_name)

    def compose_ps(self, project_name, timeout=60):
        return []


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

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"")
        return dest_path


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
        self.log_store.append_task(task_id, line)
        return line

    def require_docker(self):
        return self.docker


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _deploy_task(**overrides):
    payload = {
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "2.0.0",
        "deployment_id": "dep-v2",
        "image": "img:2.0.0",  # prebuilt: no artifact download, no build
        "manifest": {
            "name": "my-api",
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {"memory": "256m", "cpu": 1},
            "restart": "unless-stopped",
            "env": {"NODE_ENV": "production"},
        },
        "healthcheck_timeout": 3,
    }
    payload.update(overrides)
    return {"id": "task-1", "type": "deploy", "payload": payload}


def _seed_previous(ctx, dep_id="dep-v1", name="uaht-my-api-1.0.0",
                   port=None, **overrides):
    state = {
        "deployment_id": dep_id,
        "task_id": "task-0",
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "1.0.0",
        "container_name": name,
        "image": "img:1.0.0",
        "runtime": "docker",
        "host_port": port,
        "container_port": 3000,
        "ports": {str(port): 3000} if port else None,
        "healthcheck_path": "/health",
        "env": {"NODE_ENV": "production"},
        "status": "running",
        "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    }
    state.update(overrides)
    ctx.deployment_store.save(state)
    return state


def _hc_fake(monkeypatch, healthy_ports):
    """wait_for_healthcheck fake: healthy only on the given ports."""
    from health import checker as health_checker
    seen = []

    def _fake(port, path="/", timeout_secs=60, log=None):
        seen.append((int(port), path))
        return int(port) in healthy_ports

    monkeypatch.setattr(health_checker, "wait_for_healthcheck", _fake)
    return seen


# ---------------------------------------------------------------------------
# Automatic rollback: the previous deployment is health-checked FOR REAL
# ---------------------------------------------------------------------------
def test_auto_rollback_healthchecks_previous_for_real(tmp_path, monkeypatch):
    """The restored previous deployment passes its real health check, so
    it is recorded healthy — and the check actually ran against its
    port (not assumed)."""
    prev_port = _free_port()
    docker = FakeDocker()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                              "running": True}
    ctx = FakeCtx(tmp_path, docker)
    _seed_previous(ctx, port=prev_port)

    seen = _hc_fake(monkeypatch, {prev_port})  # new container's port fails

    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task())

    # the rollback health check really ran against the previous port
    assert (prev_port, "/health") in seen
    prev_state = ctx.deployment_store.load("dep-v1")
    assert prev_state["status"] == "running"
    assert prev_state["health_status"] == "healthy"
    # Docker/OS state == deployment state: the previous port is the live one
    assert ctx.deployment_store.used_host_ports() == {prev_port}


def test_auto_rollback_records_unhealthy_previous_honestly(tmp_path,
                                                           monkeypatch):
    """THE defect fix: the restored previous deployment fails its real
    health check, so it is recorded "unhealthy" — never marked healthy
    just because its container restarted."""
    prev_port = _free_port()
    docker = FakeDocker()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                              "running": True}
    ctx = FakeCtx(tmp_path, docker)
    _seed_previous(ctx, port=prev_port)

    _hc_fake(monkeypatch, set())  # everything fails, including previous

    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task())

    prev_state = ctx.deployment_store.load("dep-v1")
    assert prev_state["status"] == "running"
    assert prev_state["health_status"] == "unhealthy"
    assert any("UNHEALTHY" in line for _, line in ctx.logged)


def test_auto_rollback_rebuilds_gcd_previous_from_contract(tmp_path,
                                                           monkeypatch):
    """Previous container GC'd but its image survives: the automatic
    path rebuilds it from the persisted contract, exactly like the
    explicit path."""
    prev_port = _free_port()
    docker = FakeDocker()
    docker.images.add("img:1.0.0")  # image survived GC; container did not
    ctx = FakeCtx(tmp_path, docker)
    _seed_previous(ctx, port=prev_port)

    _hc_fake(monkeypatch, {prev_port})

    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task())

    assert len(docker.run_calls) == 2  # v2 attempt + v1 rebuild
    run = docker.run_calls[-1]
    assert run["name"] == "uaht-my-api-1.0.0"
    assert run["image"] == "img:1.0.0"
    assert run["ports"] == {prev_port: 3000}
    prev_state = ctx.deployment_store.load("dep-v1")
    assert prev_state["health_status"] == "healthy"


def test_auto_rollback_compose_missing_file_fails_clean(tmp_path, monkeypatch):
    """Previous compose stack whose compose file is gone: the automatic
    rollback raises DeployError (wrapping the verify failure) and leaves
    the previous row untouched; the failed deploy is recorded "failed",
    not "rolled_back"."""
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    _seed_previous(ctx, container_name=None, port=None, ports=None,
                   runtime="docker-compose",
                   compose_project="uaht-my-api-proj1",
                   compose_file=str(ctx.config.work_dir) + "/gone.yml",
                   compose_ports=[18099],
                   status="running")
    _hc_fake(monkeypatch, set())

    with pytest.raises(pipeline.DeployError,
                       match="rollback to dep-v1 failed"):
        pipeline.deploy(ctx, _deploy_task())

    # the failed deploy never became a row (same as the pre-unification
    # behavior: rollback failure raises before the row is persisted)
    assert ctx.deployment_store.load("dep-v2") is None
    # the previous row was never touched by the failed rollback
    assert ctx.deployment_store.load("dep-v1")["status"] == "running"


# ---------------------------------------------------------------------------
# Automatic rollback: port-reservation reconciliation with the registry
# ---------------------------------------------------------------------------
def test_auto_rollback_settles_ports_via_control_plane(tmp_path, monkeypatch):
    prev_port = _free_port()
    docker = FakeDocker()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                              "running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    _seed_previous(ctx, port=prev_port)
    _hc_fake(monkeypatch, {prev_port})

    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task())

    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == ("https://control-plane.example.com"
                           "/v1/deployments/dep-v2/settle-rollback-ports")
    assert post["json"] == {"target_deployment_id": "dep-v1"}
    v2_state = ctx.deployment_store.load("dep-v2")
    assert v2_state["rollback_of"] == "dep-v1"
    assert v2_state["port_settle"] == {"settled": True, "reason": None}


def test_auto_rollback_settle_failure_is_recorded_not_silent(tmp_path,
                                                             monkeypatch):
    """A settle failure never fails the rollback — it is logged and
    recorded on the failed deployment's row (and surfaced in the
    DeployError), never silently claimed as success. Spec §6: the row
    persists rollback_failed (partially_reconciled), not "rolled_back"."""
    prev_port = _free_port()
    docker = FakeDocker()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                              "running": True}
    session = FakeSession(fail_with=ConnectionError("control plane down"))
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    _seed_previous(ctx, port=prev_port)
    _hc_fake(monkeypatch, {prev_port})

    with pytest.raises(pipeline.DeployError,
                       match="port registry settle failed") as excinfo:
        pipeline.deploy(ctx, _deploy_task())

    assert "rolled back to deployment dep-v1" in str(excinfo.value)
    assert "PARTIALLY RECONCILED" in str(excinfo.value)
    v2_state = ctx.deployment_store.load("dep-v2")
    assert v2_state["status"] == "rollback_failed"
    assert v2_state["rollback_status"] == "partially_reconciled"
    assert "control plane down" in v2_state["rollback_error"]
    assert v2_state["port_settle"]["settled"] is False
    assert "control plane down" in v2_state["port_settle"]["reason"]
    assert any("non-fatal" in line for _, line in ctx.logged)


# ---------------------------------------------------------------------------
# Common module directly: verify-before-destroy on a stopped target whose
# port is squatted (port collision) — the healthy deployment is untouched.
# ---------------------------------------------------------------------------
def _rollback_ctx(tmp_path, docker):
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save({
        "deployment_id": "dep-cur", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "2.0.0",
        "container_name": "c-current", "image": "img:x", "runtime": "docker",
        "host_port": 18002, "ports": {"18002": 3000},
        "status": "running", "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })
    return ctx


def test_module_verify_before_destroy_on_port_collision(tmp_path):
    """perform_rollback raises RollbackError for a stopped target whose
    recorded port is squatted — the healthy current deployment and its
    state are untouched."""
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squat.bind(("0.0.0.0", 0))
    squat.listen(1)
    port = squat.getsockname()[1]
    try:
        ctx = _rollback_ctx(tmp_path, docker)
        target = {
            "deployment_id": "dep-target", "task_id": "t0",
            "project_id": "proj-1", "project_name": "my-api",
            "version": "1.0.0", "container_name": "c-target",
            "image": "img:x", "runtime": "docker", "host_port": port,
            "ports": {str(port): 3000}, "status": "superseded",
            "health_status": "unknown",
            "created_at": "2026-09-01T00:00:00Z",
        }
        with pytest.raises(rollback_mod.RollbackError, match="not free"):
            rollback_mod.perform_rollback(
                ctx, docker, log=lambda line: None, target=target)

        assert docker.stop_calls == []
        assert docker.rm_calls == []
        assert docker.start_calls == []
        assert docker.containers["c-current"]["running"] is True
        assert ctx.deployment_store.load("dep-cur")["status"] == "running"
    finally:
        squat.close()


def test_module_failed_restore_leaves_state_untouched(tmp_path):
    """A target whose container is gone and whose image is unavailable
    raises RollbackError from verify — nothing is torn down."""
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    ctx = _rollback_ctx(tmp_path, docker)
    target = {
        "deployment_id": "dep-target", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "1.0.0",
        "container_name": "c-gone", "image": "img:gone",
        "runtime": "docker", "ports": {"18003": 3000},
        "status": "superseded", "health_status": "unknown",
        "created_at": "2026-09-01T00:00:00Z",
    }
    with pytest.raises(rollback_mod.RollbackError, match="cannot be restored"):
        rollback_mod.perform_rollback(
            ctx, docker, log=lambda line: None, target=target)

    assert docker.stop_calls == []
    assert docker.run_calls == []
    assert ctx.deployment_store.load("dep-cur")["status"] == "running"


# ---------------------------------------------------------------------------
# Explicit rollback, spec scenario: V1->18001, V2->18002, V2 fails, V1
# restored on 18001 — registry view matches Docker/OS state afterwards.
# ---------------------------------------------------------------------------
def test_explicit_rollback_restores_target_port_in_registry(tmp_path,
                                                           monkeypatch):
    from health import checker as health_checker
    docker = FakeDocker()
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    ctx.deployment_store.save({
        "deployment_id": "dep-v1", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "1.0.0",
        "container_name": "c-v1", "image": "img:x", "runtime": "docker",
        "host_port": 18001, "ports": {"18001": 3000},
        "healthcheck_path": "/health",
        "status": "superseded", "health_status": "unknown",
        "created_at": "2026-09-01T00:00:00Z",
    })
    ctx.deployment_store.save({
        "deployment_id": "dep-v2", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "2.0.0",
        "container_name": "c-v2", "image": "img:x", "runtime": "docker",
        "host_port": 18002, "ports": {"18002": 3000},
        "status": "running", "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })
    monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    result = handlers.handle_rollback(
        ctx, {"id": "t1", "type": "rollback",
              "payload": {"deployment_id": "dep-v2",
                          "target_deployment_id": "dep-v1"}})

    assert result["status"] == "rolled_back"
    assert result["rolled_back_to"] == "dep-v1"
    assert result["target_health_status"] == "healthy"
    assert result["ports_settled"] is True
    # V2 torn down, V1 restored on its own port
    assert docker.containers["c-v1"]["running"] is True
    assert "c-v2" not in docker.containers
    # registry view: only the restored target's port is live
    assert ctx.deployment_store.used_host_ports() == {18001}
    # the control plane was told to release V2's reservation and
    # re-reserve V1's
    assert session.posts[0]["json"] == {"target_deployment_id": "dep-v1"}


def test_explicit_rollback_refuses_unrestorable_target(tmp_path):
    """GC'd target with no image: HandlerError, healthy current untouched
    (verify-before-destroy preserved through the unified module)."""
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save({
        "deployment_id": "dep-cur", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "2.0.0",
        "container_name": "c-current", "image": "img:x", "runtime": "docker",
        "status": "running", "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })
    ctx.deployment_store.save({
        "deployment_id": "dep-old", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "1.0.0",
        "container_name": "c-gone", "image": "img:gone",
        "runtime": "docker", "status": "superseded",
        "created_at": "2026-09-01T00:00:00Z",
    })

    with pytest.raises(HandlerError, match="cannot be restored"):
        handlers.handle_rollback(
            ctx, {"id": "t1", "type": "rollback",
                  "payload": {"deployment_id": "dep-cur",
                              "target_deployment_id": "dep-old"}})

    assert docker.stop_calls == []
    assert docker.rm_calls == []
    assert docker.containers["c-current"]["running"] is True
    assert ctx.deployment_store.load("dep-cur")["status"] == "running"


# ---------------------------------------------------------------------------
# Spec §§5-7 regression tests: same-port rollback, durable rollback
# failure, repeated rollback, rollback-then-deploy, host restart.
# ---------------------------------------------------------------------------
class SamePortDocker(FakeDocker):
    """Reports every container's published port (with docker State) in
    ps(), so the OS-bind probe genuinely fails during verify — the
    same-port rollback must still succeed via the free-after-teardown
    excuse for the deployment being replaced."""

    def __init__(self, port):
        super().__init__()
        self._port = port
        self.ops = []

    def ps(self, all=False):
        rows = []
        for name, info in self.containers.items():
            running = bool(info.get("running"))
            rows.append({"Names": "/" + name,
                         "State": "running" if running else "exited",
                         "Ports": f"0.0.0.0:{self._port}->3000/tcp"})
        return rows

    def stop(self, name, timeout_secs=10, timeout=120):
        self.ops.append(("stop", name))
        super().stop(name, timeout_secs=timeout_secs, timeout=timeout)

    def rm(self, name, force=False, timeout=120):
        self.ops.append(("rm", name))
        super().rm(name, force=force, timeout=timeout)

    def start(self, name, timeout=120):
        self.ops.append(("start", name))
        super().start(name, timeout=timeout)


def _hold_port():
    """A real listening socket: makes the OS-bind probe genuinely fail."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _seed_pair(ctx, port_v1, port_v2):
    ctx.deployment_store.save({
        "deployment_id": "dep-v1", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "1.0.0",
        "container_name": "c-v1", "image": "img:x", "runtime": "docker",
        "host_port": port_v1, "container_port": 3000,
        "ports": {str(port_v1): 3000}, "healthcheck_path": "/health",
        "status": "superseded", "health_status": "unknown",
        "created_at": "2026-09-01T00:00:00Z",
    })
    ctx.deployment_store.save({
        "deployment_id": "dep-v2", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "2.0.0",
        "container_name": "c-v2", "image": "img:x", "runtime": "docker",
        "host_port": port_v2, "container_port": 3000,
        "ports": {str(port_v2): 3000}, "healthcheck_path": "/health",
        "status": "running", "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })


def _rollback_task(dep_id, target_id):
    return {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": dep_id,
                        "target_deployment_id": target_id}}


def test_explicit_rollback_same_port(tmp_path, monkeypatch):
    """Spec §5/§7: V1 -> port P (stopped), V2 -> port P (same port,
    running). V2's port claim is expected — it is torn down before the
    restore — so the rollback must succeed, and two physical containers
    must never claim P at once (teardown strictly before start)."""
    from health import checker as health_checker
    squat, port = _hold_port()
    try:
        docker = SamePortDocker(port)
        docker.containers["c-v1"] = {"running": False}
        docker.containers["c-v2"] = {"running": True}
        session = FakeSession()
        ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
        _seed_pair(ctx, port, port)
        monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                            lambda *a, **k: True)

        result = handlers.handle_rollback(ctx, _rollback_task("dep-v2",
                                                              "dep-v1"))

        assert result["status"] == "rolled_back"
        assert result["rollback_status"] == "succeeded"
        assert result["rolled_back_to"] == "dep-v1"
        # safe sequence: V2 fully torn down BEFORE V1 started
        assert docker.ops.index(("rm", "c-v2")) < \
            docker.ops.index(("start", "c-v1"))
        assert ("stop", "c-v2") in docker.ops
        assert docker.containers["c-v1"]["running"] is True
        assert "c-v2" not in docker.containers
        # Docker/OS state == deployment state == registry: P -> V1, and
        # P is never still attributed to V2
        assert ctx.deployment_store.used_host_ports() == {port}
        assert ctx.deployment_store.load("dep-v1")["health_status"] == "healthy"
        assert ctx.deployment_store.load("dep-v2")["status"] == "rolled_back"
        assert session.posts[0]["json"] == {"target_deployment_id": "dep-v1"}
        assert any("free-after-teardown" in line for _, line in ctx.logged)
    finally:
        squat.close()


def test_explicit_rollback_same_port_refuses_foreign_squat(tmp_path,
                                                             monkeypatch):
    """Spec §5: same-port rollback where a THIRD party squats the port —
    refused loudly before the current deployment is touched (the
    free-after-teardown excuse covers ONLY the deployment being
    replaced)."""
    from health import checker as health_checker
    _squat, port = _hold_port()
    try:
        docker = SamePortDocker(port)
        docker.containers["c-v1"] = {"running": False}
        docker.containers["c-v2"] = {"running": True}
        docker.containers["c-squat"] = {"running": True}
        ctx = FakeCtx(tmp_path, docker)
        _seed_pair(ctx, port, port)
        monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                            lambda *a, **k: True)

        with pytest.raises(handlers.HandlerError, match="not free"):
            handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))

        assert docker.ops == []
        assert docker.containers["c-v2"]["running"] is True
        assert ctx.deployment_store.load("dep-v2")["status"] == "running"
        # the failed attempt is recorded durably (spec §6)
        cur = ctx.deployment_store.load("dep-v2")
        assert cur["rollback_status"] == "failed"
    finally:
        _squat.close()


def test_auto_rollback_unhealthy_target_persists_rollback_failed(
        tmp_path, monkeypatch):
    """Spec §6: automatic rollback restores the target but its health
    check fails — the failed deployment persists rollback_failed (NOT
    "rolled_back"), with enough state for reconciliation/retry."""
    prev_port = _free_port()
    docker = FakeDocker()
    docker.containers["uaht-my-api-1.0.0"] = {"image": "img:1.0.0",
                                              "running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    _seed_previous(ctx, port=prev_port)
    _hc_fake(monkeypatch, set())  # everything unhealthy, incl. the target

    with pytest.raises(pipeline.DeployError, match="rollback FAILED"):
        pipeline.deploy(ctx, _deploy_task())

    v2 = ctx.deployment_store.load("dep-v2")
    assert v2["status"] == "rollback_failed"
    assert v2["rollback_status"] == "failed"
    assert v2["rollback_of"] == "dep-v1"
    assert "health check failed" in v2["rollback_error"]
    # the target row is honest: running (it IS up) but unhealthy
    v1 = ctx.deployment_store.load("dep-v1")
    assert v1["status"] == "running"
    assert v1["health_status"] == "unhealthy"
    assert v1["rollback_status"] == "failed"


def test_explicit_rollback_verify_failure_records_failed_phase(tmp_path):
    """Spec §6: verify fails -> HandlerError, the current deployment is
    untouched and still honestly "running", but the failed attempt is
    recorded durably (requested -> failed) for reconciliation/retry."""
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save({
        "deployment_id": "dep-cur", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "2.0.0",
        "container_name": "c-current", "image": "img:x", "runtime": "docker",
        "status": "running", "health_status": "healthy",
        "created_at": "2026-10-01T00:00:00Z",
    })
    ctx.deployment_store.save({
        "deployment_id": "dep-old", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "1.0.0",
        "container_name": "c-gone", "image": "img:gone",
        "runtime": "docker", "status": "superseded",
        "created_at": "2026-09-01T00:00:00Z",
    })

    with pytest.raises(handlers.HandlerError, match="cannot be restored"):
        handlers.handle_rollback(ctx, _rollback_task("dep-cur", "dep-old"))

    assert docker.stop_calls == []
    assert docker.rm_calls == []
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "running"  # untouched — still the truth
    assert cur["rollback_status"] == "failed"
    assert cur["rollback_target_deployment_id"] == "dep-old"
    assert "cannot be restored" in cur["rollback_error"]


def test_repeated_rollback(tmp_path, monkeypatch):
    """Spec §7: rollback V2->V1, then rollback V1->V2 again. The second
    rollback rebuilds V2 from its persisted contract (its container was
    removed by the first rollback's teardown) and the registry
    converges on V2's port."""
    from health import checker as health_checker
    p1, p2 = _free_port(), _free_port()
    docker = FakeDocker()
    docker.images.add("img:x")
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    _seed_pair(ctx, p1, p2)
    monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    r1 = handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))
    assert r1["status"] == "rolled_back"
    assert "c-v2" not in docker.containers  # teardown removed it

    r2 = handlers.handle_rollback(ctx, _rollback_task("dep-v1", "dep-v2"))
    assert r2["status"] == "rolled_back"
    assert r2["rollback_status"] == "succeeded"
    assert r2["rolled_back_to"] == "dep-v2"
    assert docker.containers["c-v2"]["running"] is True
    assert "c-v1" not in docker.containers  # torn down by the 2nd rollback
    assert ctx.deployment_store.load("dep-v2")["status"] == "running"
    assert ctx.deployment_store.load("dep-v1")["status"] == "rolled_back"
    assert ctx.deployment_store.used_host_ports() == {p2}
    assert len(session.posts) == 2
    assert session.posts[1]["json"] == {"target_deployment_id": "dep-v2"}


def test_rollback_followed_by_deploy(tmp_path, monkeypatch):
    """Spec §7: after rollback V2->V1, a fresh deploy V3 builds on the
    restored generation — V1 becomes the rollback target (previous),
    V1 is superseded on success, and the registry converges on V3."""
    from health import checker as health_checker
    p1, p2 = _free_port(), _free_port()
    docker = FakeDocker()
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    _seed_pair(ctx, p1, p2)
    monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    r = handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))
    assert r["status"] == "rolled_back"

    result = pipeline.deploy(
        ctx, _deploy_task(version="3.0.0", deployment_id="dep-v3",
                          image="img:3.0.0"))
    assert result["status"] == "running"
    v3 = ctx.deployment_store.load("dep-v3")
    assert v3["status"] == "running"
    assert v3["health_status"] == "healthy"
    assert v3["previous_deployment_id"] == "dep-v1"
    v1 = ctx.deployment_store.load("dep-v1")
    assert v1["status"] == "superseded"
    # §4.16: the successful deploy GC'd the oldest generation's container
    # (keep=2) — collection happens only after successful settlement
    assert "c-v1" not in docker.containers
    assert ctx.deployment_store.load("dep-v2")["status"] == "rolled_back"
    assert ctx.deployment_store.used_host_ports() == {v3["host_port"]}


class PsDocker(FakeDocker):
    """ps() honors the all flag with docker State, like the real client."""

    def ps(self, all=False):
        rows = []
        for name, info in self.containers.items():
            running = bool(info.get("running"))
            if all or running:
                rows.append({"Names": "/" + name,
                             "State": "running" if running else "exited",
                             "Ports": ""})
        return rows


def test_host_restart_after_rollback(tmp_path, monkeypatch):
    """Spec §7: worker restarts after a completed rollback — reconcile
    converges: the restored target is already running, the rolled-back
    generation is skipped, and the registry matches physical state."""
    from deployments import reconcile as reconcile_mod
    from health import checker as health_checker
    p1, p2 = _free_port(), _free_port()
    docker = FakeDocker()
    docker.containers["c-v1"] = {"running": False}
    docker.containers["c-v2"] = {"running": True}
    session = FakeSession()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(session))
    _seed_pair(ctx, p1, p2)
    monkeypatch.setattr(health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    r = handlers.handle_rollback(ctx, _rollback_task("dep-v2", "dep-v1"))
    assert r["status"] == "rolled_back"

    # simulate the restart: post-restart docker view + a fresh store
    # handle over the same state dir
    docker2 = PsDocker()
    docker2.containers["c-v1"] = {"running": True}
    ctx2 = FakeCtx(tmp_path, docker2)
    summary = reconcile_mod.reconcile(ctx2)

    assert summary["already_running"] == 1
    assert summary["missing"] == 0
    assert summary["unexpected"] == []
    assert ctx2.deployment_store.load("dep-v1")["status"] == "running"
    assert ctx2.deployment_store.load("dep-v1")["health_status"] == "healthy"
    assert ctx2.deployment_store.load("dep-v2")["status"] == "rolled_back"
    assert ctx2.deployment_store.used_host_ports() == {p1}


def test_interrupted_rollback_marked_failed_on_restart(tmp_path):
    """Spec §6: the worker restarts mid-rollback (a transient phase was
    recorded) — reconcile marks the operation failed loudly and does NOT
    resurrect the torn-down source deployment."""
    from deployments import reconcile as reconcile_mod
    docker = PsDocker()
    docker.containers["c-target"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    # rollback source: teardown already happened (container gone), the
    # worker died before the restore
    ctx.deployment_store.save({
        "deployment_id": "dep-cur", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "2.0.0",
        "container_name": "c-current", "image": "img:x", "runtime": "docker",
        "host_port": 18002, "ports": {"18002": 3000},
        "status": "running", "health_status": "healthy",
        "rollback_status": "restoring",
        "rollback_target_deployment_id": "dep-target",
        "created_at": "2026-10-01T00:00:00Z",
    })
    ctx.deployment_store.save({
        "deployment_id": "dep-target", "task_id": "t0", "project_id": "proj-1",
        "project_name": "my-api", "version": "1.0.0",
        "container_name": "c-target", "image": "img:x", "runtime": "docker",
        "host_port": 18001, "ports": {"18001": 3000},
        "status": "superseded", "health_status": "unknown",
        "rollback_status": "restoring",
        "created_at": "2026-09-01T00:00:00Z",
    })

    summary = reconcile_mod.reconcile(ctx)

    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "rollback_failed"  # DB no longer claims running
    assert cur["rollback_status"] == "failed"
    assert "c-current" not in docker.containers  # NOT resurrected
    assert docker.run_calls == []
    tgt = ctx.deployment_store.load("dep-target")
    assert tgt["rollback_status"] == "failed"
    assert any("interrupted" in a for a in summary["actions"])
