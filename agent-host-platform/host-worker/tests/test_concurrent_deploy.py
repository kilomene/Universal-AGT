"""W13b §42 — concurrent deployments.

Submits multiple deployments SIMULTANEOUSLY (threads + barrier release)
through the REAL pipeline/dispatcher/client code and verifies:

  * no duplicate port allocation (worker-side port reservation atomicity
    under threads — deployments.pipeline reserve_free_port);
  * no corrupted artifacts (concurrent downloads of the same artifact_id
    stay byte-identical; different artifacts stay separate);
  * no cross-project state leakage (A's env/files/secrets never appear in
    B's containers, state files or logs — exercises the thread-local log
    scrubber);
  * no deployment race (every deploy reaches a correct terminal state);
  * no duplicate task claim (exactly one worker wins per task) and no
    incorrect host assignment — via the REAL ControlPlaneClient over real
    HTTP against a minimal in-process control plane whose claim is
    serialized under a lock, mirroring the real route's
    FOR UPDATE SKIP LOCKED (see the SCOPE NOTE below).

The same-key idempotency race (exactly one deployment) is API-side and is
covered in control-plane/api/test/concurrentDeploy.test.ts.

Determinism: threads are released simultaneously with threading.Barrier;
joins use timeouts and every assertion is on recorded state, never on
timing. No sleeps-as-synchronization.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest
import requests

from agent.api import ControlPlaneClient, WorkerAPIError
from agent.context import WorkerContext
from deployments import pipeline
from deployments.state import DeploymentStore
from docker.client import DockerError
from executor.dispatcher import TaskDispatcher
from logs.store import LogStore

from fake_thread_docker import ThreadDockerClient


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------
@dataclass
class _Config:
    work_dir: str
    apps_dir: str
    host_token: str = "host-test-token-not-real"


class FakeAPI:
    """Chunk-streaming artifact downloads + per-project fetched secrets."""

    def __init__(self, artifacts=None, secrets_by_project=None):
        self.artifacts = dict(artifacts or {})
        self.secrets_by_project = dict(secrets_by_project or {})
        self.progress_calls = []
        self._lock = threading.Lock()

    def download_artifact(self, artifact_id, dest_path, expected_size=None):
        data = self.artifacts[artifact_id]
        if expected_size is not None and len(data) != expected_size:
            raise WorkerAPIError(
                f"artifact {artifact_id} size mismatch: got {len(data)}, "
                f"expected {expected_size}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        # Small chunks: without the per-artifact download lock two threads
        # would interleave these writes and corrupt each other's file.
        with open(dest_path, "wb") as fh:
            for off in range(0, len(data), 4096):
                fh.write(data[off:off + 4096])
        return dest_path

    def get_project_secrets(self, project_id):
        return dict(self.secrets_by_project.get(project_id, {}))

    def progress(self, task_id, payload):
        with self._lock:
            self.progress_calls.append((task_id, payload))
        return {}


def _make_ctx(tmp_path, docker, api, name="shared"):
    work = tmp_path / f"work-{name}"
    work.mkdir(parents=True, exist_ok=True)
    config = _Config(work_dir=str(work), apps_dir=str(tmp_path / f"apps-{name}"))
    return WorkerContext(
        config=config,
        api=api,
        docker=docker,
        log_store=LogStore(str(tmp_path / f"logs-{name}")),
        deployment_store=DeploymentStore(str(work)),
    )


def _deploy_task(project_id, project_name, version, deployment_id, task_id,
                 image="prebuilt/app:latest", env=None, secrets=None,
                 artifact_id=None, artifact_checksum=None, artifact_size=None):
    manifest = {
        "name": project_name,
        "runtime": "docker",
        "service": {"port": 3000, "healthcheck": "/health"},
        "env": {"APP_NAME": project_name, "APP_VERSION": version,
                **(env or {})},
    }
    payload = {
        "project_id": project_id,
        "project_name": project_name,
        "version": version,
        "deployment_id": deployment_id,
        "manifest": manifest,
        "image": image,
        "healthcheck_timeout": 20,
    }
    if secrets:
        payload["secrets"] = secrets
    if artifact_id:
        payload["artifact_id"] = artifact_id
        payload["artifact_checksum"] = artifact_checksum
        payload["artifact_size"] = artifact_size
    return {"id": task_id, "type": "deploy", "payload": payload}


def _run_threads(n, fn):
    """Release n threads simultaneously; return their results in order.

    fn(i) runs on thread i. An exception on any thread fails the test
    with that thread's traceback.
    """
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = [None] * n

    def _wrap(i):
        try:
            barrier.wait(timeout=30)
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001
            errors[i] = exc

    threads = [threading.Thread(target=_wrap, args=(i,), daemon=True,
                                name=f"race-{i}") for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "worker thread hung"
    for i, err in enumerate(errors):
        if err is not None:
            raise AssertionError(f"thread {i} raised") from err
    return results


def _make_artifact(owner: str, size: int = 200_000) -> bytes:
    """A real tar.gz: manifest + owner marker + padding to widen races."""
    manifest = {"name": "placeholder", "runtime": "docker",
                "service": {"port": 3000, "healthcheck": "/health"}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, data in (
            ("agent.deploy.json", json.dumps(manifest).encode()),
            ("owner.txt", f"owner={owner}\n".encode()),
            ("padding.bin", b"x" * size),
        ):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. No duplicate port allocation under threads
# ---------------------------------------------------------------------------
def test_concurrent_deploys_get_distinct_ports_and_all_serve(tmp_path):
    n = 6
    docker = ThreadDockerClient()
    api = FakeAPI()
    ctx = _make_ctx(tmp_path, docker, api)  # ONE shared ctx, like production
    try:
        projects = [
            (f"1000000{i}-0000-4000-8000-000000000000", f"race-app-{i}")
            for i in range(n)
        ]

        def _one(i):
            project_id, project_name = projects[i]
            dep_id = f"dep-race-{i}-{uuid.uuid4().hex[:8]}"
            task = _deploy_task(project_id, project_name, "1.0.0", dep_id,
                                f"task-race-{i}")
            result = pipeline.deploy(ctx, task)
            assert result["status"] == "running"
            return dep_id

        dep_ids = _run_threads(n, _one)

        states = [ctx.deployment_store.load(d) for d in dep_ids]
        assert all(s is not None and s["status"] == "running" for s in states)
        ports = [s["host_port"] for s in states]
        assert len(set(ports)) == n, f"duplicate host port allocated: {ports}"
        names = [s["container_name"] for s in states]
        assert len(set(names)) == n
        assert set(ports) == ctx.deployment_store.used_host_ports()
        # Every app serves its own identity on its own port.
        for (project_id, project_name), s in zip(projects, states):
            status, body = docker.http_get(s["host_port"], "/")
            assert status == 200
            assert json.loads(body)["app"] == project_name
        # No port reservation leaked: the process-wide reservation set must
        # be empty once every deploy persisted its state row.
        assert pipeline.reserved_port_snapshot() == set()
    finally:
        docker.shutdown_all()


# ---------------------------------------------------------------------------
# 2. Concurrent artifact downloads stay separate and uncorrupted
# ---------------------------------------------------------------------------
def test_concurrent_artifact_downloads_not_corrupted(tmp_path):
    docker = ThreadDockerClient()
    art_a = _make_artifact("proj-A")
    art_b = _make_artifact("proj-B")
    api = FakeAPI(
        artifacts={"art-A": art_a, "art-B": art_b},
        secrets_by_project={},
    )
    ctx = _make_ctx(tmp_path, docker, api)
    try:
        specs = [
            # (project_id, project_name, artifact_id, artifact_bytes)
            ("a0000000-0000-4000-8000-000000000000", "art-proj-a",
             "art-A", art_a),
            ("a0000000-0000-4000-8000-000000000000", "art-proj-a",
             "art-A", art_a),  # same artifact, concurrent redeploy
            ("b0000000-0000-4000-8000-000000000000", "art-proj-b",
             "art-B", art_b),
            ("b0000000-0000-4000-8000-000000000000", "art-proj-b",
             "art-B", art_b),
        ]

        def _one(i):
            project_id, project_name, artifact_id, blob = specs[i]
            dep_id = f"dep-art-{i}-{uuid.uuid4().hex[:8]}"
            task = _deploy_task(
                project_id, project_name, "1.0.0", dep_id, f"task-art-{i}",
                artifact_id=artifact_id,
                artifact_checksum="sha256:" + hashlib.sha256(blob).hexdigest(),
                artifact_size=len(blob),
            )
            result = pipeline.deploy(ctx, task)
            assert result["status"] == "running"
            return dep_id, project_name

        results = _run_threads(4, _one)

        # Both downloads of the same artifact verified (no torn bytes), and
        # each deployment extracted its OWN artifact's owner marker.
        owners = {"art-A": "proj-A", "art-B": "proj-B"}
        for (dep_id, project_name), (pid, _pname, aid, _blob) in zip(
                results, specs):
            state = ctx.deployment_store.load(dep_id)
            assert state is not None
            assert state["status"] in ("running", "superseded")
            owner = Path(ctx.config.work_dir) / "deployments" / dep_id \
                / "source" / "owner.txt"
            assert owner.read_text() == f"owner={owners[aid]}\n", \
                "cross-artifact contamination in extracted sources"
        # The shared .bin files are byte-identical to the originals.
        for artifact_id, blob in (("art-A", art_a), ("art-B", art_b)):
            dest = Path(ctx.config.work_dir) / "artifacts" / f"{artifact_id}.bin"
            assert dest.read_bytes() == blob
    finally:
        docker.shutdown_all()


# ---------------------------------------------------------------------------
# 3. No duplicate task claim; no incorrect host assignment
# ---------------------------------------------------------------------------
# SCOPE NOTE: the real claim route (POST /v1/worker/tasks/claim) cannot run
# under pg-mem — its atomic-claim SQL uses FOR UPDATE SKIP LOCKED, which
# pg-mem's planner does not support. This mini plane mirrors the route's
# claim semantics faithfully (highest-priority oldest queued task whose
# assigned_to is null or the claiming host; the pick-and-mark is
# serialized under a lock, exactly what SKIP LOCKED gives the real
# route), and the test drives it with the REAL ControlPlaneClient over
# real HTTP. What is proven: concurrent claimants cannot double-claim one
# task and cannot be given another host's task, given the atomic claim.
class _MiniPlane:
    def __init__(self):
        self.lock = threading.RLock()
        self.hosts = {}
        self.tasks = {}
        self._ids = 0
        self._server = None
        self._thread = None

    def _new_id(self, prefix):
        with self.lock:
            self._ids += 1
            return f"{prefix}-{self._ids:06d}"

    def add_task(self, task_id, assigned_to=None, priority=0):
        with self.lock:
            self.tasks[task_id] = {
                "id": task_id, "type": "deploy", "status": "queued",
                "assigned_to": assigned_to, "priority": priority,
                "created_at": self._ids, "payload": {},
            }

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        plane = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if parsed.path == "/v1/hosts/register":
                    host_id = str(uuid.uuid4())
                    with plane.lock:
                        plane.hosts[host_id] = {"id": host_id}
                    self._json(201, {"host": {"id": host_id}})
                elif parsed.path == "/v1/worker/tasks/claim":
                    host_id = body.get("host_id")
                    with plane.lock:
                        cands = [t for t in plane.tasks.values()
                                 if t["status"] == "queued"
                                 and (t["assigned_to"] is None
                                      or t["assigned_to"] == host_id)]
                        cands.sort(key=lambda t: (-t["priority"],
                                                 t["created_at"]))
                        if not cands:
                            self.send_response(204)
                            self.end_headers()
                            return
                        task = cands[0]
                        task["status"] = "claimed"
                        task["claimed_by"] = host_id
                    self._json(200, {"task": dict(task)})
                else:
                    self._json(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def shutdown(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None


@pytest.fixture
def plane():
    p = _MiniPlane().start()
    yield p
    p.shutdown()


def _register(plane):
    client = ControlPlaneClient(plane.url, "bootstrap")
    return client.register_host("race-host", ["docker"], "9.9.9-test")["host"]["id"]


def test_concurrent_claim_exactly_one_winner(plane):
    plane.add_task("task-only")
    host_ids = [_register(plane) for _ in range(8)]

    def _one(i):
        client = ControlPlaneClient(plane.url, "tok")
        return client.claim_task(host_ids[i], ["docker"], wait=0)

    claimed = _run_threads(8, _one)
    winners = [t for t in claimed if t is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert winners[0]["id"] == "task-only"
    assert winners[0]["claimed_by"] in host_ids


def test_concurrent_claim_respects_host_assignment(plane):
    host_a = _register(plane)
    host_b = _register(plane)
    host_c = _register(plane)
    plane.add_task("task-for-a", assigned_to=host_a)
    plane.add_task("task-for-b", assigned_to=host_b)

    def _one(i):
        client = ControlPlaneClient(plane.url, "tok")
        host = [host_a, host_b][i]
        return client.claim_task(host, ["docker"], wait=0)

    got_a, got_b = _run_threads(2, _one)
    assert got_a is not None and got_a["id"] == "task-for-a", \
        "host A must win exactly its own assigned task"
    assert got_b is not None and got_b["id"] == "task-for-b", \
        "host B must win exactly its own assigned task"

    # A third host gets nothing: no incorrect host assignment, no double
    # claim of an already-claimed task.
    client = ControlPlaneClient(plane.url, "tok")
    assert client.claim_task(host_c, ["docker"], wait=0) is None


# ---------------------------------------------------------------------------
# 4. No cross-project state leakage (env, files, secret values in logs)
# ---------------------------------------------------------------------------
class _FailDocker(ThreadDockerClient):
    """docker run always fails, embedding the env argv in the DockerError
    message — exactly like the real client (`-e KEY=VALUE` in argv). Both
    dispatches rendezvous here so the failure logging truly overlaps."""

    def __init__(self, barrier):
        super().__init__()
        self._barrier = barrier

    def run(self, name, image, ports=None, env=None, **kwargs):
        self._barrier.wait(timeout=30)
        env = {str(k): str(v) for k, v in (env or {}).items()}
        argv = ["docker", "run", "-d", "--name", name]
        for key in sorted(env):
            argv += ["-e", f"{key}={env[key]}"]
        argv.append(image)
        raise DockerError(argv, 125, "simulated run failure")


def test_concurrent_dispatch_no_cross_project_leakage(tmp_path):
    barrier = threading.Barrier(2)
    fail_docker = _FailDocker(barrier)

    secrets = {
        "a0000000-0000-4000-8000-000000000000": {
            "FETCHED_A": "fetched-secret-A-9f8e7d6c5b"},
        "b0000000-0000-4000-8000-000000000000": {
            "FETCHED_B": "fetched-secret-B-1a2b3c4d5e"},
    }
    api = FakeAPI(secrets_by_project=secrets)
    ctx = _make_ctx(tmp_path, fail_docker, api)  # ONE shared ctx
    dispatcher = TaskDispatcher(ctx, api)        # ONE shared dispatcher
    try:
        specs = [
            ("a0000000-0000-4000-8000-000000000000", "leak-app-a",
             {"PAYLOAD_A": "payload-secret-A-11223344"}),
            ("b0000000-0000-4000-8000-000000000000", "leak-app-b",
             {"PAYLOAD_B": "payload-secret-B-55667788"}),
        ]
        all_values = ["payload-secret-A-11223344", "fetched-secret-A-9f8e7d6c5b",
                      "payload-secret-B-55667788", "fetched-secret-B-1a2b3c4d5e"]

        def _one(i):
            project_id, project_name, payload_secrets = specs[i]
            dep_id = f"dep-leak-{i}-{uuid.uuid4().hex[:8]}"
            task_id = f"task-leak-{i}"
            task = _deploy_task(
                project_id, project_name, "1.0.0", dep_id, task_id,
                env={"APP_NAME": project_name},
                secrets=payload_secrets,
            )
            # The REAL dispatcher path (shared ctx, thread pool in prod).
            return dispatcher.dispatch(task), dep_id, task_id

        outcomes = _run_threads(2, _one)
        assert all(o[0]["status"] == "failed" for o in outcomes)

        for i, (project_id, project_name, _ps) in enumerate(specs):
            _outcome, dep_id, task_id = outcomes[i]
            task_log = Path(ctx.log_store.task_log_path(task_id)).read_text()
            dep_log = Path(
                ctx.log_store.deployment_log_path(dep_id)).read_text()
            # Every secret value of BOTH projects must be redacted in A's
            # logs and vice versa — thread-local scrubbers, no clobbering.
            # (The deployment log legitimately carries no secrets at all:
            # the failing docker argv is only ever logged by the
            # dispatcher's raise path, into the task log.)
            for value in all_values:
                assert value not in task_log, \
                    f"secret value leaked into {task_id} task log"
                assert value not in dep_log, \
                    f"secret value leaked into {task_id} deployment log"
            assert "***" in task_log, \
                f"expected redacted secrets in {task_id} task log"

        # The progress error shipped to the control plane is redacted too.
        for task_id, payload in api.progress_calls:
            if payload.get("status") == "failed":
                for value in all_values:
                    assert value not in json.dumps(payload), \
                        f"secret value leaked into progress error for {task_id}"
    finally:
        fail_docker.shutdown_all()


# ---------------------------------------------------------------------------
# 5. Every deploy in a mixed batch reaches a correct terminal state
# ---------------------------------------------------------------------------
def test_mixed_batch_all_reach_terminal_state(tmp_path):
    docker = ThreadDockerClient()
    api = FakeAPI()
    ctx = _make_ctx(tmp_path, docker, api)
    try:
        def _good(i):
            dep_id = f"dep-mix-{i}-{uuid.uuid4().hex[:8]}"
            task = _deploy_task(
                f"c000000{i}-0000-4000-8000-000000000000", f"mix-app-{i}",
                "1.0.0", dep_id, f"task-mix-{i}")
            return ("ok", dep_id, pipeline.deploy(ctx, task))

        def _bad_manifest(i):
            dep_id = f"dep-mix-bad-{uuid.uuid4().hex[:8]}"
            task = _deploy_task(
                "d0000000-0000-4000-8000-000000000000", "mix-app-bad",
                "1.0.0", dep_id, "task-mix-bad")
            task["payload"]["manifest"] = {"runtime": "docker"}  # no name
            try:
                pipeline.deploy(ctx, task)
            except pipeline.DeployError:
                return ("rejected", dep_id, None)
            raise AssertionError("bad manifest deploy should have raised")

        def _bad_health(i):
            dep_id = f"dep-mix-sick-{uuid.uuid4().hex[:8]}"
            task = _deploy_task(
                "e0000000-0000-4000-8000-000000000000", "mix-app-sick",
                "1.0.0", dep_id, "task-mix-sick",
                env={"HEALTH_FAIL": "1"})
            task["payload"]["healthcheck_timeout"] = 3
            try:
                pipeline.deploy(ctx, task)
            except pipeline.DeployError:
                return ("failed", dep_id, None)
            raise AssertionError("unhealthy deploy should have raised")

        fns = [_good] * 4 + [_bad_manifest, _bad_health]

        def _one(i):
            return fns[i](i)

        results = _run_threads(6, _one)

        ok = [r for r in results if r[0] == "ok"]
        rejected = [r for r in results if r[0] == "rejected"]
        failed = [r for r in results if r[0] == "failed"]
        assert len(ok) == 4 and len(rejected) == 1 and len(failed) == 1

        for _kind, dep_id, result in ok:
            assert result["status"] == "running"
            state = ctx.deployment_store.load(dep_id)
            assert state["status"] == "running"
            status, body = docker.http_get(state["host_port"], "/")
            assert status == 200

        # Manifest validation fails before any state is persisted: nothing
        # half-written, no port held.
        for _kind, dep_id, _none in rejected:
            assert ctx.deployment_store.load(dep_id) is None

        # Healthcheck failure persists an honest terminal state and frees
        # the port.
        for _kind, dep_id, _none in failed:
            state = ctx.deployment_store.load(dep_id)
            assert state is not None, "failed deploy must record state"
            assert state["status"] in ("failed", "rolled_back")
            assert state["host_port"] is None, \
                "failed deploy must not hold a port"

        # Port registry view is coherent: exactly the 4 live ports.
        live_ports = {ctx.deployment_store.load(d)["host_port"]
                      for _k, d, _r in ok}
        assert ctx.deployment_store.used_host_ports() == live_ports
        assert pipeline.reserved_port_snapshot() == set()
    finally:
        docker.shutdown_all()
