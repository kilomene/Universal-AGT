"""Spec §9 (WS3): compose vs docker-run parity for lifecycle tasks.

Before this workstream, restart/stop/start/logs/status handlers raised
HandlerError for docker-compose deployments (they assumed container_name
is always set; compose deployments record compose_project instead).
These tests pin the parity: every lifecycle/introspection task that works
on a single-container deployment works on a compose deployment too.
"""
from executor import handlers
from deployments.state import DeploymentStore


class FakeDocker:
    """In-memory docker double covering both container and compose ops."""

    def __init__(self):
        self.containers = {}        # name -> {"running": bool}
        self.compose_projects = {}  # project -> [container names]
        self.calls = []
        self.compose_down_calls = []

    # -- single-container ops ------------------------------------------
    def restart_container(self, name, timeout=120):
        self.calls.append(("restart_container", name))

    def stop(self, name, timeout_secs=10, timeout=120):
        self.calls.append(("stop", name))
        if name in self.containers:
            self.containers[name]["running"] = False

    def start(self, name, timeout=120):
        self.calls.append(("start", name))
        if name in self.containers:
            self.containers[name]["running"] = True

    def container_status(self, name):
        c = self.containers.get(name)
        return "running" if c and c["running"] else "exited"

    def inspect(self, name):
        return [{"Config": {"Image": "img:x"},
                 "State": {"Status": self.container_status(name)},
                 "NetworkSettings": {"Ports": {"3000/tcp": [
                     {"HostIp": "0.0.0.0", "HostPort": "18080"}]}}}]

    def logs(self, name, tail=500):
        self.calls.append(("logs", name))
        return f"logs-of-{name}\n"

    # -- compose ops ----------------------------------------------------
    def compose_restart(self, project_name, timeout=300):
        self.calls.append(("compose_restart", project_name))
        return "restarted"

    def compose_stop(self, project_name, timeout=300):
        self.calls.append(("compose_stop", project_name))
        for n in self.compose_projects.get(project_name, []):
            self.containers[n]["running"] = False
        return "stopped"

    def compose_start(self, project_name, timeout=300):
        self.calls.append(("compose_start", project_name))
        for n in self.compose_projects.get(project_name, []):
            self.containers[n]["running"] = True
        return "started"

    def compose_down(self, compose_file, project_name=None, timeout=300):
        self.calls.append(("compose_down", project_name))
        self.compose_down_calls.append((compose_file, project_name))
        self.compose_projects.pop(project_name, None)
        return "down"

    def compose_ps(self, project_name, timeout=60):
        recs = []
        for n in self.compose_projects.get(project_name, []):
            running = (self.containers.get(n) or {}).get("running", False)
            recs.append({
                "Name": n,
                "State": "running" if running else "exited",
                "Publishers": [{"PublishedPort": "18081",
                                "TargetPort": 3000, "Protocol": "tcp"}],
            })
        return recs


class FakeCtx:
    def __init__(self, tmp_path, docker):
        self.docker = docker
        self.deployment_store = DeploymentStore(str(tmp_path))
        self._task = "t"

    def require_docker(self):
        return self.docker

    def log(self, task_id, line):
        return line


def _save_compose(store, dep_id="dep-compose-1", project="uaht-web-abc123"):
    store.save({
        "deployment_id": dep_id,
        "project_id": "proj-1",
        "project_name": "web",
        "version": "1.0.0",
        "container_name": None,
        "compose_project": project,
        "compose_file": "/x/docker-compose.yml",
        "compose_ports": [18081],
        "runtime": "docker-compose",
        "host_port": 18081,
        "status": "running",
    })
    return project


def _save_docker(store, dep_id="dep-docker-1", name="uaht-web-1-0-0-abc"):
    store.save({
        "deployment_id": dep_id,
        "project_id": "proj-1",
        "project_name": "web",
        "version": "1.0.0",
        "container_name": name,
        "runtime": "docker",
        "host_port": 18080,
        "status": "running",
    })
    return name


def _task(dep_id):
    return {"id": "t-1", "type": "x", "payload": {"deployment_id": dep_id}}


# ---------------------------------------------------------------------------
# restart / stop / start
# ---------------------------------------------------------------------------
def test_restart_compose_project(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    project = _save_compose(ctx.deployment_store)
    docker.compose_projects[project] = [f"{project}-web-1"]

    result = handlers.handle_restart(ctx, _task("dep-compose-1"))

    assert result["status"] == "restarted"
    assert result["compose_project"] == project
    assert ("compose_restart", project) in docker.calls


def test_stop_compose_project_marks_stopped(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    project = _save_compose(ctx.deployment_store)
    docker.compose_projects[project] = [f"{project}-web-1"]
    docker.containers[f"{project}-web-1"] = {"running": True}

    result = handlers.handle_stop(ctx, _task("dep-compose-1"))

    assert result["status"] == "stopped"
    assert ("compose_stop", project) in docker.calls
    assert ctx.deployment_store.load("dep-compose-1")["status"] == "stopped"


def test_start_compose_project_marks_running(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    project = _save_compose(ctx.deployment_store, dep_id="dep-c2")
    docker.compose_projects[project] = [f"{project}-web-1"]
    docker.containers[f"{project}-web-1"] = {"running": False}
    ctx.deployment_store.load("dep-c2")["status"] = "stopped"
    st = ctx.deployment_store.load("dep-c2")
    st["status"] = "stopped"
    ctx.deployment_store.save(st)

    result = handlers.handle_start(ctx, _task("dep-c2"))

    assert result["status"] == "running"
    assert ("compose_start", project) in docker.calls
    assert ctx.deployment_store.load("dep-c2")["status"] == "running"


def test_restart_stop_start_docker_path_unchanged(tmp_path):
    """The single-container path must behave exactly as before."""
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    name = _save_docker(ctx.deployment_store)

    r = handlers.handle_restart(ctx, _task("dep-docker-1"))
    assert r == {"deployment_id": "dep-docker-1", "container": name,
                 "status": "restarted"}
    assert ("restart_container", name) in docker.calls

    s = handlers.handle_stop(ctx, _task("dep-docker-1"))
    assert s["status"] == "stopped" and s["container"] == name
    assert ctx.deployment_store.load("dep-docker-1")["status"] == "stopped"

    st = handlers.handle_start(ctx, _task("dep-docker-1"))
    assert st["status"] == "running"
    assert ctx.deployment_store.load("dep-docker-1")["status"] == "running"


# ---------------------------------------------------------------------------
# logs / status
# ---------------------------------------------------------------------------
def test_logs_compose_project_returns_per_container_logs(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    project = _save_compose(ctx.deployment_store)
    c1, c2 = f"{project}-web-1", f"{project}-db-1"
    docker.compose_projects[project] = [c1, c2]

    result = handlers.handle_logs(ctx, _task("dep-compose-1"))

    assert result["compose_project"] == project
    assert set(result["logs"]) == {c1, c2}
    assert result["logs"][c1] == f"logs-of-{c1}\n"


def test_logs_docker_path_unchanged(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    name = _save_docker(ctx.deployment_store)

    result = handlers.handle_logs(ctx, _task("dep-docker-1"))
    assert result == {"deployment_id": "dep-docker-1",
                      "logs": f"logs-of-{name}\n"}


def test_status_compose_project_all_running(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    project = _save_compose(ctx.deployment_store)
    c1 = f"{project}-web-1"
    docker.compose_projects[project] = [c1]
    docker.containers[c1] = {"running": True}

    result = handlers.handle_status(ctx, _task("dep-compose-1"))

    assert result["compose_project"] == project
    assert result["status"] == "running"
    assert result["containers"][c1]["status"] == "running"
    assert result["containers"][c1]["ports"] == {"3000": "18081"}


def test_status_compose_project_partial(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    project = _save_compose(ctx.deployment_store)
    c1, c2 = f"{project}-web-1", f"{project}-db-1"
    docker.compose_projects[project] = [c1, c2]
    docker.containers[c1] = {"running": True}
    docker.containers[c2] = {"running": False}

    result = handlers.handle_status(ctx, _task("dep-compose-1"))

    assert result["status"] == "partial"
    assert result["containers"][c1]["status"] == "running"
    assert result["containers"][c2]["status"] == "exited"


def test_status_docker_path_unchanged(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)
    name = _save_docker(ctx.deployment_store)
    docker.containers[name] = {"running": True}

    result = handlers.handle_status(ctx, _task("dep-docker-1"))
    assert result["container"] == name
    assert result["status"] == "running"
    assert result["ports"] == {"3000/tcp": "18080"}


# ---------------------------------------------------------------------------
# DockerClient: compose lifecycle argv (no new flags, scrubbed env)
# ---------------------------------------------------------------------------
def _bare_client():
    from docker.client import DockerClient
    client = DockerClient.__new__(DockerClient)  # skip PATH check
    client.binary = "docker"
    return client


def test_compose_lifecycle_argv_and_env(monkeypatch):
    from docker import client as client_mod
    client = _bare_client()
    seen = {}

    def fake_run(self, *argv, timeout=None, check=True, capture=True,
                 env=None):
        seen["argv"] = list(argv)
        seen["env"] = env
        seen["timeout"] = timeout

        class P:
            stdout = "ok"
        return P()

    monkeypatch.setattr(client_mod.DockerClient, "_run", fake_run)
    monkeypatch.setattr(client_mod, "compose_subprocess_env",
                        lambda: {"PATH": "/x"})
    # project names are sanitized exactly like every other compose call
    client.compose_restart("UAHT-Web_ABC123")
    assert seen["argv"] == ["compose", "-p", "uaht-web_abc123", "restart"]
    assert seen["env"] == {"PATH": "/x"}

    client.compose_stop("uaht-web-abc123")
    assert seen["argv"] == ["compose", "-p", "uaht-web-abc123", "stop"]

    client.compose_start("uaht-web-abc123")
    assert seen["argv"] == ["compose", "-p", "uaht-web-abc123", "start"]


# ---------------------------------------------------------------------------
# GC parity: old compose generations are torn down via compose down
# ---------------------------------------------------------------------------
def test_gc_collects_old_compose_generation(tmp_path):
    from deployments import gc as gc_mod
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker)

    def gen(dep_id, project, status, seq):
        ctx.deployment_store.save({
            "deployment_id": dep_id,
            "project_id": "proj-gc",
            "project_name": "web",
            "version": "1.0.0",
            "container_name": None,
            "compose_project": project,
            "compose_file": "/x/docker-compose.yml",
            "runtime": "docker-compose",
            "status": status,
            "created_at": f"2026-10-06T00:00:0{seq}Z",
            "seq": seq,
        })

    # newest first: gen-3 (running, kept), gen-2 (superseded, kept),
    # gen-1 (superseded, doomed with keep=2)
    gen("dep-gc-1", "uaht-web-1", "superseded", 1)
    gen("dep-gc-2", "uaht-web-2", "superseded", 2)
    gen("dep-gc-3", "uaht-web-3", "running", 3)

    summary = gc_mod.collect_garbage(ctx, keep=2)

    assert ("uaht-web-1",) in [
        (p,) for (_f, p) in docker.compose_down_calls]
    assert not any(p == "uaht-web-3" for (_f, p) in
                   docker.compose_down_calls)
    assert not any(p == "uaht-web-2" for (_f, p) in
                   docker.compose_down_calls)
    assert "compose:uaht-web-1" in summary["removed_containers"]
