"""handle_rollback tests with a fake docker client.

The control plane's POST /v1/deployments/:id/rollback names the target
explicitly (payload.target_deployment_id). The handler must honor it;
without it, it falls back to the newest restorable local deployment.
"""
import pytest

from deployments.state import DeploymentStore
from executor import handlers


class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []

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


class FakeCtx:
    def __init__(self, tmp_path, docker):
        self.docker = docker
        self.deployment_store = DeploymentStore(str(tmp_path))
        self.logged = []

    def require_docker(self):
        return self.docker

    def log(self, task_id, line):
        self.logged.append((task_id, line))


def _state(dep_id, name, project="proj-1", status="running"):
    return {
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


def test_rollback_honors_target_deployment_id(tmp_path):
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    docker.containers["c-target"] = {"running": False}
    docker.containers["c-newer"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-current"))
    ctx.deployment_store.save(_state("dep-target", "c-target"))
    ctx.deployment_store.save(_state("dep-newer", "c-newer"))

    task = {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": "dep-cur",
                        "target_deployment_id": "dep-target"}}
    result = handlers.handle_rollback(ctx, task)

    # the EXPLICIT target was restored, not the newest candidate (dep-newer)
    assert result["rolled_back_to"] == "dep-target"
    assert "c-target" in docker.start_calls
    assert "c-newer" not in docker.start_calls
    assert ctx.deployment_store.load("dep-cur")["status"] == "rolled_back"
    assert ctx.deployment_store.load("dep-target")["status"] == "running"


def test_rollback_rejects_unknown_target(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-current"))
    task = {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": "dep-cur",
                        "target_deployment_id": "dep-missing"}}
    with pytest.raises(handlers.HandlerError, match="not found in the local registry"):
        handlers.handle_rollback(ctx, task)


def test_rollback_rejects_cross_project_target(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-current", project="proj-1"))
    ctx.deployment_store.save(_state("dep-other", "c-other", project="proj-2"))
    task = {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": "dep-cur",
                        "target_deployment_id": "dep-other"}}
    with pytest.raises(handlers.HandlerError, match="different project"):
        handlers.handle_rollback(ctx, task)


def test_rollback_without_target_falls_back_to_newest_candidate(tmp_path):
    docker = FakeDocker()
    docker.containers["c-current"] = {"running": True}
    docker.containers["c-old"] = {"running": False}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_state("dep-cur", "c-current"))
    old = _state("dep-old", "c-old")
    old["created_at"] = "2026-09-01T00:00:00Z"  # older than dep-cur
    ctx.deployment_store.save(old)

    task = {"id": "t1", "type": "rollback",
            "payload": {"deployment_id": "dep-cur"}}
    result = handlers.handle_rollback(ctx, task)
    assert result["rolled_back_to"] == "dep-old"
    assert "c-old" in docker.start_calls
