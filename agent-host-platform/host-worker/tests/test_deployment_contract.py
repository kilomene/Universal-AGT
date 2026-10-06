"""W4: the full deployment contract survives env updates, restarts, reboots.

Regression coverage for the known bug: environment updates fell back to
restart policy ``unless-stopped`` (and silently dropped cpu/memory limits)
because the pipeline never persisted the contract and the handler
re-derived run behavior from ``docker inspect`` + hard-coded defaults.

The test deploys with NON-DEFAULT values for every contract field, then:
  1. performs an environment-update and asserts the replacement container
     was rebuilt with the identical contract (restart policy, cpu/memory,
     image, ports);
  2. simulates a worker restart (a brand-new DeploymentStore over the same
     directory — no in-memory state survives) and asserts every field is
     intact;
  3. runs post-reboot reconcile from that reloaded state with the container
     missing and asserts it was recreated with the identical contract.
"""
import json
from pathlib import Path

import pytest

from deployments import pipeline
from deployments import reconcile as reconcile_mod
from deployments.state import DeploymentStore
from executor import handlers
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDockerClient:
    """Records the FULL docker.run kwargs (memory/cpus/restart included)."""

    def __init__(self):
        self.run_calls = []
        self.stop_calls = []
        self.rm_calls = []
        self.start_calls = []
        self.containers = {}
        self.ps_rows = []

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self.run_calls.append({"name": name, "image": image, "ports": ports,
                               "env": dict(env or {}), "memory": memory,
                               "cpus": cpus, "restart": restart})
        self.containers[name] = {"image": image, "running": True,
                                 "ports": ports or {}}
        return "fake-id-" + name

    def stop(self, name, timeout_secs=10, timeout=120):
        self.stop_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self.rm_calls.append(name)
        self.containers.pop(name, None)

    def start(self, name, timeout=120):
        self.start_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = True

    def container_exists(self, name):
        return name in self.containers

    def logs(self, name, tail=500):
        return "fake logs\n"

    def ps(self, all=False):
        return list(self.ps_rows)

    def inspect(self, name):
        c = self.containers.get(name, {})
        return [{"Config": {"Image": c.get("image")},
                 "State": {"Status": "running" if c.get("running") else "exited"},
                 "NetworkSettings": {"Ports": {}}}]


class FakeAPI:
    def __init__(self):
        self.downloads = []

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        self.downloads.append(artifact_id)
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"")
        return dest_path


class FakeConfig:
    def __init__(self, work_dir):
        self.work_dir = str(work_dir)
        self.apps_dir = str(work_dir / "apps")


class FakeCtx:
    def __init__(self, tmp_path, docker):
        self.config = FakeConfig(tmp_path)
        self.api = FakeAPI()
        self.docker = docker
        self.log_store = LogStore(str(tmp_path / "logs"))
        self.deployment_store = DeploymentStore(str(tmp_path))
        self.scrub = lambda s: s

    def log(self, task_id, line):
        clean = self.scrub(line)
        self.log_store.append_task(task_id, clean)
        return clean

    def require_docker(self):
        return self.docker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
CONTRACT_MANIFEST = {
    "name": "contract-app",
    "runtime": "docker",
    "service": {"port": 8080, "healthcheck": "/ready"},
    "resources": {"memory": "1g", "cpu": 2},   # non-default limits
    "restart": "on-failure",                   # non-default policy (the bug)
    "env": {"APP_MODE": "contract", "LOG_LEVEL": "debug"},
    "volumes": ["contract-data:/data"],
    "healthcheck": {"path": "/ready", "interval": "30s"},
    "domains": ["contract.example.com"],
}


def _deploy_task():
    return {"id": "task-deploy-1", "type": "deploy", "payload": {
        "project_id": "proj-contract",
        "project_name": "contract-app",
        "version": "3.1.0",
        "deployment_id": "dep-contract-1",
        "image": "registry.example.com/contract-app:3.1.0",  # prebuilt
        "manifest": dict(CONTRACT_MANIFEST),
        "secrets": {"DB_PASSWORD": "s3cr3t"},
        "healthcheck_timeout": 3,
    }}


def _deploy(ctx, monkeypatch):
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, _deploy_task())
    assert result["status"] == "running"
    return ctx.deployment_store.load("dep-contract-1")


def _assert_contract(state, host_port):
    """Every contract field persisted with its non-default value."""
    assert state["runtime"] == "docker"
    assert state["restart"] == "on-failure"
    assert state["resources"] == {"cpu": 2, "memory": "1g"}
    assert state["image"] == "registry.example.com/contract-app:3.1.0"
    assert state["container_name"] == "uaht-contract-app-3.1.0-depcontr"
    assert state["host_port"] == host_port
    assert state["container_port"] == 8080
    assert state["ports"] == {str(host_port): 8080}
    assert state["healthcheck_path"] == "/ready"
    assert state["volumes"] == ["contract-data:/data"]
    assert state["healthcheck"] == {"path": "/ready", "interval": "30s"}
    assert state["domains"] == ["contract.example.com"]
    assert state["manifest"]["restart"] == "on-failure"
    assert state["manifest"]["resources"] == {"memory": "1g", "cpu": 2}
    # secrets never persisted
    assert "DB_PASSWORD" not in json.dumps(state)


def _assert_run_preserves_contract(run_call, host_port):
    assert run_call["restart"] == "on-failure"   # the known bug: was unless-stopped
    assert run_call["memory"] == "1g"            # the known bug: was dropped
    assert run_call["cpus"] == "2"               # the known bug: was dropped
    assert run_call["image"] == "registry.example.com/contract-app:3.1.0"
    assert run_call["ports"] == {host_port: 8080}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_deploy_persists_full_contract(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    state = _deploy(ctx, monkeypatch)
    _assert_contract(state, docker.run_calls[0]["ports"] and
                     next(iter(docker.run_calls[0]["ports"])))
    assert state["env"] == {"APP_MODE": "contract", "LOG_LEVEL": "debug"}


def test_environment_update_preserves_every_contract_field(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    state = _deploy(ctx, monkeypatch)
    host_port = next(iter(docker.run_calls[0]["ports"]))
    assert len(docker.run_calls) == 1

    result = handlers.handle_environment_update(ctx, {
        "id": "task-env-1", "type": "environment-update",
        "payload": {"deployment_id": "dep-contract-1",
                    "env": {"NEW_KEY": "new-value"},
                    "secrets": {"DB_PASSWORD": "rotated-s3cr3t"}},
    })
    assert result["status"] == "running"
    assert result["updated_env_keys"] == ["NEW_KEY"]

    # the replacement container was rebuilt with the identical contract
    assert len(docker.run_calls) == 2
    rebuilt = docker.run_calls[1]
    assert rebuilt["name"] == state["container_name"]
    _assert_run_preserves_contract(rebuilt, host_port)
    # env merged: old non-secret + new + secrets injected but not persisted
    assert rebuilt["env"]["NEW_KEY"] == "new-value"
    assert rebuilt["env"]["APP_MODE"] == "contract"
    assert rebuilt["env"]["DB_PASSWORD"] == "rotated-s3cr3t"
    reloaded = ctx.deployment_store.load("dep-contract-1")
    assert reloaded["env"]["NEW_KEY"] == "new-value"
    assert "DB_PASSWORD" not in reloaded["env"]
    # contract untouched by the env update
    assert reloaded["restart"] == "on-failure"
    assert reloaded["resources"] == {"cpu": 2, "memory": "1g"}


def test_contract_survives_simulated_worker_restart(tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    state = _deploy(ctx, monkeypatch)
    host_port = next(iter(docker.run_calls[0]["ports"]))

    # env update, then "reboot": a brand-new store over the same directory —
    # nothing in memory survives.
    handlers.handle_environment_update(ctx, {
        "id": "task-env-1", "type": "environment-update",
        "payload": {"deployment_id": "dep-contract-1",
                    "env": {"NEW_KEY": "new-value"}},
    })
    fresh_store = DeploymentStore(str(tmp_path))  # new process, same disk
    reloaded = fresh_store.load("dep-contract-1")
    assert reloaded is not None
    _assert_contract(reloaded, host_port)
    assert reloaded["env"]["NEW_KEY"] == "new-value"
    # _assert_contract checked the pre-update env; the update added NEW_KEY
    assert reloaded["env"]["APP_MODE"] == "contract"


def test_reconcile_recreates_missing_container_with_full_contract(
        tmp_path, monkeypatch):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _deploy(ctx, monkeypatch)
    host_port = next(iter(docker.run_calls[0]["ports"]))

    # simulate a reboot: fresh store, and docker knows no containers
    fresh_store = DeploymentStore(str(tmp_path))
    rebooted_docker = FakeDockerClient()

    class RebootCtx:
        deployment_store = fresh_store
        docker = rebooted_docker

    summary = reconcile_mod.reconcile(RebootCtx())
    assert summary["reconciled"] == 1
    assert len(rebooted_docker.run_calls) == 1
    recreated = rebooted_docker.run_calls[0]
    assert recreated["name"] == "uaht-contract-app-3.1.0-depcontr"
    _assert_run_preserves_contract(recreated, host_port)
    assert recreated["env"] == {"APP_MODE": "contract", "LOG_LEVEL": "debug"}


def test_run_spec_from_state_prefers_stored_contract():
    spec = pipeline.run_spec_from_state({
        "image": "img:x",
        "ports": {"8080": 80, "8443": 443},
        "restart": "always",
        "resources": {"cpu": 0.5, "memory": "256m"},
        "host_port": 9999,  # stale legacy pair must NOT win
        "container_port": 9999,
    })
    assert spec == {"image": "img:x",
                    "ports": {8080: 80, 8443: 443},
                    "memory": "256m", "cpus": "0.5", "restart": "always"}


def test_run_spec_from_state_falls_back_for_pre_contract_states():
    # states written before the full contract existed: legacy pair + default
    spec = pipeline.run_spec_from_state({
        "image": "img:old", "host_port": 18080, "container_port": 3000,
    })
    assert spec == {"image": "img:old", "ports": {18080: 3000},
                    "memory": None, "cpus": None,
                    "restart": "unless-stopped"}
    # empty state: all defaults, nothing invented
    spec = pipeline.run_spec_from_state({})
    assert spec == {"image": None, "ports": None, "memory": None,
                    "cpus": None, "restart": "unless-stopped"}


# ---------------------------------------------------------------------------
# Spec §14: env updates must not silently change restart policy, CPU,
# memory, port, volumes, image, healthcheck, domain, ingress or identity.
# The spec's explicit case: restart=always survives an env update.
# ---------------------------------------------------------------------------
def test_environment_update_preserves_restart_always(tmp_path, monkeypatch):
    """Deploy restart=always -> update env -> container recreated ->
    restart still always (spec §14's explicit case)."""
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    manifest = dict(CONTRACT_MANIFEST)
    manifest["restart"] = "always"  # the spec's explicit non-default case
    task = _deploy_task()
    task["payload"]["manifest"] = manifest
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"
    assert docker.run_calls[0]["restart"] == "always"
    state = ctx.deployment_store.load("dep-contract-1")
    assert state["restart"] == "always"
    host_port = next(iter(docker.run_calls[0]["ports"]))

    handlers.handle_environment_update(ctx, {
        "id": "task-env-always", "type": "environment-update",
        "payload": {"deployment_id": "dep-contract-1",
                    "env": {"NEW_KEY": "new-value"}},
    })

    assert len(docker.run_calls) == 2
    rebuilt = docker.run_calls[1]
    assert rebuilt["restart"] == "always"  # not silently reset to unless-stopped
    assert rebuilt["memory"] == "1g"
    assert rebuilt["cpus"] == "2"
    assert rebuilt["image"] == "registry.example.com/contract-app:3.1.0"
    assert rebuilt["ports"] == {host_port: 8080}  # same host port, not re-picked
    assert rebuilt["env"]["NEW_KEY"] == "new-value"
    reloaded = ctx.deployment_store.load("dep-contract-1")
    assert reloaded["restart"] == "always"
    assert reloaded["resources"] == {"cpu": 2, "memory": "1g"}


def test_deploy_persists_artifact_id(tmp_path, monkeypatch):
    """Spec §13: the artifact reference is part of the persisted contract."""
    import hashlib
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    task = _deploy_task()
    task["payload"]["artifact_id"] = "art-123"
    task["payload"]["artifact_checksum"] = (
        "sha256:" + hashlib.sha256(b"").hexdigest())
    # the contract test's FakeAPI "downloads" empty bytes; skip real
    # extraction (artifact security is WS-F's area)
    monkeypatch.setattr(pipeline, "extract_archive",
                        lambda archive_path, dest_dir: dest_dir)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"
    state = ctx.deployment_store.load("dep-contract-1")
    assert state["artifact_id"] == "art-123"
