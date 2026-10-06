"""Fix #1 — worker enforcement of the control-plane resource reservation.

The scheduler reserves CPU/RAM from the artifact's validated agent.deploy.json
manifest (migration 014). The worker deploys the tarball's manifest. These
must never diverge: if the tarball demands MORE than the task payload's
reserved contract, the deploy fails loudly instead of silently
over-committing the host.

Uses the same fakes as test_pipeline.py (FakeDockerClient/FakeCtx injected
through WorkerContext — the real docker module is never touched).
"""
from pathlib import Path

import pytest

from deployments import pipeline
from deployments.state import DeploymentStore
from logs.store import LogStore


class FakeDockerClient:
    def __init__(self):
        self.calls = []
        self.containers = {}

    def _record(self, name, *args):
        self.calls.append((name, args))

    def version(self):
        return "99.0-fake"

    def compose_available(self):
        return True

    def build(self, context_dir, dockerfile, tag, build_args=None, timeout=1200):
        self._record("build", tag)
        return "fake build output"

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self._record("run", name)
        self.containers[name] = {"image": image, "running": True}
        return "fake-container-id-" + name

    def start(self, name, timeout=120):
        if name in self.containers:
            self.containers[name]["running"] = True

    def stop(self, name, timeout_secs=10, timeout=120):
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self.containers.pop(name, None)

    def container_exists(self, name):
        return name in self.containers

    def logs(self, name, tail=500):
        return "fake logs\n"

    def ps(self, all=False):
        return []

    def inspect(self, name):
        c = self.containers.get(name, {})
        return [{"Config": {"Image": c.get("image")},
                 "State": {"Status": "running" if c.get("running") else "exited"},
                 "NetworkSettings": {"Ports": {}}}]


class FakeAPI:
    def __init__(self):
        self.downloads = []

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        raise AssertionError("no artifact download expected in these tests")


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
        return self.scrub(line)

    def require_docker(self):
        return self.docker


def _task(manifest_resources, reserved_cpu, reserved_ram_mb):
    payload = {
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "2.0.0",
        "deployment_id": "dep-v2",
        "image": "img:2.0.0",  # prebuilt: manifest comes from payload
        "manifest": {
            "name": "my-api",
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": manifest_resources,
            "restart": "unless-stopped",
        },
        "healthcheck_timeout": 3,
    }
    if reserved_cpu is not None:
        payload["reserved_cpu"] = reserved_cpu
    if reserved_ram_mb is not None:
        payload["reserved_ram_mb"] = reserved_ram_mb
    return {"id": "task-1", "type": "deploy", "payload": payload}


def test_tarball_exceeding_reservation_fails_loudly(tmp_path):
    """Fix #1: 4 CPU / 8Gi tarball against a 1 CPU / 512Mi reservation."""
    ctx = FakeCtx(tmp_path, FakeDockerClient())
    task = _task({"cpu": 4, "memory": "8Gi"}, reserved_cpu=1, reserved_ram_mb=512)
    with pytest.raises(pipeline.DeployError, match="demands more resources than the control plane reserved"):
        pipeline.deploy(ctx, task)
    # Nothing was deployed: no container was created.
    assert ctx.docker.calls == []


def test_tarball_exceeding_ram_only_fails(tmp_path):
    ctx = FakeCtx(tmp_path, FakeDockerClient())
    task = _task({"cpu": 1, "memory": "8Gi"}, reserved_cpu=4, reserved_ram_mb=512)
    with pytest.raises(pipeline.DeployError, match="demands more resources than the control plane reserved"):
        pipeline.deploy(ctx, task)
    assert ctx.docker.calls == []


def test_tarball_within_reservation_deploys(tmp_path, monkeypatch):
    """Exact match and under-match both proceed past the enforcement gate."""
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    ctx = FakeCtx(tmp_path, FakeDockerClient())
    task = _task({"cpu": 1, "memory": "512Mi"}, reserved_cpu=1, reserved_ram_mb=512)
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"


def test_legacy_payload_without_reservation_skips_check(tmp_path, monkeypatch):
    """Payloads from an older control plane carry no reservation fields —
    the gate is skipped rather than breaking the deploy."""
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    ctx = FakeCtx(tmp_path, FakeDockerClient())
    task = _task({"cpu": 1, "memory": "512Mi"}, reserved_cpu=None, reserved_ram_mb=None)
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"
