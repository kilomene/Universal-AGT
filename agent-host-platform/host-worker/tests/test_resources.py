"""W8: reserved vs utilized resources, and admission control on reservations.

allocated = sum of reservations from running deployments' manifests.
utilized = instantaneous measured usage (cpu_pct/ram_pct). The two are
tracked and reported separately; admission control rejects a deployment
that would overcommit RESERVED resources even when utilization is low.

Includes the spec's exact scenario: host 4 CPU / 16 GB RAM; App A reserves
2 CPU/8GB, App B reserves 2 CPU/8GB -> App C requesting 2 CPU/8GB is
REJECTED with a clear reason, even with low instantaneous utilization.
"""
from pathlib import Path

import pytest

from deployments import pipeline
from deployments.state import DeploymentStore
from health import collector
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDockerClient:
    def __init__(self):
        self.run_calls = []
        self.containers = {}
        self.ps_rows = []

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self.run_calls.append({"name": name, "image": image, "ports": ports})
        self.containers[name] = {"image": image, "running": True}
        return "fake-id-" + name

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
        return list(self.ps_rows)

    def compose_available(self):
        return False


class FakeAPI:
    pass


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


def _seed(store, dep_id, project, cpu, memory, status="running"):
    store.save({
        "deployment_id": dep_id,
        "task_id": "task-0",
        "project_id": f"proj-{project}",
        "project_name": project,
        "version": "1.0.0",
        "container_name": f"uaht-{project}-1-0-0",
        "image": f"{project}:1.0.0",
        "runtime": "docker",
        "resources": {"cpu": cpu, "memory": memory},
        "restart": "unless-stopped",
        "ports": {"18080": 3000},
        "host_port": 18080,
        "container_port": 3000,
        "env": {},
        "status": status,
        "created_at": "2026-10-01T00:00:00Z",
    })


def _deploy_task(project, dep_id, cpu, memory):
    return {"id": f"task-{dep_id}", "type": "deploy", "payload": {
        "project_id": f"proj-{project}",
        "project_name": project,
        "version": "9.9.9",
        "deployment_id": dep_id,
        "image": f"{project}:9.9.9",
        "manifest": {
            "name": project,
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {"cpu": cpu, "memory": memory},
            "restart": "unless-stopped",
            "env": {},
        },
        "healthcheck_timeout": 3,
    }}


@pytest.fixture
def host_4cpu_16gb(monkeypatch):
    """Pretend the host is 4 CPU / 16 GB regardless of the real machine."""
    monkeypatch.setattr(collector, "total_cpu", lambda: 4.0)
    monkeypatch.setattr(collector, "total_ram_mb", lambda: 16384.0)
    # pipeline holds its own reference to the collector module — same object
    assert pipeline.health_collector is collector


# ---------------------------------------------------------------------------
# allocated_resources unit tests
# ---------------------------------------------------------------------------
def test_allocated_sums_running_reservations(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _seed(store, "dep-a", "app-a", 2, "8g")
    _seed(store, "dep-b", "app-b", 2, "8g")
    assert collector.allocated_resources(store) == {"cpu": 4.0, "ram_mb": 16384.0}


def test_allocated_ignores_non_running_and_unreserved(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _seed(store, "dep-a", "app-a", 2, "8g", status="running")
    _seed(store, "dep-b", "app-b", 99, "99g", status="stopped")
    _seed(store, "dep-c", "app-c", 99, "99g", status="superseded")
    _seed(store, "dep-d", "app-d", 99, "99g", status="rolled_back")
    store.save({"deployment_id": "dep-e", "status": "running"})  # no resources
    assert collector.allocated_resources(store) == {"cpu": 2.0, "ram_mb": 8192.0}


def test_allocated_tolerates_malformed_values(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _seed(store, "dep-a", "app-a", "lots", "plenty", status="running")
    assert collector.allocated_resources(store) == {"cpu": 0.0, "ram_mb": 0.0}


def test_allocated_without_store_is_zero():
    assert collector.allocated_resources(None) == {"cpu": 0.0, "ram_mb": 0.0}


# ---------------------------------------------------------------------------
# Admission control: the spec's exact scenario
# ---------------------------------------------------------------------------
def test_spec_scenario_third_app_rejected_despite_low_utilization(
        tmp_path, monkeypatch, host_4cpu_16gb):
    """Host 4 CPU/16GB; A reserves 2/8GB, B reserves 2/8GB ->
    C requesting 2/8GB is REJECTED, even with low instantaneous use."""
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 2, "8g")
    _seed(ctx.deployment_store, "dep-b", "app-b", 2, "8g")
    # instantaneous utilization is (near) zero — irrelevant to the decision
    monkeypatch.setattr(collector, "cpu_percent", lambda *a, **k: 1.0)
    monkeypatch.setattr(collector, "memory_percent", lambda: 2.0)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    with pytest.raises(pipeline.DeployError, match="insufficient reservable"):
        pipeline.deploy(ctx, _deploy_task("app-c", "dep-c", 2, "8g"))
    assert docker.run_calls == []  # rejected before ANY docker interaction


def test_rejection_reason_names_reserved_and_available(
        tmp_path, monkeypatch, host_4cpu_16gb):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 2, "8g")
    _seed(ctx.deployment_store, "dep-b", "app-b", 2, "8g")
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    with pytest.raises(pipeline.DeployError) as excinfo:
        pipeline.deploy(ctx, _deploy_task("app-c", "dep-c", 2, "8g"))
    msg = str(excinfo.value)
    assert "reserved" in msg and "available" in msg
    assert "16384" in msg or "16" in msg  # host total is named


def test_memory_only_overcommit_rejected(tmp_path, monkeypatch, host_4cpu_16gb):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 0.5, "12g")
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    with pytest.raises(pipeline.DeployError, match="insufficient reservable memory"):
        pipeline.deploy(ctx, _deploy_task("app-b", "dep-b", 0.5, "8g"))


def test_cpu_only_overcommit_rejected(tmp_path, monkeypatch, host_4cpu_16gb):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 3, "1g")
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    with pytest.raises(pipeline.DeployError, match="insufficient reservable CPU"):
        pipeline.deploy(ctx, _deploy_task("app-b", "dep-b", 2, "1g"))


def test_fitting_deployment_is_admitted(tmp_path, monkeypatch, host_4cpu_16gb):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 1, "4g")
    _seed(ctx.deployment_store, "dep-b", "app-b", 1, "4g")
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, _deploy_task("app-c", "dep-c", 2, "8g"))
    assert result["status"] == "running"
    assert len(docker.run_calls) == 1


def test_exact_fit_is_admitted(tmp_path, monkeypatch, host_4cpu_16gb):
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 2, "8g")
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    result = pipeline.deploy(ctx, _deploy_task("app-b", "dep-b", 2, "8g"))
    assert result["status"] == "running"


def test_no_resources_in_manifest_skips_admission_check(
        tmp_path, monkeypatch, host_4cpu_16gb):
    """A manifest with no reservations is never rejected (old behavior)."""
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    _seed(ctx.deployment_store, "dep-a", "app-a", 4, "16g")  # host "full"
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    task = _deploy_task("app-b", "dep-b", 1, "1g")
    del task["payload"]["manifest"]["resources"]
    result = pipeline.deploy(ctx, task)
    assert result["status"] == "running"


# ---------------------------------------------------------------------------
# Reporting: reserved vs utilized are separate payload keys
# ---------------------------------------------------------------------------
def test_collector_reports_reserved_separately_from_utilization(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _seed(store, "dep-a", "app-a", 1, "1g")
    metrics = collector.collect_metrics(deployment_store=store)
    # utilization (measured)
    for key in ("cpu_pct", "ram_pct", "disk_pct"):
        assert key in metrics
    # reservation accounting (manifest sums) — distinct keys
    assert metrics["allocated_cpu"] == 1.0
    assert metrics["allocated_ram_mb"] == 1024.0
    assert metrics["allocated_disk_gb"] == 0.0
    # available = total - allocated
    assert metrics["available_cpu"] == pytest.approx(metrics["total_cpu"] - 1.0)
    assert metrics["available_ram_mb"] == pytest.approx(
        metrics["total_ram_mb"] - 1024.0)
    assert metrics["available_disk_gb"] > 0


# ---------------------------------------------------------------------------
# Spec §11: allocation is transactional — concurrent deploys cannot consume
# the same capacity twice.
# ---------------------------------------------------------------------------
def test_concurrent_deploys_cannot_overcommit_resources(
        tmp_path, monkeypatch, host_4cpu_16gb):
    """Two simultaneous deploys each requesting 12GB on a 16GB host: exactly
    one is admitted. The loser sees the winner's in-flight reservation
    (not just persisted rows) and is rejected — no over-commit, no leak."""
    import threading
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    barrier = threading.Barrier(2)
    outcomes = {}

    def _one(i):
        barrier.wait(timeout=30)
        try:
            result = pipeline.deploy(
                ctx, _deploy_task(f"race-app-{i}", f"dep-race-{i}", 1, "12g"))
            outcomes[i] = ("admitted", result)
        except pipeline.DeployError as exc:
            outcomes[i] = ("rejected", str(exc))

    threads = [threading.Thread(target=_one, args=(i,), daemon=True)
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "deploy thread hung"

    admitted = [i for i, (k, _) in outcomes.items() if k == "admitted"]
    rejected = [i for i, (k, _) in outcomes.items() if k == "rejected"]
    assert len(admitted) == 1 and len(rejected) == 1, outcomes
    assert "insufficient reservable memory" in outcomes[rejected[0]][1]

    # No leaked reservation: the process-wide set is empty after both
    # finished, and the winner's persisted row now carries the accounting —
    # a third 12GB deploy is still rejected (store-based, not double).
    assert pipeline.reserved_resources_snapshot() == {}
    winner = ctx.deployment_store.load(f"dep-race-{admitted[0]}")
    assert winner["status"] == "running"
    assert collector.allocated_resources(
        ctx.deployment_store)["ram_mb"] == 12288.0
    with pytest.raises(pipeline.DeployError,
                       match="insufficient reservable memory"):
        pipeline.deploy(ctx, _deploy_task("race-app-2", "dep-race-2", 1, "12g"))
    assert docker.run_calls and len(
        [c for c in docker.run_calls]) == 1


def test_failed_deploy_releases_its_reservation(
        tmp_path, monkeypatch, host_4cpu_16gb):
    """A deploy admitted and then failed (healthcheck) must not permanently
    hold its reservation (spec §60): a retry afterwards fits again."""
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        return calls["n"] > 1  # first healthcheck fails, retry passes

    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck", _flaky)
    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _deploy_task("retry-app", "dep-retry-1", 1, "12g"))
    assert pipeline.reserved_resources_snapshot() == {}

    result = pipeline.deploy(
        ctx, _deploy_task("retry-app", "dep-retry-2", 1, "12g"))
    assert result["status"] == "running"
    assert pipeline.reserved_resources_snapshot() == {}


def test_rejected_admission_records_no_reservation(tmp_path, host_4cpu_16gb):
    store = DeploymentStore(str(tmp_path))
    with pytest.raises(pipeline.DeployError, match="insufficient reservable"):
        pipeline.reserve_resources("dep-x", 99.0, 0.0, store)
    with pytest.raises(pipeline.DeployError, match="insufficient reservable"):
        pipeline.reserve_resources("dep-y", 0.0, 999999.0, store)
    assert pipeline.reserved_resources_snapshot() == {}


def test_reserve_and_release_roundtrip(tmp_path, host_4cpu_16gb):
    store = DeploymentStore(str(tmp_path))
    pipeline.reserve_resources("dep-a", 1.0, 4096.0, store)
    assert pipeline.reserved_resources_snapshot() == {
        "dep-a": {"cpu": 1.0, "ram_mb": 4096.0}}
    # A second deploy sees the in-flight reservation, not just the store.
    with pytest.raises(pipeline.DeployError, match="insufficient reservable"):
        pipeline.reserve_resources("dep-b", 4.0, 16384.0, store)
    pipeline.release_resource_reservation("dep-a")
    assert pipeline.reserved_resources_snapshot() == {}
    # Releasing a non-existent reservation is a safe no-op.
    pipeline.release_resource_reservation("dep-a")


def test_build_failure_after_admission_releases_reservation(
        tmp_path, monkeypatch, host_4cpu_16gb):
    """A §5 failure (here: compose requested but unavailable) happens after
    the §3 resource reservation was taken: the reservation must be
    released, not leaked for the life of the worker process."""
    docker = FakeDockerClient()
    ctx = FakeCtx(tmp_path, docker)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)
    task = _deploy_task("compose-app", "dep-compose-1", 1, "4g")
    task["payload"]["manifest"]["runtime"] = "docker-compose"
    with pytest.raises(pipeline.DeployError, match="compose.*unavailable"):
        pipeline.deploy(ctx, task)
    assert pipeline.reserved_resources_snapshot() == {}
    # and a later deploy is still admitted (capacity was not shrunk)
    task2 = _deploy_task("ok-app", "dep-ok-1", 1, "4g")
    result = pipeline.deploy(ctx, task2)
    assert result["status"] == "running"
