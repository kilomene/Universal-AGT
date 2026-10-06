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
    DeployError), never silently claimed as success."""
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
    v2_state = ctx.deployment_store.load("dep-v2")
    assert v2_state["status"] == "rolled_back"
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
