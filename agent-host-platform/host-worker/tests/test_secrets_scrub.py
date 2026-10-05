"""W11: API-fetched project secrets must be scrubbed from every log line.

The dispatcher's SecretScrubber only knows payload-carried secrets and the
host token. pipeline.deploy() additionally pulls secrets over its
authenticated channel (ctx.api.get_project_secrets); those values are
registered with the active scrubber by deploy() itself. Without that, a
failing `docker run` — DockerError embeds the full `-e KEY=value` argv —
would land in the task log, progress errors and task.failed events in
plaintext.

Fakes are dependency-injected doubles (never stubs of the real modules).
All secret values are obvious placeholders (test-key-not-real): nothing
here may trip scripts/secrets-sweep.sh.
"""
import json
from pathlib import Path

import pytest

from deployments import pipeline
from deployments.state import DeploymentStore
from docker.client import DockerError
from executor.dispatcher import SecretScrubber
from logs.store import LogStore

FETCHED_API_KEY = "fetched-test-key-not-real"
FETCHED_DB_PASS = "fetched-db-test-key-not-real"
PAYLOAD_KEY = "payload-test-key-not-real"


class RecordingDockerClient:
    """Fake docker client: records run() env, then behaves as told."""

    def __init__(self, fail_run=False):
        self.fail_run = fail_run
        self.run_envs = []  # env dicts handed to run()
        self.run_calls = []
        self.containers = {}

    def _record(self, name, *args):
        pass

    def version(self):
        return "99.0-fake"

    def compose_available(self):
        return True

    def run(self, name, image, ports=None, env=None, memory=None,
            cpus=None, restart="unless-stopped", timeout=120):
        self.run_calls.append(name)
        self.run_envs.append(dict(env or {}))
        argv = ["docker", "run", "-d", "--name", name]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        argv.append(image)
        if self.fail_run:
            # Mirrors the real client: the exception message embeds the full
            # argv, secret values included.
            raise DockerError(argv, 125, "fake daemon refused the run")
        self.containers[name] = {"image": image, "running": True}
        return "fake-container-id-" + name

    def container_exists(self, name):
        return name in self.containers

    def stop(self, name, timeout_secs=10, timeout=120):
        self.containers.get(name, {})["running"] = False

    def rm(self, name, force=False, timeout=120):
        self.containers.pop(name, None)

    def logs(self, name, tail=500):
        return "fake logs\n"

    def ps(self, all=False):
        return []

    def inspect(self, name):
        c = self.containers.get(name, {})
        return [{"Config": {"Image": c.get("image")},
                 "State": {"Status": "running" if c.get("running") else "exited"},
                 "NetworkSettings": {"Ports": {}}}]

    def container_status(self, name):
        c = self.containers.get(name)
        return "running" if c and c.get("running") else "exited"

    def remove_image(self, image, timeout=120):
        return None

    def image_exists(self, tag):
        return False

    def compose_down(self, compose_file, project_name=None, timeout=300):
        return None

class SecretsAPI:
    """Serves project secrets like GET /v1/worker/projects/:id/secrets."""

    def __init__(self, secrets):
        self.secrets = dict(secrets)
        self.fetches = []

    def get_project_secrets(self, project_id):
        self.fetches.append(project_id)
        return dict(self.secrets)


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
        self.scrub = lambda s: s  # replaced per-task by the dispatcher

    def log(self, task_id, line):
        clean = self.scrub(line)
        self.log_store.append_task(task_id, clean)
        return clean

    def require_docker(self):
        return self.docker


def _deploy_task(**overrides):
    payload = {
        "project_id": "proj-1",
        "project_name": "my-api",
        "version": "2.0.0",
        "deployment_id": "dep-scrub",
        "image": "img:2.0.0",  # prebuilt: no artifact download, no build
        "manifest": {
            "name": "my-api",
            "runtime": "docker",
            "service": {"port": 3000, "healthcheck": "/health"},
            "resources": {"memory": "256m", "cpu": 1},
            "restart": "unless-stopped",
            "env": {"NODE_ENV": "production"},
        },
        "healthcheck_timeout": 3,
    }
    payload.update(overrides)
    return {"id": "task-1", "type": "deploy", "payload": payload}


def _task_log_text(ctx, task_id="task-1"):
    return Path(ctx.log_store.task_log_path(task_id)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fetched secrets are registered with the scrubber at deploy time
# ---------------------------------------------------------------------------
def test_fetched_secret_scrubbed_from_docker_error(tmp_path):
    docker = RecordingDockerClient(fail_run=True)
    api = SecretsAPI({"API_KEY": FETCHED_API_KEY, "DB_PASS": FETCHED_DB_PASS})
    ctx = FakeCtx(tmp_path, docker, api)

    with pytest.raises(DockerError):
        pipeline.deploy(ctx, _deploy_task())

    # What the dispatcher does on handler failure: log the traceback tail
    # and ship it as the progress error. Both must be redacted.
    logged = ctx.log("task-1", f"task failed:\ndocker run -d --name x -e API_KEY={FETCHED_API_KEY} exited 125")
    assert FETCHED_API_KEY not in logged
    assert "***" in logged

    body = _task_log_text(ctx)
    assert FETCHED_API_KEY not in body
    assert FETCHED_DB_PASS not in body
    # the fetch itself was logged (count only, never values)
    assert "project secret(s)" in body


def test_payload_and_fetched_secrets_both_scrubbed(tmp_path):
    docker = RecordingDockerClient(fail_run=True)
    api = SecretsAPI({"API_KEY": FETCHED_API_KEY})
    ctx = FakeCtx(tmp_path, docker, api)
    # Simulate the dispatcher: its scrubber already covers payload secrets
    # and the host token before the handler runs.
    ctx.scrub = SecretScrubber([PAYLOAD_KEY, "host-test-token-not-real"]).scrub

    task = _deploy_task(secrets={"PAYLOAD_KEY": PAYLOAD_KEY})
    with pytest.raises(DockerError):
        pipeline.deploy(ctx, task)

    probe = (
        f"docker run -e PAYLOAD_KEY={PAYLOAD_KEY} "
        f"-e API_KEY={FETCHED_API_KEY} exited 125"
    )
    cleaned = ctx.scrub(probe)
    assert PAYLOAD_KEY not in cleaned
    assert FETCHED_API_KEY not in cleaned
    assert cleaned.count("***") == 2


def test_state_and_result_never_carry_secret_values(tmp_path, monkeypatch):
    docker = RecordingDockerClient()
    api = SecretsAPI({"API_KEY": FETCHED_API_KEY, "DB_PASS": FETCHED_DB_PASS})
    ctx = FakeCtx(tmp_path, docker, api)
    monkeypatch.setattr(pipeline.health_checker, "wait_for_healthcheck",
                        lambda *a, **k: True)

    result = pipeline.deploy(ctx, _deploy_task())

    # Injection worked: the container env really got the values...
    assert docker.run_envs, "docker run was never called"
    injected = docker.run_envs[0]
    assert injected["API_KEY"] == FETCHED_API_KEY
    assert injected["DB_PASS"] == FETCHED_DB_PASS

    # ...but neither the persisted deployment state nor the task result
    # (which the control plane stores) carries them.
    state = ctx.deployment_store.load("dep-scrub")
    assert state is not None
    assert FETCHED_API_KEY not in json.dumps(state)
    assert FETCHED_DB_PASS not in json.dumps(state)
    assert "API_KEY" not in str(state.get("env"))
    assert FETCHED_API_KEY not in json.dumps(result)
    assert FETCHED_DB_PASS not in json.dumps(result)


# ---------------------------------------------------------------------------
# Scrubber shape coverage (executor.dispatcher.SecretScrubber)
# ---------------------------------------------------------------------------
def test_scrubber_covers_secret_shapes():
    scrubber = SecretScrubber(["host-test-token-not-real", FETCHED_API_KEY,
                               "overlap-test-key-not-real",
                               "overlap-test-key-not-real-longer"])
    # argv form (DockerError message)
    line = "docker run -d --name x -e API_KEY=" + FETCHED_API_KEY + " img"
    assert FETCHED_API_KEY not in scrubber.scrub(line)
    # host token form
    assert "host-test-token-not-real" not in scrubber.scrub(
        "Authorization: Bearer host-test-token-not-real")
    # overlapping values: longest first, no partial leftovers
    both = "a overlap-test-key-not-real-longer b"
    cleaned = scrubber.scrub(both)
    assert "overlap-test-key-not-real" not in cleaned
    assert cleaned == "a *** b"
    # non-string input is coerced, not crashed on
    assert scrubber.scrub(None) == "None"
    assert scrubber.scrub(123) == "123"


def test_scrubber_ignores_tiny_values():
    # Values shorter than 4 chars are deliberately NOT redacted: replacing
    # 1-3 char strings would mangle ordinary log text with false positives.
    scrubber = SecretScrubber(["abc", "x"])
    assert scrubber.scrub("say abc now") == "say abc now"
