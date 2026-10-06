"""W5 (docker lockdown) + W7 (compose first-class) security tests.

Covers:
  * deployments.compose_validate — the explicit DENY list against the
    normalized compose model (privileged, host namespaces, devices,
    cap_add, security_opt, bind mounts escaping the workspace, docker
    socket mounts, env_file/build/extends/config/secret file escapes,
    malformed resource limits). Benign compose files pass.
  * pipeline.deploy (docker-compose runtime) — a malicious compose file
    fails BEFORE `compose up` with zero docker side effects; benign
    single-service and multi-service compose apps deploy through the real
    pipeline; declared ports land in the port registry.
  * executor.handlers.handle_docker_compose — same validation gate for
    the raw task path.
  * docker.client — explicit run-arg validation (restart/memory/cpus/
    ports), build-arg key validation, and the scrubbed compose
    subprocess environment (WORKER_HOST_TOKEN must not leak into
    ${VAR} interpolation).
"""
import hashlib
import io
import json
import os
import socket
import tarfile
from pathlib import Path

import pytest

from deployments import compose_validate, pipeline
from deployments.state import DeploymentStore
from docker import client as docker_client_mod
from executor import handlers
from logs.store import LogStore


# ---------------------------------------------------------------------------
# Fakes (dependency injection, same style as test_phase4.py)
# ---------------------------------------------------------------------------
class FakeDocker:
    """In-memory stand-in for docker.client.DockerClient, with compose."""

    def __init__(self):
        self.calls = []
        self.compose_up_calls = []    # (compose_file, project_name, build)
        self.compose_down_calls = []  # (compose_file, project_name)
        self.compose_projects = {}
        self.next_compose_ports = []  # host ports the next compose_up exposes
        self.compose_config_models = {}  # compose_file -> normalized model
        self.ps_rows = []

    def _record(self, name, *args):
        self.calls.append((name, args))

    def compose_available(self):
        return True

    def ps(self, all=False):
        self._record("ps", all)
        rows = list(self.ps_rows)
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
        data = self.compose_projects.get(project_name)
        if not data:
            return []
        return [{
            "Name": name,
            "State": "running" if c["running"] else "exited",
            "Publishers": [{"PublishedPort": str(hp), "TargetPort": cp,
                            "Protocol": "tcp"}
                           for hp, cp in c["ports"].items()],
        } for name, c in data["containers"].items()]

    def compose_config_json(self, compose_file, timeout=60):
        self._record("compose_config_json", compose_file)
        if compose_file in self.compose_config_models:
            return self.compose_config_models[compose_file]
        # Default model mirrors the fake's exposed ports.
        return {"services": {"web": {
            "image": "fake:latest",
            "ports": [{"published": p, "target": 3000}
                      for p in self.next_compose_ports],
        }}}

    # -- single-container surface (unused here, but part of the interface)
    def container_exists(self, name):
        return False

    def stop(self, name, timeout_secs=10, timeout=120):
        pass

    def rm(self, name, force=False, timeout=120):
        pass


class FakeAPI:
    def __init__(self, artifact_bytes: bytes):
        self.artifact_bytes = artifact_bytes

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        data = self.artifact_bytes
        if expected_size is not None and len(data) != expected_size:
            raise Exception(f"size mismatch: got {len(data)}, "
                            f"expected {expected_size}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(data)
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


def _manifest(name, runtime="docker-compose"):
    return {"name": name, "runtime": runtime,
            "service": {"port": 3000, "healthcheck": "/"}}


def _compose_deploy_task(project, version, deployment_id, artifact_id,
                         checksum, size, compose_file=None):
    payload = {
        "project_id": "proj-1",
        "project_name": project,
        "version": version,
        "deployment_id": deployment_id,
        "artifact_id": artifact_id,
        "artifact_checksum": checksum,
        "artifact_size": size,
        "manifest": _manifest(project),
        "healthcheck_timeout": 3,
    }
    if compose_file:
        payload["compose_file"] = compose_file
    return {"id": "task-1", "type": "deploy", "payload": payload}


def _healthy(monkeypatch):
    from health import checker
    monkeypatch.setattr(checker, "wait_for_healthcheck",
                        lambda *a, **k: True)


def _register_compose_model(ctx, deployment_id, model):
    """Point the fake's `compose config` at the normalized model for the
    compose file the pipeline will extract for this deployment."""
    path = str(Path(ctx.config.work_dir) / "deployments" / deployment_id
               / "source" / "docker-compose.yml")
    ctx.docker.compose_config_models[path] = model
    return path


def _normalized_model(port, service_flags=None, volumes=None):
    """A `docker compose config --format json`-shaped model."""
    svc = {"image": "evil:latest",
           "ports": [{"published": port, "target": 80}]}
    if service_flags:
        svc.update(service_flags)
    if volumes is not None:
        svc["volumes"] = volumes
    return {"services": {"web": svc}}


# ---------------------------------------------------------------------------
# 1. compose_validate unit tests — the explicit DENY list
# ---------------------------------------------------------------------------
def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_validator_allows_benign_single_service(tmp_path):
    ws = _ws(tmp_path)
    model = {"services": {"web": {
        "image": "myapp:1",
        "ports": ["8080:80"],
        "volumes": ["./data:/data", "dbdata:/var/lib/db"],
        "environment": {"FOO": "bar"},
        "deploy": {"resources": {"limits": {"cpus": "0.5",
                                            "memory": "512m"}}},
    }}}
    assert compose_validate.validate_compose_model(model, ws) == []


def test_validator_allows_tmpfs_and_readonly(tmp_path):
    ws = _ws(tmp_path)
    model = {"services": {"web": {
        "image": "x",
        "tmpfs": ["/run"],
        "read_only": True,
        "volumes": [{"type": "tmpfs", "target": "/cache"}],
    }}}
    assert compose_validate.validate_compose_model(model, ws) == []


@pytest.mark.parametrize("flag", ["privileged"])
def test_validator_denies_privileged(tmp_path, flag):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x", flag: True}}}, ws)
    assert any("privileged" in e and "denied" in e for e in errors)


@pytest.mark.parametrize("key", ["network_mode", "pid", "ipc", "uts"])
def test_validator_denies_host_namespaces(tmp_path, key):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x", key: "host"}}}, ws)
    assert any(key in e and "host namespace" in e for e in errors), errors


@pytest.mark.parametrize("key", ["devices", "cap_add", "security_opt"])
def test_validator_denies_devices_caps_security_opt(tmp_path, key):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x", key: ["whatever"]}}}, ws)
    assert any(key in e and "denied" in e for e in errors), errors


def test_validator_denies_host_bind_mount(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "volumes": ["/:/host", "/etc:/etc-ro:ro"]}}}, ws)
    assert len(errors) == 2
    assert all("escapes the deployment workspace" in e for e in errors)


def test_validator_denies_docker_socket_mount(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {
            "image": "x",
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}}}, ws)
    assert any("docker.sock" in e for e in errors)


def test_validator_denies_docker_socket_smuggled_under_workspace(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "volumes": ["./docker.sock:/sock"]}}}, ws)
    assert any("docker.sock" in e for e in errors)


def test_validator_denies_dotdot_volume_escape(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "volumes": ["../../etc:/x"]}}}, ws)
    assert any("escapes the deployment workspace" in e for e in errors)


def test_validator_denies_long_syntax_bind_escape(tmp_path):
    ws = _ws(tmp_path)
    model = {"services": {"web": {
        "image": "x",
        "volumes": [{"type": "bind", "source": "/etc",
                     "target": "/host-etc"}]}}}
    errors = compose_validate.validate_compose_model(model, ws)
    assert any("escapes the deployment workspace" in e for e in errors)


def test_validator_allows_long_syntax_bind_inside_workspace(tmp_path):
    ws = _ws(tmp_path)
    (ws / "srv").mkdir()
    model = {"services": {"web": {
        "image": "x",
        "volumes": [{"type": "bind", "source": "./srv",
                     "target": "/srv"}]}}}
    assert compose_validate.validate_compose_model(model, ws) == []


def test_validator_denies_symlink_volume_escape(tmp_path):
    ws = _ws(tmp_path)
    (ws / "link").symlink_to("/etc")
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "volumes": ["./link:/data"]}}}, ws)
    assert any("escapes the deployment workspace" in e for e in errors)


def test_validator_denies_top_level_bind_driver_opts(tmp_path):
    ws = _ws(tmp_path)
    model = {
        "services": {"web": {"image": "x", "volumes": ["data:/d"]}},
        "volumes": {"data": {"driver_opts": {"type": "none", "o": "bind",
                                             "device": "/etc"}}},
    }
    errors = compose_validate.validate_compose_model(model, ws)
    assert any("bind device" in e for e in errors)


def test_validator_denies_env_file_escape(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "env_file": ["../../secrets.env"]}}}, ws)
    assert any("env_file" in e and "escapes" in e for e in errors)


def test_validator_allows_env_file_inside_workspace(tmp_path):
    ws = _ws(tmp_path)
    (ws / "app.env").write_text("FOO=bar\n")
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "env_file": ["./app.env"]}}}, ws)
    assert errors == []


def test_validator_denies_build_context_escape(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"build": {"context": "..",
                                        "dockerfile": "Dockerfile"}}}},
        ws, compose_file=ws / "docker-compose.yml")
    assert any("build.context" in e for e in errors)


def test_validator_denies_extends_file_escape(tmp_path):
    ws = _ws(tmp_path)
    errors = compose_validate.validate_compose_model(
        {"services": {"web": {"image": "x",
                              "extends": {"file": "../../base.yml",
                                          "service": "web"}}}},
        ws, compose_file=ws / "sub" / "docker-compose.yml")
    assert any("extends.file" in e for e in errors)


def test_validator_denies_secret_file_escape(tmp_path):
    ws = _ws(tmp_path)
    model = {
        "services": {"web": {"image": "x",
                             "secrets": [{"source": "pw", "target": "pw"}]}},
        "secrets": {"pw": {"file": "/etc/shadow"}},
    }
    errors = compose_validate.validate_compose_model(model, ws)
    assert any("secrets" in e and "escapes" in e for e in errors)


def test_validator_denies_malformed_resource_limits(tmp_path):
    ws = _ws(tmp_path)
    model = {"services": {"web": {
        "image": "x",
        "deploy": {"resources": {"limits": {"memory": "lots",
                                            "cpus": "0.5"}}}}}}
    errors = compose_validate.validate_compose_model(model, ws)
    assert any("memory" in e for e in errors)


def test_validator_denies_bad_build_arg_key(tmp_path):
    ws = _ws(tmp_path)
    model = {"services": {"web": {
        "build": {"context": ".", "args": {"--network": "host"}}}}}
    errors = compose_validate.validate_compose_model(model, ws)
    assert any("build.args key" in e for e in errors)


def test_validator_rejects_non_mapping_services(tmp_path):
    ws = _ws(tmp_path)
    assert compose_validate.validate_compose_model(
        {"services": ["web"]}, ws) == ["compose 'services' must be a mapping"]
    assert compose_validate.validate_compose_model(
        {"services": {}}, ws) == ["compose file defines no services"]


# ---------------------------------------------------------------------------
# 2. pipeline.deploy: malicious compose fails BEFORE compose up
# ---------------------------------------------------------------------------
def _deploy_compose_artifact(tmp_path, project, compose_yaml,
                             compose_file_name="docker-compose.yml"):
    art = _tarball({
        compose_file_name: compose_yaml,
        "agent.deploy.json": json.dumps(_manifest(project)),
    })
    checksum = "sha256:" + hashlib.sha256(art).hexdigest()
    return art, checksum


def test_deploy_rejects_privileged_compose_before_up(tmp_path, monkeypatch):
    port = _free_port()
    evil_yaml = (
        "services:\n  web:\n    image: evil:latest\n"
        f"    ports:\n      - \"{port}:80\"\n    privileged: true\n"
    )
    art, checksum = _deploy_compose_artifact(tmp_path, "evilapp", evil_yaml)
    docker = FakeDocker()
    docker.next_compose_ports = [port]
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    # The normalized model must carry the evil flag — the fake's default
    # model would not.
    _register_compose_model(ctx, "dep-evil",
                            _normalized_model(port, {"privileged": True}))
    _healthy(monkeypatch)
    with pytest.raises(pipeline.DeployError,
                       match="privileged is denied"):
        pipeline.deploy(ctx, _compose_deploy_task(
            "evilapp", "1.0.0", "dep-evil", "art-evil", checksum, len(art)))
    assert docker.compose_up_calls == [], "compose up must never run"
    assert ctx.deployment_store.load("dep-evil") is None


def test_deploy_rejects_host_volume_escape_before_up(tmp_path, monkeypatch):
    port = _free_port()
    evil_yaml = (
        "services:\n  web:\n    image: evil:latest\n"
        f"    ports:\n      - \"{port}:80\"\n"
        "    volumes:\n      - /:/host\n"
    )
    art, checksum = _deploy_compose_artifact(tmp_path, "volapp", evil_yaml)
    docker = FakeDocker()
    docker.next_compose_ports = [port]
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    _register_compose_model(ctx, "dep-vol",
                            _normalized_model(port, volumes=["/:/host"]))
    _healthy(monkeypatch)
    with pytest.raises(pipeline.DeployError, match="escapes the deployment"):
        pipeline.deploy(ctx, _compose_deploy_task(
            "volapp", "1.0.0", "dep-vol", "art-vol", checksum, len(art)))
    assert docker.compose_up_calls == []
    assert ctx.deployment_store.load("dep-vol") is None


def test_deploy_compose_single_service_success_registers_ports(
        tmp_path, monkeypatch):
    port = _free_port()
    good_yaml = (
        "services:\n  web:\n    image: myapp:1\n"
        f"    ports:\n      - \"{port}:3000\"\n"
        "    volumes:\n      - ./data:/data\n"
    )
    art, checksum = _deploy_compose_artifact(tmp_path, "webapp", good_yaml)
    docker = FakeDocker()
    docker.next_compose_ports = [port]
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    _healthy(monkeypatch)
    result = pipeline.deploy(ctx, _compose_deploy_task(
        "webapp", "1.0.0", "dep-web", "art-web", checksum, len(art)))
    assert result["status"] == "running"
    assert len(docker.compose_up_calls) == 1
    _file, project, _build = docker.compose_up_calls[0]
    # §18: project name carries the project_id suffix so two projects whose
    # names sanitize identically ("My App" vs "my-app") never share a stack.
    assert project == "uaht-webapp-proj1"  # project_id "proj-1" -> "proj1"
    state = ctx.deployment_store.load("dep-web")
    assert state["compose_ports"] == [port]
    # W7: declared ports go through the port registry.
    assert port in ctx.deployment_store.used_host_ports()


def test_deploy_compose_multi_service_registers_all_ports(
        tmp_path, monkeypatch):
    port_a, port_b = _free_port(), _free_port()
    good_yaml = (
        "services:\n  web:\n    image: myapp:1\n"
        f"    ports:\n      - \"{port_a}:3000\"\n"
        "  db:\n    image: postgres:16\n"
        f"    ports:\n      - \"{port_b}:5432\"\n"
        "    volumes:\n      - pgdata:/var/lib/postgresql/data\n"
        "volumes:\n  pgdata:\n"
    )
    art, checksum = _deploy_compose_artifact(tmp_path, "multiapp", good_yaml)
    docker = FakeDocker()
    docker.next_compose_ports = [port_a, port_b]
    # Multi-service normalized model: the fake's default only has web —
    # provide the real two-service model.
    model = {"services": {
        "web": {"image": "myapp:1",
                "ports": [{"published": port_a, "target": 3000}]},
        "db": {"image": "postgres:16",
               "ports": [{"published": port_b, "target": 5432}],
               "volumes": ["pgdata:/var/lib/postgresql/data"]},
    }}
    ctx = FakeCtx(tmp_path, docker, FakeAPI(art))
    _register_compose_model(ctx, "dep-multi", model)
    _healthy(monkeypatch)
    result = pipeline.deploy(ctx, _compose_deploy_task(
        "multiapp", "2.0.0", "dep-multi", "art-multi", checksum, len(art)))
    assert result["status"] == "running"
    state = ctx.deployment_store.load("dep-multi")
    assert sorted(state["compose_ports"]) == sorted([port_a, port_b])
    used = ctx.deployment_store.used_host_ports()
    assert port_a in used and port_b in used


def test_deploy_compose_port_collision_with_registry_fails(
        tmp_path, monkeypatch):
    port = _free_port()
    # Another live deployment already reserved this port in the registry
    # (no real bind — only the registry knows).
    docker = FakeDocker()
    ctx = FakeCtx(tmp_path, docker, FakeAPI(b""))
    ctx.deployment_store.save({
        "deployment_id": "dep-other",
        "project_id": "proj-9",
        "project_name": "other",
        "host_port": port,
        "status": "running",
        "created_at": "2026-10-05T00:00:00Z",
    })
    good_yaml = (
        "services:\n  web:\n    image: myapp:1\n"
        f"    ports:\n      - \"{port}:3000\"\n"
    )
    art, checksum = _deploy_compose_artifact(tmp_path, "clashapp", good_yaml)
    docker.next_compose_ports = [port]
    ctx.api = FakeAPI(art)
    _healthy(monkeypatch)
    with pytest.raises(pipeline.DeployError, match="port registry"):
        pipeline.deploy(ctx, _compose_deploy_task(
            "clashapp", "1.0.0", "dep-clash", "art-clash",
            checksum, len(art)))
    assert docker.compose_up_calls == []


# ---------------------------------------------------------------------------
# 3. handle_docker_compose: same gate on the raw task path
# ---------------------------------------------------------------------------
def _write_compose_file(work_dir: Path, name: str, content: str) -> str:
    p = work_dir / name
    p.write_text(content)
    return name


def _handler_ctx(tmp_path):
    docker = FakeDocker()
    return FakeCtx(tmp_path, docker, FakeAPI(b"")), docker


def test_handler_docker_compose_rejects_privileged(tmp_path):
    port = _free_port()
    ctx, docker = _handler_ctx(tmp_path)
    name = _write_compose_file(
        Path(ctx.config.work_dir), "evil-compose.yml",
        "services:\n  web:\n    image: evil:latest\n"
        f"    ports:\n      - \"{port}:80\"\n    privileged: true\n")
    docker.next_compose_ports = [port]
    docker.compose_config_models[str(Path(ctx.config.work_dir) / name)] = \
        _normalized_model(port, {"privileged": True})
    task = {"id": "t1", "type": "docker-compose",
            "payload": {"compose_file": name, "project_name": "evilproj"}}
    with pytest.raises(handlers.HandlerError, match="privileged is denied"):
        handlers.handle_docker_compose(ctx, task)
    assert docker.compose_up_calls == []


def test_handler_docker_compose_rejects_host_mount(tmp_path):
    port = _free_port()
    ctx, docker = _handler_ctx(tmp_path)
    name = _write_compose_file(
        Path(ctx.config.work_dir), "evil2-compose.yml",
        "services:\n  web:\n    image: evil:latest\n"
        f"    ports:\n      - \"{port}:80\"\n"
        "    volumes:\n      - /var/run/docker.sock:/sock\n")
    docker.next_compose_ports = [port]
    docker.compose_config_models[str(Path(ctx.config.work_dir) / name)] = \
        _normalized_model(
            port, volumes=["/var/run/docker.sock:/var/run/docker.sock"])
    task = {"id": "t2", "type": "docker-compose",
            "payload": {"compose_file": name}}
    with pytest.raises(handlers.HandlerError,
                       match="rejected by security policy"):
        handlers.handle_docker_compose(ctx, task)
    assert docker.compose_up_calls == []


def test_handler_docker_compose_rejects_path_traversal(tmp_path):
    ctx, docker = _handler_ctx(tmp_path)
    task = {"id": "t3", "type": "docker-compose",
            "payload": {"compose_file": "../../etc/hostname"}}
    with pytest.raises(handlers.HandlerError, match="escapes"):
        handlers.handle_docker_compose(ctx, task)
    assert docker.compose_up_calls == []


def test_handler_docker_compose_benign_runs(tmp_path):
    port = _free_port()
    ctx, docker = _handler_ctx(tmp_path)
    name = _write_compose_file(
        Path(ctx.config.work_dir), "good-compose.yml",
        "services:\n  web:\n    image: myapp:1\n"
        f"    ports:\n      - \"{port}:3000\"\n")
    docker.next_compose_ports = [port]
    task = {"id": "t4", "type": "docker-compose",
            "payload": {"compose_file": name, "project_name": "goodproj"}}
    result = handlers.handle_docker_compose(ctx, task)
    assert result["status"] == "running"
    assert result["project_name"] == "goodproj"
    assert len(docker.compose_up_calls) == 1


# ---------------------------------------------------------------------------
# 4. docker.client: run-arg validation, build-arg keys, env scrubbing
# ---------------------------------------------------------------------------
def test_validate_run_args_accepts_sane_values():
    docker_client_mod.validate_run_args(
        ports={8080: 80}, memory="256m", cpus="1.5",
        restart="unless-stopped")


@pytest.mark.parametrize("kwargs", [
    {"restart": "sometimes"},
    {"restart": "--privileged"},
    {"memory": "lots"},
    {"memory": "256x"},
    {"cpus": "0"},
    {"cpus": "-2"},
    {"cpus": "many"},
    {"ports": {0: 80}},
    {"ports": {99999: 80}},
    {"ports": {"notaport": 80}},
])
def test_validate_run_args_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        docker_client_mod.validate_run_args(**kwargs)


def test_docker_client_run_validates_before_argv():
    c = docker_client_mod.DockerClient.__new__(docker_client_mod.DockerClient)
    c.binary = "docker"
    calls = []
    c._run = lambda *a, **k: calls.append((a, k)) or \
        type("P", (), {"stdout": "cid"})()
    with pytest.raises(ValueError, match="restart"):
        c.run("n", "img:latest", restart="bogus")
    assert calls == [], "no docker invocation on invalid args"
    c.run("n", "img:latest", ports={8080: 80}, memory="128m")
    argv = calls[0][0]
    assert "--privileged" not in argv
    assert "-v" not in argv and "--volume" not in argv
    assert "--cap-add" not in argv


def test_docker_client_build_rejects_bad_build_arg_key():
    c = docker_client_mod.DockerClient.__new__(docker_client_mod.DockerClient)
    c.binary = "docker"
    c._run = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not run"))
    with pytest.raises(ValueError, match="build-arg key"):
        c.build("/ctx", "/ctx/Dockerfile", "tag",
                build_args={"--privileged": "x"})


def test_compose_subprocess_env_scrubs_secrets(monkeypatch):
    monkeypatch.setenv("WORKER_HOST_TOKEN", "super-secret-token")
    monkeypatch.setenv("HOST_TOKEN", "other-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = docker_client_mod.compose_subprocess_env()
    assert "WORKER_HOST_TOKEN" not in env
    assert "HOST_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_deny_list_documents_all_required_entries():
    src = (Path(docker_client_mod.__file__).read_text())
    for entry in ["--privileged", "--network host", "--pid host",
                  "--ipc host", "--device", "docker.sock", "--cap-add",
                  "--security-opt", "--mount"]:
        assert entry in src, f"DENY list must document {entry}"


# ---------------------------------------------------------------------------
# 5. port registry includes compose ports
# ---------------------------------------------------------------------------
def test_used_host_ports_includes_compose_ports(tmp_path):
    store = DeploymentStore(str(tmp_path))
    store.save({
        "deployment_id": "d1", "project_id": "p1", "host_port": 8001,
        "compose_ports": [9001, 9002], "status": "running",
        "created_at": "2026-10-05T00:00:00Z",
    })
    store.save({
        "deployment_id": "d2", "project_id": "p2",
        "compose_ports": [9003], "status": "failed",
        "created_at": "2026-10-05T00:00:00Z",
    })
    used = store.used_host_ports()
    assert used == {8001, 9001, 9002}
