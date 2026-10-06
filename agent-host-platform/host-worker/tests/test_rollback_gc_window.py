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
        self.rmi_calls = []
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

    def container_status(self, name):
        c = self.containers.get(name)
        if c is None:
            return None
        return "running" if c.get("running") else "exited"

    def rm(self, name, force=False, timeout=120):
        self.rm_calls.append(name)
        self.containers.pop(name, None)

    def remove_image(self, tag):
        self.rmi_calls.append(tag)
        self.images.discard(tag)

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


def _contract_state(dep_id, name, project="proj-1", status="running",
                    image="img:x", version="1.0.0"):
    """A full-contract state as persisted by pipeline.py (W4)."""
    return {
        "deployment_id": dep_id,
        "task_id": "task-0",
        "project_id": project,
        "project_name": "my-api",
        "version": version,
        "container_name": name,
        "image": image,
        "image_built": True,
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


def test_gc_keep_window_then_rollback_v3_to_v2_retains_target(tmp_path):
    """Spec §18: three generations V1/V2/V3 with DEPLOY_KEEP_GENERATIONS=2.

    GC must collect V1's container AND its worker-built image (beyond the
    keep window) while RETAINING V2 — the rollback target — and never
    touching active V3. A subsequent rollback V3->V2 then restores V2 via
    ``docker start`` (not a rebuild): V2's container was never removed and
    its image was never rmi'd.
    """
    from deployments import gc

    docker = FakeDocker()
    for ver in ("1.0.0", "2.0.0", "3.0.0"):
        docker.images.add(f"uaht-app:{ver}")
    docker.containers["c-v1"] = {"running": False, "image": "uaht-app:1.0.0"}
    docker.containers["c-v2"] = {"running": False, "image": "uaht-app:2.0.0"}
    docker.containers["c-v3"] = {"running": True, "image": "uaht-app:3.0.0"}
    ctx = FakeCtx(tmp_path, docker)
    ctx.deployment_store.save(_contract_state(
        "dep-v1", "c-v1", status="superseded",
        image="uaht-app:1.0.0", version="1.0.0"))
    ctx.deployment_store.save(_contract_state(
        "dep-v2", "c-v2", status="superseded",
        image="uaht-app:2.0.0", version="2.0.0"))
    ctx.deployment_store.save(_contract_state(
        "dep-v3", "c-v3", status="running",
        image="uaht-app:3.0.0", version="3.0.0"))

    summary = gc.collect_garbage(ctx, keep=2)

    # V1 is beyond the keep window: container AND worker-built image gone
    assert "c-v1" in docker.rm_calls
    assert "uaht-app:1.0.0" in docker.rmi_calls
    assert "uaht-app:1.0.0" not in docker.images
    # V2 is inside the keep window: retained, container AND image intact
    assert "c-v2" not in docker.rm_calls
    assert "uaht-app:2.0.0" not in docker.rmi_calls
    assert "uaht-app:2.0.0" in docker.images
    # V3 (active) untouched
    assert "c-v3" not in docker.rm_calls
    assert summary["removed_containers"] == ["c-v1"]
    assert summary["removed_images"] == ["uaht-app:1.0.0"]

    # rollback V3 -> V2: the retained target starts in place — no rebuild
    result = handlers.handle_rollback(ctx, _rollback_task("dep-v3", "dep-v2"))
    assert result["rolled_back_to"] == "dep-v2"
    assert docker.run_calls == []            # never rebuilt from contract
    assert docker.start_calls == ["c-v2"]    # started in place
    assert docker.containers["c-v2"]["running"] is True
    # the rolled-back deployment (V3) was torn down
    assert "c-v3" in docker.stop_calls
    assert "c-v3" in docker.rm_calls
    # and V2's image is still there afterwards
    assert "uaht-app:2.0.0" in docker.images
