"""Phase 4 (multi-application hosting) tests with a fake docker client.

The fake implements the DockerClient interface used by the pipeline,
handlers and GC — the real docker module is never stubbed. Scenarios:

  (a) multi-app E2E: deploy app-a/app-b/app-c; assert unique container
      names, dirs, ports and separate logs; stopping app-b leaves a and c
      running; removing app-a does not touch b/c containers or files.
  (b) compose rollback: a failed healthcheck tears down the new compose
      stack and restores the previous one via its stored compose file.
  (c) GC: after 4 successful deploys with keep=2 only 2 generations of
      containers/images remain; images still referenced by a kept
      generation are never removed; prebuilt (not worker-built) images are
      never removed; a container docker still reports as running is
      skipped.
  (d) port collision: a second compose deployment declaring the same host
      port fails BEFORE `compose up`.
Plus unit tests for the compose port parsing/discovery helpers and the
compose name-matching hardening.
"""
import hashlib
import io
import json
import socket
import tarfile
from pathlib import Path

import pytest

from deployments import gc, pipeline
from deployments import reconcile
from deployments.state import DeploymentStore
from executor import handlers
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDocker:
    """In-memory stand-in for docker.client.DockerClient, with compose."""

    def __init__(self):
        self.calls = []
        self.containers = {}      # name -> {"image", "running", "ports"{host:ctr}}
        self.images = set()       # tags this worker "built"
        self.run_calls = []
        self.build_calls = []
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []
        self.rmi_calls = []
        self.compose_up_calls = []    # (compose_file, project_name, build)
        self.compose_down_calls = []  # (compose_file, project_name)
        self.compose_projects = {}    # project -> {"file", "containers"}
        self.next_compose_ports = []  # host ports the next compose_up exposes
        self.compose_config_models = {}
        self.ps_rows = []
        self.ps_records_override = None
        self.inspect_ports = {}

    def _record(self, name, *args):
        self.calls.append((name, args))

    def compose_available(self):
        self._record("compose_available")
        return True

    def build(self, context_dir, dockerfile, tag, build_args=None, timeout=1200):
        self._record("build", tag)
        self.build_calls.append(tag)
        self.images.add(tag)
        return "fake build output"

    def image_exists(self, tag):
        self._record("image_exists", tag)
        return tag in self.images

    def remove_image(self, tag):
        self._record("remove_image", tag)
        self.rmi_calls.append(tag)
        self.images.discard(tag)

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self._record("run", name)
        self.run_calls.append({"name": name, "image": image,
                               "ports": dict(ports or {})})
        self.containers[name] = {"image": image, "running": True,
                                 "ports": dict(ports or {})}
        return "cid-" + name

    def start(self, name, timeout=120):
        self._record("start", name)
        self.start_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = True

    def stop(self, name, timeout_secs=10, timeout=120):
        self._record("stop", name)
        self.stop_calls.append(name)
        if name in self.containers:
            self.containers[name]["running"] = False
        for proj in self.compose_projects.values():
            if name in proj["containers"]:
                proj["containers"][name]["running"] = False

    def rm(self, name, force=False, timeout=120):
        self._record("rm", name)
        self.rm_calls.append(name)
        self.containers.pop(name, None)
        for proj in self.compose_projects.values():
            proj["containers"].pop(name, None)

    def container_exists(self, name):
        self._record("container_exists", name)
        return name in self.containers

    def container_status(self, name):
        self._record("container_status", name)
        c = self.containers.get(name)
        if c is None:
            return None
        return "running" if c["running"] else "exited"

    def logs(self, name, tail=500):
        self._record("logs", name)
        return "fake logs\n"

    def inspect(self, name):
        self._record("inspect", name)
        ports = self.inspect_ports.get(name)
        if ports is None:
            ports = (self.containers.get(name) or {}).get("ports", {})
        bindings = {f"{cp}/tcp": [{"HostIp": "0.0.0.0",
                                   "HostPort": str(hp)}]
                    for hp, cp in ports.items()}
        return [{"Config": {"Image": (self.containers.get(name) or {}).get("image")},
                 "State": {"Status": "running"},
                 "NetworkSettings": {"Ports": bindings}}]

    def ps(self, all=False):
        self._record("ps", all)
        rows = list(self.ps_rows)
        for name, c in self.containers.items():
            if c.get("ports"):
                cell = ", ".join(f"0.0.0.0:{hp}->{cp}/tcp"
                                 for hp, cp in c["ports"].items())
                rows.append({"Names": "/" + name, "Ports": cell})
        for _proj, data in self.compose_projects.items():
            for name, c in data["containers"].items():
                cell = ", ".join(f"0.0.0.0:{hp}->{cp}/tcp"
                                 for hp, cp in c.get("ports", {}).items())
                rows.append({"Names": "/" + name, "Ports": cell})
        return rows

    def compose_up(self, compose_file, project_name=None, build=False,
                   timeout=1200):
        self._record("compose_up", project_name)
        self.compose_up_calls.append((compose_file, project_name, build))
        ports = {p: 3000 for p in self.next_compose_ports}
        cname = f"{project_name}-web-1"
        self.compose_projects[project_name] = {
            "file": compose_file,
            "containers": {cname: {"ports": ports, "running": True}},
        }
        return "fake compose up"

    def compose_down(self, compose_file, project_name=None, timeout=300):
        self._record("compose_down", project_name)
        self.compose_down_calls.append((compose_file, project_name))
        self.compose_projects.pop(project_name, None)
        return "fake compose down"

    def compose_ps(self, project_name, timeout=60):
        self._record("compose_ps", project_name)
        if self.ps_records_override is not None:
            return self.ps_records_override
        data = self.compose_projects.get(project_name)
        if not data:
            return []
        recs = []
        for name, c in data["containers"].items():
            recs.append({
                "Name": name,
                "State": "running" if c["running"] else "exited",
                "Publishers": [{"PublishedPort": str(hp), "TargetPort": cp,
                                "Protocol": "tcp"}
                               for hp, cp in c["ports"].items()],
            })
        return recs

    def compose_config_json(self, compose_file, timeout=60):
        self._record("compose_config_json", compose_file)
        if compose_file in self.compose_config_models:
            return self.compose_config_models[compose_file]
        return {"services": {"web": {"ports": [
            {"published": p, "target": 3000}
            for p in self.next_compose_ports]}}}


class FakeAPI:
    def __init__(self, artifact_bytes: bytes):
        self.artifact_bytes = artifact_bytes

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(self.artifact_bytes)
        return dest_path


class FakeConfig:
    def __init__(self, work_dir):
        self.work_dir = str(work_dir)
        self.apps_dir = str(work_dir / "apps")


class FakeCtx:
    def __init__(self, tmp_path, docker, api):
        self.config = FakeConfig(tmp_path)
        self.api = api
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
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tarball(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            raw = data.encode("utf-8")
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


def _manifest(name, runtime, port=3000):
    return {
        "name": name,
        "runtime": runtime,
        "service": {"port": port, "healthcheck": "/health"},
        "restart": "unless-stopped",
        "env": {"NODE_ENV": "production"},
    }


def _docker_deploy_task(project, version, dep_id, image=None,
                        artifact_id=None, artifact_checksum=None,
                        requested_host_port=None):
    payload = {
        "project_id": f"proj-{project}",
        "project_name": project,
        "version": version,
        "deployment_id": dep_id,
        "manifest": _manifest(project, "docker"),
        "healthcheck_timeout": 3,
    }
    if image:
        payload["image"] = image
    if artifact_id:
        payload["artifact_id"] = artifact_id
        payload["artifact_checksum"] = artifact_checksum
    if requested_host_port is not None:
        payload["requested_host_port"] = requested_host_port
    return {"id": f"task-{dep_id}", "type": "deploy", "payload": payload}


def _compose_artifact(project, port_decl="8080:80"):
    compose_yml = (
        "services:\n"
        "  web:\n"
        "    image: nginx:alpine\n"
        f"    ports:\n      - \"{port_decl}\"\n"
    )
    return _tarball({
        "docker-compose.yml": compose_yml,
        "agent.deploy.json": json.dumps(_manifest(project, "docker-compose")),
    })


def _compose_deploy_task(project, version, dep_id, artifact_id, checksum):
    return {"id": f"task-{dep_id}", "type": "deploy", "payload": {
        "project_id": f"proj-{project}",
        "project_name": project,
        "version": version,
        "deployment_id": dep_id,
        "artifact_id": artifact_id,
        "artifact_checksum": checksum,
        "healthcheck_timeout": 3,
    }}


def _healthy(monkeypatch):
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)


def _unhealthy(monkeypatch):
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: False)


# ---------------------------------------------------------------------------
# (a) multi-app E2E: isolation between deployments of different projects
# ---------------------------------------------------------------------------
def test_multi_app_deployments_are_isolated(tmp_path, monkeypatch):
    _healthy(monkeypatch)
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    ports = [_free_port() for _ in range(3)]

    for proj, dep, port in (("app-a", "dep-a1", ports[0]),
                            ("app-b", "dep-b1", ports[1]),
                            ("app-c", "dep-c1", ports[2])):
        result = pipeline.deploy(
            ctx, _docker_deploy_task(proj, "1.0.0", dep,
                                     image=f"img-{proj}:1.0.0",
                                     requested_host_port=port))
        assert result["status"] == "running"

    states = {d: ctx.deployment_store.load(d)
              for d in ("dep-a1", "dep-b1", "dep-c1")}

    # unique container names, dirs and ports per deployment
    names = [s["container_name"] for s in states.values()]
    assert len(set(names)) == 3
    assert all(n.startswith("uaht-") for n in names)
    host_ports = [s["host_port"] for s in states.values()]
    assert host_ports == ports and len(set(host_ports)) == 3
    dirs = [ctx.deployment_store.deployment_dir(d) for d in states]
    assert len({str(d) for d in dirs}) == 3
    assert all(Path(d, "state.json").exists() for d in dirs)
    # extraction/source dirs (had there been artifacts) live under the
    # deployment dir, never shared
    assert all(str(d).startswith(str(ctx.deployment_store.root))
               for d in dirs)

    # separate per-deployment logs
    for dep, proj in (("dep-a1", "app-a"), ("dep-b1", "app-b"),
                      ("dep-c1", "app-c")):
        body = ctx.log_store.tail_deployment(dep)
        assert f"project={proj}" in body
        assert "app-b" not in body or proj == "app-b"

    # stopping app-b leaves a and c running (scoped by stored name)
    handlers.handle_stop(ctx, {"id": "t-stop", "type": "stop",
                               "payload": {"deployment_id": "dep-b1"}})
    assert docker.containers[states["dep-b1"]["container_name"]]["running"] is False
    assert docker.containers[states["dep-a1"]["container_name"]]["running"] is True
    assert docker.containers[states["dep-c1"]["container_name"]]["running"] is True

    # removing app-a touches neither b's nor c's containers, dirs or files
    handlers.handle_remove(ctx, {"id": "t-rm", "type": "remove",
                                 "payload": {"deployment_id": "dep-a1"}})
    assert states["dep-a1"]["container_name"] not in docker.containers
    assert states["dep-b1"]["container_name"] in docker.containers
    assert states["dep-c1"]["container_name"] in docker.containers
    for dep in ("dep-b1", "dep-c1"):
        assert (ctx.deployment_store.root / dep / "state.json").exists()
    assert ctx.deployment_store.load("dep-b1")["status"] == "stopped"
    assert ctx.deployment_store.load("dep-c1")["status"] == "running"
    assert ctx.deployment_store.load("dep-a1")["status"] == "removed"


def test_multi_app_same_version_redeploy_gets_unique_name(tmp_path, monkeypatch):
    _healthy(monkeypatch)
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    pipeline.deploy(ctx, _docker_deploy_task("app-a", "1.0.0", "dep-a1",
                                             image="img-a:1.0.0",
                                             requested_host_port=_free_port()))
    pipeline.deploy(ctx, _docker_deploy_task("app-a", "1.0.0", "dep-a2",
                                             image="img-a:1.0.0",
                                             requested_host_port=_free_port()))
    n1 = ctx.deployment_store.load("dep-a1")["container_name"]
    n2 = ctx.deployment_store.load("dep-a2")["container_name"]
    assert n1 != n2
    # both generations exist; the old one was only stopped, never removed
    assert n1 in docker.containers and n2 in docker.containers


# ---------------------------------------------------------------------------
# (b) compose rollback: failed healthcheck tears down the new stack and
#     restores the previous one
# ---------------------------------------------------------------------------
def test_compose_rollback_tears_down_new_and_restores_previous(
        tmp_path, monkeypatch):
    docker = FakeDocker()
    port = _free_port()
    docker.next_compose_ports = [port]
    art_v1 = _compose_artifact("capp")
    sum_v1 = "sha256:" + hashlib.sha256(art_v1).hexdigest()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art_v1))

    _healthy(monkeypatch)
    pipeline.deploy(ctx, _compose_deploy_task("capp", "1.0.0", "cdep-v1",
                                              "art-v1", sum_v1))
    v1_state = ctx.deployment_store.load("cdep-v1")
    assert v1_state["status"] == "running"
    assert v1_state["compose_project"] == "uaht-capp"
    assert v1_state["container_name"] is None
    assert v1_state["compose_file"] and Path(v1_state["compose_file"]).is_file()
    v1_file = v1_state["compose_file"]
    assert docker.compose_up_calls[-1][1] == "uaht-capp"

    # v2 deploy with a failing healthcheck
    art_v2 = _compose_artifact("capp")
    sum_v2 = "sha256:" + hashlib.sha256(art_v2).hexdigest()
    ctx.api = FakeAPI(art_v2)
    _unhealthy(monkeypatch)
    with pytest.raises(pipeline.DeployError, match="healthcheck failed"):
        pipeline.deploy(ctx, _compose_deploy_task("capp", "2.0.0", "cdep-v2",
                                                  "art-v2", sum_v2))

    # the NEW unhealthy stack was torn down via compose down ...
    down_projects = [p for _f, p in docker.compose_down_calls]
    assert "uaht-capp" in down_projects
    # ... and the PREVIOUS stack was restored via compose up with v1's file
    assert docker.compose_up_calls[-1] == (v1_file, "uaht-capp", True)

    v2_state = ctx.deployment_store.load("cdep-v2")
    assert v2_state["status"] == "rolled_back"
    assert v2_state["rollback_of"] == "cdep-v1"
    assert ctx.deployment_store.load("cdep-v1")["status"] == "running"
    # the restored stack is live in the fake
    assert "uaht-capp" in docker.compose_projects


def test_compose_rollback_without_previous_fails_clean(tmp_path, monkeypatch):
    docker = FakeDocker()
    docker.next_compose_ports = [_free_port()]
    art = _compose_artifact("solo")
    ctx = FakeCtx(tmp_path, docker,
                   FakeAPI(art))
    _unhealthy(monkeypatch)
    with pytest.raises(pipeline.DeployError, match="no previous healthy"):
        pipeline.deploy(ctx, _compose_deploy_task(
            "solo", "1.0.0", "cdep-s1", "art-s1",
            "sha256:" + hashlib.sha256(art).hexdigest()))
    # the failed stack was still torn down
    assert docker.compose_down_calls, "compose down must run on failure"
    assert ctx.deployment_store.load("cdep-s1")["status"] == "failed"


def test_compose_redeploy_same_ports_skips_own_port_check(tmp_path, monkeypatch):
    """Redeploying the same compose project must not fail its own port
    check: `compose up` replaces the stack in place."""
    docker = FakeDocker()
    port = _free_port()
    docker.next_compose_ports = [port]
    art = _compose_artifact("capp")
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    _healthy(monkeypatch)
    pipeline.deploy(ctx, _compose_deploy_task("capp", "1.0.0", "cdep-r1",
                                              "art-r1", checksum))
    ctx.api = FakeAPI(art)
    result = pipeline.deploy(ctx, _compose_deploy_task("capp", "1.0.0",
                                                       "cdep-r2", "art-r2",
                                                       checksum))
    assert result["status"] == "running"
    assert len(docker.compose_up_calls) == 2


# ---------------------------------------------------------------------------
# (c) GC: keep N newest generations per project
# ---------------------------------------------------------------------------
def _build_artifact(project):
    return _tarball({
        "Dockerfile": "FROM nginx:alpine\n",
        "agent.deploy.json": json.dumps(_manifest(project, "docker")),
    })


def test_gc_keeps_two_generations_after_four_deploys(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "2")
    _healthy(monkeypatch)
    docker = FakeDocker()
    art = _build_artifact("gcapp")
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))

    for i in range(1, 5):
        pipeline.deploy(ctx, _docker_deploy_task(
            "gcapp", f"{i}.0.0", f"g{i}",
            artifact_id=f"art-g{i}", artifact_checksum=checksum))

    # only the 2 newest generations' containers/images survive
    assert set(docker.containers) == {"uaht-gcapp-3.0.0-g3",
                                      "uaht-gcapp-4.0.0-g4"}
    assert docker.images == {"uaht-gcapp:3.0.0", "uaht-gcapp:4.0.0"}
    assert "uaht-gcapp:1.0.0" in docker.rmi_calls
    assert "uaht-gcapp:2.0.0" in docker.rmi_calls
    assert "uaht-gcapp-1.0.0-g1" in docker.rm_calls
    assert "uaht-gcapp-2.0.0-g2" in docker.rm_calls
    # state dirs are kept (the registry is not pruned, only containers/images)
    assert (ctx.deployment_store.root / "g1" / "state.json").exists()
    assert ctx.deployment_store.load("g4")["status"] == "running"
    assert ctx.deployment_store.load("g3")["status"] == "superseded"


def test_gc_never_removes_image_referenced_by_kept_generation(
        tmp_path, monkeypatch):
    """Same-version redeploys share the image tag: the tag is referenced by
    a kept generation, so rmi must not run even though the old generation
    is collected."""
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "2")
    _healthy(monkeypatch)
    docker = FakeDocker()
    art = _build_artifact("happ")
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))

    for i, dep in enumerate(("h1", "h2", "h3"), start=1):
        pipeline.deploy(ctx, _docker_deploy_task(
            "happ", "1.0.0", dep,
            artifact_id=f"art-{dep}", artifact_checksum=checksum))

    assert "uaht-happ-1.0.0-h1" in docker.rm_calls  # old container collected
    assert docker.rmi_calls == []                   # ...but the image is shared
    assert "uaht-happ:1.0.0" in docker.images


def test_gc_never_removes_prebuilt_images(tmp_path, monkeypatch):
    """Images the worker did not build (prebuilt `image:` payloads) are
    never rmi'd, even when their generation is collected."""
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "1")
    _healthy(monkeypatch)
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    pipeline.deploy(ctx, _docker_deploy_task("papp", "1.0.0", "p1",
                                             image="external/app:9.9",
                                             requested_host_port=_free_port()))
    pipeline.deploy(ctx, _docker_deploy_task("papp", "2.0.0", "p2",
                                             image="external/app:9.9",
                                             requested_host_port=_free_port()))
    assert "uaht-papp-1.0.0-p1" in docker.rm_calls
    assert docker.rmi_calls == []


def test_gc_skips_container_still_running(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "1")
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    old = {
        "deployment_id": "old-1", "task_id": "t0", "project_id": "proj-x",
        "project_name": "x", "version": "1.0.0", "container_name": "c-old",
        "image": "uaht-x:1.0.0", "image_built": True, "runtime": "docker",
        "status": "superseded", "created_at": "2026-10-01T00:00:00Z",
    }
    new = dict(old, deployment_id="new-1", container_name="c-new",
               status="running", created_at="2026-10-02T00:00:00Z")
    ctx.deployment_store.save(old)
    ctx.deployment_store.save(new)
    docker.containers["c-old"] = {"image": "uaht-x:1.0.0", "running": True,
                                  "ports": {}}
    docker.images.add("uaht-x:1.0.0")

    summary = gc.collect_garbage(ctx)
    assert "c-old" in docker.containers          # not removed: still running
    assert "uaht-x:1.0.0" in docker.images       # ...so its image stays too
    assert summary["removed_containers"] == []


def test_gc_is_per_project(tmp_path, monkeypatch):
    """GC on one project never touches another project's containers."""
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "1")
    _healthy(monkeypatch)
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    pipeline.deploy(ctx, _docker_deploy_task("proj-a", "1.0.0", "pa1",
                                             image="img-pa:1.0.0",
                                             requested_host_port=_free_port()))
    pipeline.deploy(ctx, _docker_deploy_task("proj-b", "1.0.0", "pb1",
                                             image="img-pb:1.0.0",
                                             requested_host_port=_free_port()))
    pipeline.deploy(ctx, _docker_deploy_task("proj-a", "2.0.0", "pa2",
                                             image="img-pa:2.0.0",
                                             requested_host_port=_free_port()))
    name_b = ctx.deployment_store.load("pb1")["container_name"]
    assert name_b in docker.containers  # proj-b untouched by proj-a's GC
    assert ctx.deployment_store.load("pa1")["container_name"] \
        not in docker.containers


def test_keep_generations_env_parsing(monkeypatch):
    monkeypatch.delenv("DEPLOY_KEEP_GENERATIONS", raising=False)
    assert gc.keep_generations() == 2
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "5")
    assert gc.keep_generations() == 5
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "0")
    assert gc.keep_generations() == 1  # clamped: never collect the newest
    monkeypatch.setenv("DEPLOY_KEEP_GENERATIONS", "bogus")
    assert gc.keep_generations() == 2


# ---------------------------------------------------------------------------
# (d) port collision: compose deployment declaring a taken port fails
#     BEFORE `compose up`
# ---------------------------------------------------------------------------
def test_compose_port_collision_fails_before_compose_up(tmp_path, monkeypatch):
    docker = FakeDocker()
    port = _free_port()
    docker.next_compose_ports = [port]
    art_x = _compose_artifact("xapp")
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art_x))
    _healthy(monkeypatch)
    pipeline.deploy(ctx, _compose_deploy_task(
        "xapp", "1.0.0", "xdep-1", "art-x",
        "sha256:" + hashlib.sha256(art_x).hexdigest()))
    assert len(docker.compose_up_calls) == 1

    # a DIFFERENT project declaring the same host port must fail before up
    art_y = _compose_artifact("yapp")
    ctx.api = FakeAPI(art_y)
    with pytest.raises(pipeline.DeployError, match="already published"):
        pipeline.deploy(ctx, _compose_deploy_task(
            "yapp", "1.0.0", "ydep-1", "art-y",
            "sha256:" + hashlib.sha256(art_y).hexdigest()))
    assert len(docker.compose_up_calls) == 1  # no second stack was started


# ---------------------------------------------------------------------------
# Unit tests: compose port parsing / discovery / name matching
# ---------------------------------------------------------------------------
def test_parse_compose_published_ports():
    config = {"services": {
        "web": {"ports": ["8080:80", "127.0.0.1:8443:443",
                          {"published": 9090, "target": 90},
                          {"published": "9091", "target": 91},
                          "3000"]},          # ephemeral: no fixed host port
        "worker": {"ports": []},
        "db": {},
    }}
    assert pipeline.parse_compose_published_ports(config) == [8080, 8443, 9090, 9091]
    assert pipeline.parse_compose_published_ports({}) == []
    assert pipeline.parse_compose_published_ports(None) == []


def test_published_ports_from_compose_ps():
    rec = {"Name": "uaht-x-web-1",
           "Publishers": [{"PublishedPort": "8080", "TargetPort": 80},
                          {"PublishedPort": "bad", "TargetPort": 81}]}
    assert pipeline._published_ports_from_compose_ps(rec) == [8080]
    assert pipeline._published_ports_from_compose_ps({}) == []


def test_published_ports_from_inspect():
    info = [{"NetworkSettings": {"Ports": {
        "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
        "81/tcp": None}}}]
    assert pipeline._published_ports_from_inspect(info) == [8080]


def test_discover_compose_port_prefers_publishers_then_inspect():
    docker = FakeDocker()
    docker.ps_records_override = [
        {"Name": "uaht-z-web-1",
         "Publishers": [{"PublishedPort": "8123", "TargetPort": 80}]}]
    assert pipeline._discover_compose_port(docker, "uaht-z") == 8123

    # no Publishers -> inspect fallback
    docker.ps_records_override = [{"Name": "uaht-z-web-1"}]
    docker.inspect_ports = {"uaht-z-web-1": {8222: 80}}
    assert pipeline._discover_compose_port(docker, "uaht-z") == 8222

    docker.ps_records_override = []
    assert pipeline._discover_compose_port(docker, "uaht-z") is None


def test_compose_name_matching_no_prefix_collision():
    assert reconcile._compose_name_matches("uaht-proj", "uaht-proj-web-1")
    assert reconcile._compose_name_matches("uaht-proj", "uaht-proj")
    assert not reconcile._compose_name_matches("uaht-proj", "uaht-proj2-web-1")
    assert not reconcile._compose_name_matches("uaht-proj", "other-web-1")


def test_decide_rollback_compose_previous():
    assert pipeline.decide_rollback(False, {"compose_project": "uaht-x"}) \
        == "rollback_to_previous"
    assert pipeline.decide_rollback(False, {"container_name": None,
                                            "compose_project": None}) \
        == "fail_clean"


# ---------------------------------------------------------------------------
# handle_rollback with a compose target
# ---------------------------------------------------------------------------
def test_handler_rollback_restores_compose_target(tmp_path):
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  web:\n    image: x\n", encoding="utf-8")
    ctx.deployment_store.save({
        "deployment_id": "cur", "task_id": "t0", "project_id": "proj-1",
        "project_name": "capp", "version": "2.0.0",
        "container_name": "uaht-capp-2.0.0-cur", "runtime": "docker",
        "status": "running", "created_at": "2026-10-02T00:00:00Z",
    })
    ctx.deployment_store.save({
        "deployment_id": "tgt", "task_id": "t0", "project_id": "proj-1",
        "project_name": "capp", "version": "1.0.0",
        "container_name": None, "compose_project": "uaht-capp",
        "compose_file": str(compose_file), "runtime": "docker-compose",
        "status": "superseded", "created_at": "2026-10-01T00:00:00Z",
    })
    docker.containers["uaht-capp-2.0.0-cur"] = {"image": "img:2",
                                               "running": True, "ports": {}}

    result = handlers.handle_rollback(
        ctx, {"id": "t1", "type": "rollback",
              "payload": {"deployment_id": "cur",
                          "target_deployment_id": "tgt"}})
    assert result["rolled_back_to"] == "tgt"
    # current container torn down (scoped to its own recorded name)
    assert "uaht-capp-2.0.0-cur" not in docker.containers
    # compose target restored via compose up with its stored file
    assert docker.compose_up_calls == [(str(compose_file), "uaht-capp", True)]
    assert ctx.deployment_store.load("tgt")["status"] == "running"
