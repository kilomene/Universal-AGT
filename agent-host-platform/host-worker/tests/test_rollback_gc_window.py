"""Rollback beyond the GC window (W18 acceptance gap).

Regression tests for the bug where rolling back to a generation older than
DEPLOY_KEEP_GENERATIONS tore down the CURRENT (healthy) deployment first and
only then discovered the target's container was gone ("no such container"),
leaving the project down.

The handler must now verify the target is restorable BEFORE teardown, and
rebuild a GC'd container from the persisted deployment contract when its
image is still available.
"""
import pytest

from deployments.state import DeploymentStore
from executor import handlers
from executor.handlers import HandlerError


class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.images = set()
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []
        self.run_calls = []

    def container_exists(self, name):
        return name in self.containers

    def image_exists(self, tag):
        return tag in self.images

    def ps(self, all=False):
        return []

    def start(self, name, timeout=120):
        self.start_calls.append(name)
        if name not in self.containers:
            raise RuntimeError(f"no such container: {name}")
        self.containers[name]["running"] = True

    def stop(self, name, timeout_secs=10, timeout=120):
        self.stop_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self.rm_calls.append(name)
        self.containers.pop(name, None)

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart=None):
        self.run_calls.append({"name": name, "image": image, "ports": ports,
                               "env": env, "memory": memory, "cpus": cpus,
                               "restart": restart})
        self.containers[name] = {"running": True, "image": image}


class FakeCtx:
    def __init__(self, tmp_path, docker):
        self.docker = docker
        self.deployment_store = DeploymentStore(str(tmp_path))
        self.logged = []

    def require_docker(self):
        return self.docker

    def log(self, task_id, line):
        self.logged.append((task_id, line))


def _contract_state(dep_id, name, project="proj-1", status="running"):
    """A full-contract state as persisted by pipeline.py (W4)."""
    return {
        "deployment_id": dep_id,
        "task_id": "task-0",
        "project_id": project,
        "project_name": "my-api",
        "version": "1.0.0",
        "container_name": name,
        "image": "img:x",
        "runtime": "docker",
        "restart": "always",
        "resources": {"cpu": 1.5, "memory": "1G"},
        "ports": {"8080": 80},
        "volumes": [],
        "healthcheck": {"type": "http", "path": "/health"},
        "status": status,
        "health_status": "healthy" if status == "running" else "unknown",
        "created_at": "2026-10-01T00:00:00Z",
    }


def _rollback_task(current_id, target_id):
    return {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": current_id,
                        "target_deployment_id": target_id}}


def test_rollback_gc_target_rebuilt_from_contract(tmp_path):
    """Target container GC'd but image present: rollback rebuilds it from the
    persisted contract instead of failing after teardown."""
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    docker.images.add("img:x")  # image survived GC; container did not
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_contract_state("dep-cur", "c-current"))
    ctx.deployment_store.save(_contract_state("dep-target", "c-target",
                                              status="superseded"))

    result = handlers.handle_rollback(ctx, _rollback_task("dep-cur", "dep-target"))

    assert result["rolled_back_to"] == "dep-target"
    # current was torn down only after the target proved restorable
    assert docker.stop_calls == ["c-current"]
    assert docker.rm_calls == ["c-current"]
    # target rebuilt from contract, not from hard-coded defaults
    assert len(docker.run_calls) == 1
    run = docker.run_calls[0]
    assert run["name"] == "c-target"
    assert run["image"] == "img:x"
    assert run["ports"] == {8080: 80}
    assert run["memory"] == "1G"
    assert run["cpus"] == 1.5 or run["cpus"] == "1.5"  # raw manifest value, as stored
    assert run["restart"] == "always"
    assert docker.containers["c-target"]["running"] is True


def test_rollback_gc_target_unrestorable_keeps_current_serving(tmp_path):
    """Target container GC'd AND image gone: rollback must fail WITHOUT
    tearing down the healthy current deployment."""
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    # c-target container gone, img:x not in docker.images
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_contract_state("dep-cur", "c-current"))
    ctx.deployment_store.save(_contract_state("dep-target", "c-target",
                                              status="superseded"))

    with pytest.raises(HandlerError, match="cannot be restored"):
        handlers.handle_rollback(ctx, _rollback_task("dep-cur", "dep-target"))

    # current deployment untouched and still serving
    assert docker.stop_calls == []
    assert docker.rm_calls == []
    assert docker.run_calls == []
    assert docker.containers["c-current"]["running"] is True
    cur = ctx.deployment_store.load("dep-cur")
    assert cur["status"] == "running"
