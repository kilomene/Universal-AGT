"""Tests for deployments.reconcile (post-reboot reconciliation).

The fake docker client is an in-memory double implementing the small slice
of the DockerClient interface that reconcile() uses (ps / start / run).
"""
import json

from deployments import reconcile as reconcile_mod
from deployments.state import DeploymentStore


class FakeDockerClient:
    def __init__(self):
        self.running = set()   # container names currently running
        self.existing = set()  # container names present (running or stopped)
        self.states = {}       # name -> "running" | "exited"
        self.start_calls = []
        self.run_calls = []

    def ps(self, all=False):
        self._ps_all = getattr(self, "_ps_all", None)
        names = self.existing if all else self.running
        return [{"Names": n, "State": self.states.get(n, "running")}
                for n in sorted(names)]

    def start(self, name, timeout=120):
        self.start_calls.append(name)
        if name in self.existing:
            self.running.add(name)
            self.states[name] = "running"

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self.run_calls.append({"name": name, "image": image, "ports": ports,
                               "env": env, "restart": restart})
        self.existing.add(name)
        self.running.add(name)
        self.states[name] = "running"
        return "fake-id-" + name


class FakeCtx:
    def __init__(self, store, docker):
        self.deployment_store = store
        self.docker = docker


def _save(store, deployment_id, **fields):
    state = {
        "deployment_id": deployment_id,
        "project_id": "proj-1",
        "project_name": "web",
        "version": "1.0.0",
        "container_name": f"uaht-web-1-0-0",
        "image": "uaht-web:1.0.0",
        "runtime": "docker",
        "host_port": 18080,
        "container_port": 3000,
        "env": {"FOO": "bar"},
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
    }
    state.update(fields)
    store.save(state)
    return state


def test_already_running_is_left_alone(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")
    docker = FakeDockerClient()
    docker.existing.add("uaht-web-1-0-0")
    docker.running.add("uaht-web-1-0-0")

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["already_running"] == 1
    assert summary["reconciled"] == 0
    assert docker.start_calls == []
    assert docker.run_calls == []


def test_stopped_container_is_started(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")
    docker = FakeDockerClient()
    docker.existing.add("uaht-web-1-0-0")
    docker.states["uaht-web-1-0-0"] = "exited"  # present but not running

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["reconciled"] == 1
    assert docker.start_calls == ["uaht-web-1-0-0"]
    assert docker.run_calls == []


def test_missing_container_is_recreated_from_stored_spec(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")
    docker = FakeDockerClient()  # knows nothing: container is gone

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["reconciled"] == 1
    assert len(docker.run_calls) == 1
    call = docker.run_calls[0]
    assert call["name"] == "uaht-web-1-0-0"
    assert call["image"] == "uaht-web:1.0.0"
    assert call["ports"] == {18080: 3000}
    assert call["env"] == {"FOO": "bar"}
    assert call["restart"] == "unless-stopped"
    assert "recreated" in summary["actions"][0]


def test_missing_container_without_image_is_reported(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", image=None)
    docker = FakeDockerClient()

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["missing"] == 1
    assert summary["reconciled"] == 0
    assert docker.run_calls == []


def test_non_running_deployments_are_ignored(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", status="stopped")
    _save(store, "dep-2", status="superseded")
    docker = FakeDockerClient()

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["reconciled"] == 0
    assert summary["missing"] == 0
    assert docker.start_calls == [] and docker.run_calls == []


def test_compose_project_without_containers_is_reported_not_recreated(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", container_name=None, compose_project="uaht-web")
    docker = FakeDockerClient()

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["missing"] == 1
    assert summary["reconciled"] == 0
    assert docker.run_calls == []
    assert any("compose" in a for a in summary["actions"])


def test_compose_stopped_containers_are_started(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", container_name=None, compose_project="uaht-web")
    docker = FakeDockerClient()
    docker.existing.add("uaht-web-api-1")
    docker.states["uaht-web-api-1"] = "exited"

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["reconciled"] == 1
    assert docker.start_calls == ["uaht-web-api-1"]


def test_corrupt_state_file_does_not_crash_reconcile(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-good")
    bad_dir = store.deployment_dir("dep-bad")
    (bad_dir / "state.json").write_text("{not valid json", encoding="utf-8")

    docker = FakeDockerClient()
    docker.existing.add("uaht-web-1-0-0")
    docker.running.add("uaht-web-1-0-0")

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))  # must not raise

    assert summary["already_running"] == 1  # the good one still processed
    # corrupt file was quarantined aside
    leftovers = list(bad_dir.iterdir())
    assert len(leftovers) == 1
    assert leftovers[0].name.startswith("state.json.corrupt-")


def test_docker_unavailable_skips_gracefully(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")

    summary = reconcile_mod.reconcile(FakeCtx(store, None))

    assert summary["reconciled"] == 0
    assert summary["skipped"] == 1
    assert any("skipped" in a for a in summary["actions"])


def test_summary_has_expected_shape(tmp_path):
    store = DeploymentStore(str(tmp_path))
    summary = reconcile_mod.reconcile(FakeCtx(store, FakeDockerClient()))
    assert set(summary) == {"reconciled", "already_running", "missing",
                            "skipped", "unexpected", "actions", "at"}
    assert summary["actions"] == []
    assert summary["unexpected"] == []


def test_unexpected_container_is_reported_not_destroyed(tmp_path):
    """Spec §20: containers Docker knows that no deployment claims are
    reported — never blindly destroyed."""
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")  # claims uaht-web-1-0-0
    docker = FakeDockerClient()
    docker.existing.add("uaht-web-1-0-0")
    docker.running.add("uaht-web-1-0-0")
    # a stray: crash-mid-deploy leftover / manually started / foreign tool
    docker.existing.add("stray-debug-box")
    docker.running.add("stray-debug-box")
    docker.states["stray-debug-box"] = "running"

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["already_running"] == 1
    assert summary["unexpected"] == [
        {"name": "stray-debug-box", "state": "running"}]
    # reported, and crucially NOT stopped/removed — reconcile has no
    # destroy path for unclaimed containers
    assert "stray-debug-box" in docker.existing
    assert "stray-debug-box" in docker.running
    assert any("unexpected container stray-debug-box" in a
               for a in summary["actions"])


def test_stopped_deployment_container_is_claimed_not_unexpected(tmp_path):
    """A container belonging to a non-running (e.g. superseded) deployment
    is still claimed — not flagged unexpected."""
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", status="superseded")
    docker = FakeDockerClient()
    docker.existing.add("uaht-web-1-0-0")
    docker.states["uaht-web-1-0-0"] = "exited"

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["unexpected"] == []
    assert summary["skipped"] == 0  # superseded is not desired-running; ignored


def test_compose_project_containers_are_claimed(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", container_name=None, compose_project="uaht-web")
    docker = FakeDockerClient()
    docker.existing.add("uaht-web-api-1")
    docker.running.add("uaht-web-api-1")
    docker.existing.add("uaht-webx-api-1")  # prefix lookalike: NOT claimed
    docker.running.add("uaht-webx-api-1")

    summary = reconcile_mod.reconcile(FakeCtx(store, docker))

    assert summary["unexpected"] == [
        {"name": "uaht-webx-api-1", "state": "running"}]
