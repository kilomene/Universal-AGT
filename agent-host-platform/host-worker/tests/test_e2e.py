"""Phase 9 end-to-end tests: REAL worker code paths against a fake control
plane (real HTTP) and a subprocess-backed fake docker client.

What is REAL here (not faked):
  * agent.api.ControlPlaneClient — the worker's actual outbound-only HTTP
    client (register, heartbeat, claim with ?wait=, progress, artifact
    download, project secrets). Every call below goes over real HTTP to the
    fake plane.
  * executor.dispatcher.TaskDispatcher — the full claim -> run -> report
    lifecycle, log-chunk streaming, secret scrubbing.
  * agent.policy — the 18-type allowlist + TaskRejected handling.
  * executor.handlers.handle_deploy -> deployments.pipeline.deploy —
    artifact download, SHA-256 verification, tar extraction, agent.deploy.json
    manifest validation, resource checks, port allocation + OS/docker
    collision verification, container lifecycle, rollback, GC.
  * health.checker.wait_for_healthcheck — real HTTP polls against the
    "container".
  * deployments.state.DeploymentStore and logs.store.LogStore — real disk
    state.

What is FAKED (and why — no Docker daemon, no Postgres in this sandbox):
  * The control plane: an in-memory HTTP server implementing exactly the
    worker endpoints the client calls. Its task state machine mirrors
    control-plane/api/src/lib/stateMachine.ts, its retry classification
    mirrors lib/retryPolicy.ts, its event mirroring (task.claimed ->
    task.started -> deployment.started -> task.completed ->
    deployment.completed) mirrors routes/worker.ts, and its deployment
    creation mirrors routes/deployments.ts — including the Phase 9 fix that
    injects artifact_checksum/artifact_size into the deploy task payload.
    The claim is serialized under a lock (the real route's FOR UPDATE SKIP
    LOCKED cannot run under pg-mem, so the atomic claim is covered here).
  * Docker: SubprocessDockerClient implements the docker.client.DockerClient
    interface. With no daemon available, `run()` launches the artifact's own
    server.py as an OS subprocess with PORT=<mapped host port> injected —
    the fake collapses the host_port->container_port mapping the way
    `docker -p` would. The app code serving traffic is the REAL demo-app
    code extracted from the REAL artifact tarball. Network-namespace
    isolation is NOT simulated (documented limitation).

Determinism: no sleeps-as-synchronization on the worker side (claim
long-polls, health checks poll); ports are OS-assigned free ports; every
scenario cleans up its subprocesses via an autouse fixture.

Scenario -> acceptance criterion:
  (a) full chain ......... criteria 11,12,13,15,17,18,20,21
  (b) agent disappearance  criterion 24
  (c) manual approve/reject criterion 14
  (d) broken app .......... criterion 18 (+ previous deployment survives)
  (e) health-fail rollback  criterion 19 (worker-side auto-rollback)
  (f) multi-app ............ criterion 16
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from agent.api import ControlPlaneClient
from agent.context import WorkerContext
from deployments.state import DeploymentStore
from executor.dispatcher import TaskDispatcher
from logs.store import LogStore

DEMO_APP_SRC = Path(__file__).resolve().parent.parent.parent / "examples" / "demo-app"

# ---------------------------------------------------------------------------
# Part 1: FakeControlPlane — in-memory control plane over real HTTP.
#
# Mirrors (faithfully, including status codes and event names):
#   * lib/stateMachine.ts            (task transitions)
#   * lib/retryPolicy.ts             (safe/conditional/never classification)
#   * routes/worker.ts               (claim, progress, deployment mirroring,
#                                     artifact scoping, secrets scoping)
#   * routes/deployments.ts          (deployment creation incl. the Phase 9
#                                     artifact_checksum/artifact_size injection)
#   * routes/tasks.ts                (approve/reject semantics)
# ---------------------------------------------------------------------------

# Mirror of control-plane/api/src/lib/stateMachine.ts TRANSITIONS.
_TASK_TRANSITIONS = {
    "queued": {"claimed", "cancelled"},
    "claimed": {"running", "failed", "retrying", "cancelled"},
    "running": {"completed", "failed", "retrying", "awaiting_approval", "cancelled"},
    "awaiting_approval": {"queued", "cancelled"},
    "retrying": {"queued"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}
_TERMINAL = {"completed", "failed", "cancelled"}
_PROGRESS_STATUSES = {"claimed", "running", "awaiting_approval", "completed", "failed"}

# Mirror of control-plane/api/src/lib/retryPolicy.ts.
_SAFE_TYPES = {"logs", "status", "healthcheck", "system-info",
               "artifact-download", "ingress-sync"}
_NEVER_TYPES = {"remove", "rollback"}


def _retry_outcome(task_type, failed_from, attempts, max_attempts):
    if attempts >= max_attempts:
        return "fail"
    if task_type in _SAFE_TYPES:
        return "retry"
    if task_type in _NEVER_TYPES:
        return "fail"
    return "retry" if failed_from == "claimed" else "fail"


class FakeControlPlane:
    """In-memory control plane. Thread-safe; serves real HTTP."""

    def __init__(self):
        self.lock = threading.RLock()
        self.hosts = {}        # id -> dict(id, name, token, capabilities, worker_version, status, last_seen)
        self.tasks = {}        # id -> dict
        self.deployments = {}  # id -> dict
        self.artifacts = {}    # id -> dict(id, project_id, filename, size, checksum, status, bytes)
        self.projects = {}     # id -> dict(id, name, secrets)
        self.events = []       # list of dicts, append-only, in order
        self.task_logs = {}    # task_id -> [log chunks]
        self._ids = 0
        self._server = None
        self._thread = None

    # -- identity ------------------------------------------------------
    def _new_id(self, prefix):
        with self.lock:
            self._ids += 1
            return f"{prefix}-{self._ids:06d}"

    def _event(self, type, actor_type=None, actor_id=None, task_id=None,
               deployment_id=None, host_id=None, payload=None):
        with self.lock:
            self.events.append({
                "type": type, "actor_type": actor_type, "actor_id": actor_id,
                "task_id": task_id, "deployment_id": deployment_id,
                "host_id": host_id, "payload": payload or {},
                "seq": len(self.events),
            })

    # -- serving --------------------------------------------------------
    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        handler = _PlaneHandler
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._server.plane = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="fake-control-plane", daemon=True)
        self._thread.start()
        return self

    def shutdown(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    def event_types(self):
        with self.lock:
            return [e["type"] for e in self.events]


class _PlaneHandler(BaseHTTPRequestHandler):
    server_version = "FakeControlPlane/1.0"

    def log_message(self, *args):  # keep test output clean
        pass

    # -- helpers ---------------------------------------------------------
    @property
    def plane(self) -> FakeControlPlane:
        return self.server.plane

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _bearer(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else None

    def _host_for(self, token):
        if not token:
            return None
        with self.plane.lock:
            for h in self.plane.hosts.values():
                if h["token"] == token:
                    return h
        return None

    def _public_task(self, task):
        return {k: v for k, v in task.items() if k != "_log"}

    # -- routing ----------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/v1/hosts/register":
                return self._register_host()
            if path.startswith("/v1/hosts/") and path.endswith("/heartbeat"):
                return self._heartbeat(path.split("/")[3])
            if path == "/v1/worker/tasks/claim":
                return self._claim(parsed.query)
            if path.startswith("/v1/worker/tasks/") and path.endswith("/progress"):
                return self._progress(path.split("/")[4])
            if path == "/v1/test/agent/artifacts/init":
                return self._artifact_init()
            if path.startswith("/v1/test/agent/artifacts/") and path.endswith("/ready"):
                return self._artifact_ready(path.split("/")[5])
            if path == "/v1/test/agent/projects":
                return self._project_create()
            if path.startswith("/v1/test/agent/projects/") and path.endswith("/secrets"):
                return self._project_secrets(path.split("/")[5])
            if path == "/v1/test/agent/deployments":
                return self._deployment_create()
            if path == "/v1/test/agent/tasks":
                return self._task_create()
            if path.startswith("/v1/test/agent/tasks/") and path.endswith("/approve"):
                return self._approve(path.split("/")[5], True)
            if path.startswith("/v1/test/agent/tasks/") and path.endswith("/reject"):
                return self._approve(path.split("/")[5], False)
            self._send_json(404, {"error": {"code": "not_found", "message": path}})
        except _HttpError as exc:
            self._send_json(exc.code, {"error": {"code": exc.errcode, "message": exc.message}})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/v1/test/agent/artifacts/") and path.endswith("/content"):
                return self._artifact_content(path.split("/")[5])
            self._send_json(404, {"error": {"code": "not_found", "message": path}})
        except _HttpError as exc:
            self._send_json(exc.code, {"error": {"code": exc.errcode, "message": exc.message}})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/v1/artifacts/") and path.endswith("/download"):
                return self._artifact_download(path.split("/")[3])
            if path.startswith("/v1/worker/projects/") and path.endswith("/secrets"):
                return self._worker_secrets(path.split("/")[4])
            if path.startswith("/v1/test/agent/tasks/"):
                return self._task_get(path.split("/")[5])
            if path.startswith("/v1/test/agent/deployments/"):
                return self._deployment_get(path.split("/")[5])
            self._send_json(404, {"error": {"code": "not_found", "message": path}})
        except _HttpError as exc:
            self._send_json(exc.code, {"error": {"code": exc.errcode, "message": exc.message}})

    # -- worker endpoints ---------------------------------------------------
    def _register_host(self):
        body = self._read_json()
        plane = self.plane
        host_id = plane._new_id("host")
        token = f"uagh-e2e-{host_id}"
        host = {"id": host_id, "name": body.get("name", host_id),
                "host_type": body.get("host_type", "persistent-linux-host"),
                "capabilities": body.get("capabilities", []),
                "worker_version": body.get("worker_version"),
                "token": token, "status": "online", "last_seen": time.time()}
        with plane.lock:
            plane.hosts[host_id] = host
        plane._event("host.registered", actor_type="host", actor_id=host["name"],
                     host_id=host_id)
        public = {k: v for k, v in host.items() if k != "token"}
        self._send_json(201, {"host": public, "host_token": token})

    def _heartbeat(self, host_id):
        host = self._host_for(self._bearer())
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        if host["id"] != host_id:
            raise _HttpError(403, "forbidden", "host token does not belong to this host")
        plane = self.plane
        with plane.lock:
            host["status"] = "online"
            host["last_seen"] = time.time()
            pending = sum(1 for t in plane.tasks.values()
                          if t["status"] == "queued"
                          and (t.get("assigned_to") in (None, host_id)))
        public = {k: v for k, v in host.items() if k != "token"}
        self._send_json(200, {"host": public, "pending_tasks": pending})

    def _claim(self, query):
        host = self._host_for(self._bearer())
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        body = self._read_json()
        if body.get("host_id") != host["id"]:
            raise _HttpError(403, "forbidden", "host token does not match host_id")
        wait = urllib.parse.parse_qs(query).get("wait", ["0"])[0]
        try:
            wait_s = max(0, min(int(wait), 30))
        except ValueError:
            wait_s = 0
        plane = self.plane
        deadline = time.monotonic() + wait_s
        while True:
            with plane.lock:
                candidates = [t for t in plane.tasks.values()
                              if t["status"] == "queued"
                              and t.get("assigned_to") in (None, host["id"])]
                candidates.sort(key=lambda t: (-t.get("priority", 0), t["created_at"]))
                task = candidates[0] if candidates else None
                if task is not None:
                    # Atomic claim (serialized under the plane lock — the real
                    # route's FOR UPDATE SKIP LOCKED cannot run under pg-mem).
                    task["status"] = "claimed"
                    task["claimed_by"] = host["id"]
                    task["assigned_to"] = host["id"]
                    task["attempts"] = task.get("attempts", 0) + 1
                    task["started_at"] = task.get("started_at") or time.time()
            if task is not None:
                plane._event("task.claimed", actor_type="host",
                             actor_id=host["name"], task_id=task["id"],
                             host_id=host["id"],
                             payload={"type": task["type"],
                                      "attempts": task["attempts"]})
                self._send_json(200, {"task": self._public_task(task)})
                return
            if time.monotonic() >= deadline:
                self.send_response(204)
                self.end_headers()
                return
            time.sleep(0.1)

    def _progress(self, task_id):
        host = self._host_for(self._bearer())
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        plane = self.plane
        body = self._read_json()
        status = body.get("status")
        if status not in _PROGRESS_STATUSES:
            raise _HttpError(400, "bad_request",
                             f"status must be one of: {sorted(_PROGRESS_STATUSES)}")
        with plane.lock:
            task = plane.tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "not_found", "task not found")
            if task.get("claimed_by") != host["id"]:
                raise _HttpError(403, "forbidden",
                                 "only the claiming host may report progress on this task")
            final = status
            if status == "failed":
                # Mirror lib/retryPolicy.ts via routes/worker.ts.
                outcome = _retry_outcome(task["type"], task["status"],
                                         task.get("attempts", 0),
                                         task.get("max_attempts", 3))
                if outcome == "retry":
                    final = "retrying"
            if final not in _TASK_TRANSITIONS.get(task["status"], set()):
                raise _HttpError(409, "conflict",
                                 f"cannot transition task from {task['status']} to {final}")
            if body.get("log_chunk"):
                plane.task_logs.setdefault(task_id, []).append(body["log_chunk"])
            task["status"] = final
            if body.get("result") is not None:
                task["result"] = body["result"]
            if body.get("error"):
                task["error"] = body["error"]
            if final in _TERMINAL:
                task["completed_at"] = time.time()
            payload = task.get("payload") or {}
            deployment_id = payload.get("deployment_id")
            deployment = plane.deployments.get(deployment_id) if deployment_id else None

        # Events + deployment mirroring (mirror of mirrorDeploymentState).
        if final == "running":
            plane._event("task.started", actor_type="host", actor_id=host["name"],
                         task_id=task_id, host_id=host["id"])
            if deployment is not None and task["type"] == "deploy" \
                    and deployment["status"] in ("requested", "approved"):
                with plane.lock:
                    deployment["status"] = "building"
                plane._event("deployment.started", actor_type="host",
                             actor_id=host["name"], task_id=task_id,
                             deployment_id=deployment_id, host_id=host["id"],
                             payload={"version": deployment["version"]})
        elif final == "completed":
            plane._event("task.completed", actor_type="host", actor_id=host["name"],
                         task_id=task_id, host_id=host["id"])
            if deployment is not None and task["type"] == "deploy":
                ports = (body.get("result") or {}).get("ports")
                with plane.lock:
                    deployment["status"] = "running"
                    deployment["health_status"] = "healthy"
                    if isinstance(ports, dict):
                        deployment["ports"] = ports
                plane._event("deployment.completed", actor_type="host",
                             actor_id=host["name"], task_id=task_id,
                             deployment_id=deployment_id, host_id=host["id"],
                             payload={"version": deployment["version"]})
        elif final == "failed":
            plane._event("task.failed", actor_type="host", actor_id=host["name"],
                         task_id=task_id, host_id=host["id"],
                         payload={"error": body.get("error")})
            if deployment is not None and task["type"] == "deploy":
                with plane.lock:
                    deployment["status"] = "failed"
                    deployment["health_status"] = "unhealthy"
                plane._event("deployment.failed", actor_type="host",
                             actor_id=host["name"], task_id=task_id,
                             deployment_id=deployment_id, host_id=host["id"],
                             payload={"version": deployment["version"]})
        elif final == "retrying":
            plane._event("task.retrying", actor_type="host", actor_id=host["name"],
                         task_id=task_id, host_id=host["id"],
                         payload={"error": body.get("error")})
        self._send_json(200, {"task": self._public_task(task)})

    def _artifact_download(self, artifact_id):
        host = self._host_for(self._bearer())
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        plane = self.plane
        with plane.lock:
            artifact = plane.artifacts.get(artifact_id)
            if artifact is None or artifact["status"] != "ready":
                raise _HttpError(404, "not_found", "artifact not found")
            # Scoping (Phase 6 fix): only artifacts referenced by tasks this
            # host claimed.
            allowed = any(
                t.get("claimed_by") == host["id"]
                and (t.get("payload") or {}).get("artifact_id") == artifact_id
                for t in plane.tasks.values()
            )
            if not allowed:
                raise _HttpError(403, "forbidden",
                                 "artifact not referenced by this host's tasks")
            data = artifact["bytes"]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _worker_secrets(self, project_id):
        host = self._host_for(self._bearer())
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        plane = self.plane
        with plane.lock:
            project = plane.projects.get(project_id)
            if project is None:
                raise _HttpError(404, "not_found", "project not found")
            live_task = any(
                t.get("claimed_by") == host["id"]
                and (t.get("payload") or {}).get("project_id") == project_id
                and t["status"] in ("claimed", "running", "awaiting_approval")
                for t in plane.tasks.values()
            )
            live_dep = any(
                d["host_id"] == host["id"] and d["project_id"] == project_id
                and d["status"] in ("requested", "approved", "building",
                                    "starting", "healthcheck", "running")
                for d in plane.deployments.values()
            )
            if not (live_task or live_dep):
                raise _HttpError(403, "forbidden",
                                 "no active task or deployment for this project on this host")
            secrets = dict(project.get("secrets", {}))
        self._send_json(200, {"secrets": secrets})

    # -- test-only agent endpoints (mirror routes/deployments.ts + tasks.ts)
    def _artifact_init(self):
        body = self._read_json()
        plane = self.plane
        project_id = body.get("project_id")
        with plane.lock:
            if project_id not in plane.projects:
                raise _HttpError(404, "not_found", "project not found")
            artifact_id = plane._new_id("art")
            plane.artifacts[artifact_id] = {
                "id": artifact_id, "project_id": project_id,
                "filename": body.get("filename", "app.tar.gz"),
                "size": body.get("size", 0), "checksum": None,
                "status": "pending", "bytes": b"",
            }
        self._send_json(201, {"artifact": _public_artifact(plane.artifacts[artifact_id])})

    def _artifact_content(self, artifact_id):
        plane = self.plane
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        with plane.lock:
            artifact = plane.artifacts.get(artifact_id)
            if artifact is None:
                raise _HttpError(404, "not_found", "artifact not found")
            artifact["bytes"] = data
            artifact["size"] = len(data)
        self._send_json(200, {"artifact": _public_artifact(artifact)})

    def _artifact_ready(self, artifact_id):
        plane = self.plane
        with plane.lock:
            artifact = plane.artifacts.get(artifact_id)
            if artifact is None:
                raise _HttpError(404, "not_found", "artifact not found")
            # The agent declares the checksum at init; the server verifies it
            # after the bytes land (mirror of routes/artifacts.ts).
            artifact["checksum"] = "sha256:" + hashlib.sha256(artifact["bytes"]).hexdigest()
            artifact["status"] = "ready"
        self._send_json(200, {"artifact": _public_artifact(artifact)})

    def _project_create(self):
        body = self._read_json()
        plane = self.plane
        project_id = plane._new_id("proj")
        with plane.lock:
            plane.projects[project_id] = {"id": project_id,
                                          "name": body.get("name", project_id),
                                          "secrets": {}}
        self._send_json(201, {"project": {"id": project_id,
                                          "name": plane.projects[project_id]["name"]}})

    def _project_secrets(self, project_id):
        body = self._read_json()
        plane = self.plane
        with plane.lock:
            project = plane.projects.get(project_id)
            if project is None:
                raise _HttpError(404, "not_found", "project not found")
            secrets = body.get("secrets") or {}
            project["secrets"].update({k: str(v) for k, v in secrets.items()})
        self._send_json(200, {"ok": True})

    def _deployment_create(self):
        # Faithful mirror of POST /v1/deployments (routes/deployments.ts),
        # including the Phase 9 artifact_checksum/artifact_size injection.
        body = self._read_json()
        plane = self.plane
        project_id = body.get("project_id")
        host_id = body.get("host_id")
        version = body.get("version")
        artifact_id = body.get("artifact_id")
        mode = body.get("mode", "automatic")
        if mode not in ("automatic", "manual"):
            raise _HttpError(400, "bad_request", "mode must be 'automatic' or 'manual'")
        if not version:
            raise _HttpError(400, "bad_request", "version is required")
        with plane.lock:
            if project_id not in plane.projects:
                raise _HttpError(404, "not_found", "project not found")
            if host_id is not None and host_id not in plane.hosts:
                raise _HttpError(404, "not_found", "host not found")
            artifact_checksum = None
            artifact_size = None
            if artifact_id:
                artifact = plane.artifacts.get(artifact_id)
                if artifact is None:
                    raise _HttpError(404, "not_found", "artifact not found")
                if artifact["project_id"] != project_id:
                    raise _HttpError(422, "unprocessable",
                                     "artifact does not belong to this project")
                if artifact["status"] != "ready":
                    raise _HttpError(422, "unprocessable",
                                     f"artifact is {artifact['status']}; only ready artifacts can be deployed")
                if not artifact["checksum"]:
                    raise _HttpError(422, "unprocessable",
                                     "artifact has no verified checksum")
                artifact_checksum = artifact["checksum"]
                artifact_size = artifact["size"]
            deployment_id = plane._new_id("dep")
            task_id = plane._new_id("task")
            task_status = "awaiting_approval" if mode == "manual" else "queued"
            payload = {"project_id": project_id, "host_id": host_id,
                       "project_name": plane.projects[project_id]["name"],
                       "version": version, "artifact_id": artifact_id,
                       "deployment_id": deployment_id}
            if body.get("requested_host_port") is not None:
                payload["requested_host_port"] = body["requested_host_port"]
            if artifact_checksum is not None:
                payload["artifact_checksum"] = artifact_checksum
                payload["artifact_size"] = artifact_size
            # Test-only escape hatch: extra task payload keys (e.g.
            # healthcheck_timeout) that the real API body does not accept.
            payload.update(body.get("task_extra") or {})
            plane.deployments[deployment_id] = {
                "id": deployment_id, "project_id": project_id,
                "host_id": host_id, "version": version,
                "artifact_id": artifact_id, "mode": mode,
                "status": "requested", "health_status": None,
                "task_id": task_id, "ports": None,
                "created_at": time.time(),
            }
            plane.tasks[task_id] = {
                "id": task_id, "type": "deploy", "status": task_status,
                "created_by": "e2e-agent", "assigned_to": host_id,
                "claimed_by": None, "payload": payload, "result": None,
                "error": None, "priority": 0, "attempts": 0,
                "max_attempts": 3, "created_at": time.time(),
            }
            deployment = plane.deployments[deployment_id]
            task = plane.tasks[task_id]
        plane._event("deployment.requested", actor_type="agent",
                     actor_id="e2e-agent", task_id=task_id,
                     deployment_id=deployment_id, host_id=host_id,
                     payload={"version": version, "mode": mode})
        plane._event("task.created", actor_type="agent", actor_id="e2e-agent",
                     task_id=task_id, host_id=host_id,
                     payload={"type": "deploy", "mode": mode})
        self._send_json(201, {"deployment": _public_deployment(deployment),
                              "task": self._public_task(task)})

    def _task_create(self):
        body = self._read_json()
        plane = self.plane
        task_id = plane._new_id("task")
        with plane.lock:
            plane.tasks[task_id] = {
                "id": task_id, "type": body.get("type", "status"),
                "status": "queued", "created_by": "e2e-agent",
                "assigned_to": body.get("assigned_to"),
                "claimed_by": None, "payload": body.get("payload") or {},
                "result": None, "error": None, "priority": 0,
                "attempts": 0, "max_attempts": 3, "created_at": time.time(),
            }
            task = plane.tasks[task_id]
        plane._event("task.created", actor_type="agent", actor_id="e2e-agent",
                     task_id=task_id, payload={"type": task["type"]})
        self._send_json(201, {"task": self._public_task(task)})

    def _approve(self, task_id, approve):
        # Mirror of decideApproval in routes/tasks.ts.
        plane = self.plane
        with plane.lock:
            task = plane.tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "not_found", "task not found")
            target = "queued" if approve else "cancelled"
            if task["status"] != "awaiting_approval" or \
                    target not in _TASK_TRANSITIONS["awaiting_approval"]:
                raise _HttpError(409, "conflict",
                                 f"task is {task['status']}; only awaiting_approval "
                                 f"tasks can be {'approved' if approve else 'rejected'}")
            task["status"] = target
            deployment_id = (task.get("payload") or {}).get("deployment_id")
            if approve and deployment_id and deployment_id in plane.deployments:
                plane.deployments[deployment_id]["status"] = "approved"
                plane._event("deployment.approved", actor_type="agent",
                             actor_id="e2e-agent", task_id=task_id,
                             deployment_id=deployment_id,
                             host_id=task.get("assigned_to"))
            plane._event("task.approved" if approve else "task.rejected",
                         actor_type="agent", actor_id="e2e-agent",
                         task_id=task_id, host_id=task.get("assigned_to"))
        self._send_json(200, {"task": self._public_task(task)})

    def _task_get(self, task_id):
        plane = self.plane
        with plane.lock:
            task = plane.tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "not_found", "task not found")
            return self._send_json(200, {"task": self._public_task(task)})

    def _deployment_get(self, deployment_id):
        plane = self.plane
        with plane.lock:
            deployment = plane.deployments.get(deployment_id)
            if deployment is None:
                raise _HttpError(404, "not_found", "deployment not found")
            return self._send_json(200, {"deployment": _public_deployment(deployment)})


def _public_artifact(artifact):
    return {k: v for k, v in artifact.items() if k != "bytes"}


def _public_deployment(deployment):
    return dict(deployment)


class _HttpError(Exception):
    def __init__(self, code, errcode, message):
        super().__init__(message)
        self.code = code
        self.errcode = errcode
        self.message = message

# ---------------------------------------------------------------------------
# Part 2: SubprocessDockerClient — fake docker.client.DockerClient.
#
# There is no Docker daemon in this sandbox, so `run()` launches the
# artifact's own server.py as an OS subprocess (recorded build context ->
# image tag at build() time), with PORT=<mapped host port> injected. The
# fake collapses the host_port -> container_port mapping the way
# `docker -p host:container` would; the HTTP traffic the health checker
# sees is served by the REAL demo-app code extracted from the REAL
# artifact tarball. Lifecycle (build/run/stop/start/rm/ps/logs/inspect),
# naming, ports and env are faithful to the interface the pipeline uses.
# What is NOT simulated: network-namespace isolation, image layers,
# resource enforcement.
# ---------------------------------------------------------------------------

class DockerBuildError(Exception):
    """The fake `docker build` failed (deterministic, artifact-driven)."""


class SubprocessDockerClient:
    def __init__(self):
        self._lock = threading.RLock()
        self.calls = []            # every method call, in order
        self.images = set()        # tags this fake "built"
        self._image_ctx = {}       # tag -> build context dir (for run())
        self.containers = {}       # name -> record dict
        self.build_calls = []
        self.run_calls = []
        self.start_calls = []
        self.stop_calls = []
        self.rm_calls = []
        self.rmi_calls = []

    def _record(self, name, *args):
        with self._lock:
            self.calls.append((name, args))

    # -- interface used by the pipeline / handlers / gc --------------------
    def version(self):
        self._record("version")
        return "99.0-e2e"

    def compose_available(self):
        self._record("compose_available")
        return True

    def build(self, context_dir, dockerfile, tag, build_args=None, timeout=1200):
        self._record("build", tag)
        with self._lock:
            self.build_calls.append(tag)
            # Deterministic, artifact-driven build failure: the E2E suite
            # ships a `.fail-build` marker file inside broken-app artifacts.
            if (Path(context_dir) / ".fail-build").exists():
                raise DockerBuildError(
                    f"fake docker build failed for {tag}: .fail-build marker "
                    f"present in {context_dir} (simulated Dockerfile failure)")
            self.images.add(tag)
            self._image_ctx[tag] = str(context_dir)
        return "fake build output"

    def image_exists(self, tag):
        self._record("image_exists", tag)
        with self._lock:
            return tag in self.images

    def remove_image(self, tag):
        self._record("remove_image", tag)
        with self._lock:
            self.rmi_calls.append(tag)
            self.images.discard(tag)
            self._image_ctx.pop(tag, None)

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        self._record("run", name)
        ports = dict(ports or {})
        env = {str(k): str(v) for k, v in (env or {}).items()}
        with self._lock:
            self.run_calls.append({"name": name, "image": image,
                                   "ports": dict(ports), "env": dict(env)})
            context = self._image_ctx.get(image)
            if context is None:
                raise DockerBuildError(
                    f"no build context recorded for image {image!r}; "
                    f"the E2E fake only runs images it built")
            host_port = next(iter(ports)) if ports else None
            if host_port is None:
                raise DockerBuildError("E2E fake requires a published host port")
            server_py = Path(context) / "server.py"
            if not server_py.is_file():
                raise DockerBuildError(f"no server.py in build context {context}")
            child_env = dict(os.environ)
            child_env.update(env)
            # Collapse the host_port -> container_port mapping: the
            # "container" listens on the mapped host port directly.
            child_env["PORT"] = str(host_port)
            log_path = Path(context) / f".e2e-container-{name}.log"
            log_fh = open(log_path, "ab")
            proc = subprocess.Popen(
                [sys.executable, "server.py"],
                cwd=context, env=child_env,
                stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            record = {"name": name, "image": image, "ports": dict(ports),
                      "env": dict(env), "proc": proc, "log_fh": log_fh,
                      "log_path": str(log_path),
                      "spec": {"image": image, "ports": dict(ports),
                               "env": dict(env), "memory": memory,
                               "cpus": cpus, "restart": restart,
                               "context": context}}
            self.containers[name] = record
        self._wait_listening(host_port, name)
        return f"e2e-pid-{proc.pid}"

    def _wait_listening(self, port, name, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                record = self.containers.get(name)
                proc = record["proc"] if record else None
            if proc is not None and proc.poll() is not None:
                raise DockerBuildError(
                    f"container {name} exited immediately (code {proc.poll()}); "
                    f"see {record['log_path']}")
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise DockerBuildError(f"container {name} never listened on :{port}")

    def start(self, name, timeout=120):
        self._record("start", name)
        with self._lock:
            self.start_calls.append(name)
            record = self.containers.get(name)
            if record is None:
                raise DockerBuildError(f"no such container {name}")
            proc = record["proc"]
            if proc.poll() is None:
                return  # already running
            # Relaunch from the stored spec (rollback path).
            spec = record["spec"]
            host_port = next(iter(spec["ports"]))
            child_env = dict(os.environ)
            child_env.update(spec["env"])
            child_env["PORT"] = str(host_port)
            log_fh = open(record["log_path"], "ab")
            new_proc = subprocess.Popen(
                [sys.executable, "server.py"],
                cwd=spec["context"], env=child_env,
                stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                record["log_fh"].close()
            except OSError:
                pass
            record["proc"] = new_proc
            record["log_fh"] = log_fh
        self._wait_listening(host_port, name)

    def stop(self, name, timeout_secs=10, timeout=120):
        self._record("stop", name)
        with self._lock:
            self.stop_calls.append(name)
            record = self.containers.get(name)
            if record is None:
                return
            proc = record["proc"]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def restart_container(self, name, timeout=120):
        self._record("restart_container", name)
        self.stop(name)
        self.start(name)

    def rm(self, name, force=False, timeout=120):
        self._record("rm", name)
        with self._lock:
            self.rm_calls.append(name)
            record = self.containers.pop(name, None)
        if record is not None:
            proc = record["proc"]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            try:
                record["log_fh"].close()
            except OSError:
                pass

    def container_exists(self, name):
        self._record("container_exists", name)
        with self._lock:
            return name in self.containers

    def container_status(self, name):
        self._record("container_status", name)
        with self._lock:
            record = self.containers.get(name)
            if record is None:
                return None
            return "running" if record["proc"].poll() is None else "exited"

    def logs(self, name, tail=500):
        self._record("logs", name)
        with self._lock:
            record = self.containers.get(name)
            if record is None:
                return ""
            path = record["log_path"]
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return ""
        lines = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def ps(self, all=False):
        self._record("ps", all)
        rows = []
        with self._lock:
            items = list(self.containers.items())
        for name, record in items:
            running = record["proc"].poll() is None
            if not running and not all:
                continue
            ports = record["ports"] if running else {}
            cell = ", ".join(f"0.0.0.0:{hp}->{cp}/tcp"
                             for hp, cp in ports.items())
            rows.append({"Names": "/" + name, "Ports": cell,
                         "State": "running" if running else "exited"})
        return rows

    def inspect(self, name):
        self._record("inspect", name)
        with self._lock:
            record = self.containers.get(name)
            if record is None:
                return []
            running = record["proc"].poll() is None
            ports = record["ports"] if running else {}
        bindings = {f"{cp}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(hp)}]
                    for hp, cp in ports.items()}
        return [{"Config": {"Image": record["image"]},
                 "State": {"Status": "running" if running else "exited"},
                 "NetworkSettings": {"Ports": bindings}}]

    def env_of(self, name):
        """Test helper: the env the container was launched with."""
        with self._lock:
            return dict(self.containers[name]["env"])

    def shutdown_all(self):
        with self._lock:
            names = list(self.containers)
        for name in names:
            try:
                self.rm(name, force=True)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Part 3: AgentClient — drives the test-only agent endpoints (artifact
# upload, deployment creation, approve/reject, status polling). disconnect()
# / reconnect() simulate the agent disappearing and coming back; the agent
# holds no deployment state — everything lives on the control plane.
# ---------------------------------------------------------------------------

class AgentClient:
    def __init__(self, plane: FakeControlPlane):
        self.plane = plane
        self.session = requests.Session()

    @property
    def base(self):
        return self.plane.url

    def disconnect(self):
        """The agent disappears (drops its session, stops polling)."""
        self.session.close()

    def reconnect(self):
        """The agent comes back later with a fresh session."""
        self.session = requests.Session()

    def _post(self, path, body=None):
        resp = self.session.post(self.base + path, json=body or {}, timeout=30)
        assert resp.status_code in (200, 201), f"POST {path}: {resp.status_code} {resp.text[:300]}"
        return resp.json()

    def _get(self, path):
        resp = self.session.get(self.base + path, timeout=30)
        assert resp.status_code == 200, f"GET {path}: {resp.status_code} {resp.text[:300]}"
        return resp.json()

    def create_project(self, name):
        return self._post("/v1/test/agent/projects", {"name": name})["project"]

    def set_project_secrets(self, project_id, secrets: dict):
        return self._post(f"/v1/test/agent/projects/{project_id}/secrets",
                          {"secrets": secrets})

    def upload_artifact(self, project_id, src_dir, filename="app.tar.gz"):
        """Tar src_dir, init + PUT + ready. Returns the artifact (with checksum)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for p in sorted(Path(src_dir).rglob("*")):
                if p.is_file():
                    tf.add(p, arcname=str(p.relative_to(src_dir)))
        data = buf.getvalue()
        init = self._post("/v1/test/agent/artifacts/init",
                          {"project_id": project_id, "filename": filename,
                           "size": len(data)})["artifact"]
        resp = self.session.put(
            f"{self.base}/v1/test/agent/artifacts/{init['id']}/content",
            data=data, headers={"Content-Type": "application/octet-stream"},
            timeout=30)
        assert resp.status_code == 200, f"PUT content: {resp.status_code} {resp.text[:200]}"
        ready = self._post(f"/v1/test/agent/artifacts/{init['id']}/ready")
        return ready["artifact"]

    def create_deployment(self, project_id, host_id, version, artifact_id,
                          mode="automatic", task_extra=None, requested_host_port=None):
        body = {"project_id": project_id, "host_id": host_id,
                "version": version, "artifact_id": artifact_id, "mode": mode}
        if task_extra:
            body["task_extra"] = task_extra
        if requested_host_port is not None:
            body["requested_host_port"] = requested_host_port
        out = self._post("/v1/test/agent/deployments", body)
        return out["deployment"], out["task"]

    def create_task(self, type, payload=None, assigned_to=None):
        return self._post("/v1/test/agent/tasks",
                          {"type": type, "payload": payload or {},
                           "assigned_to": assigned_to})["task"]

    def approve(self, task_id):
        return self._post(f"/v1/test/agent/tasks/{task_id}/approve")["task"]

    def reject(self, task_id):
        return self._post(f"/v1/test/agent/tasks/{task_id}/reject")["task"]

    def get_task(self, task_id):
        return self._get(f"/v1/test/agent/tasks/{task_id}")["task"]

    def get_deployment(self, deployment_id):
        return self._get(f"/v1/test/agent/deployments/{deployment_id}")["deployment"]


# ---------------------------------------------------------------------------
# Part 4: WorkerRig — the REAL worker side: real ControlPlaneClient, real
# TaskDispatcher, real WorkerContext/LogStore/DeploymentStore, fake docker.
# ---------------------------------------------------------------------------

@dataclass
class _WorkerConfig:
    work_dir: str
    apps_dir: str
    host_token: str = ""


class WorkerRig:
    """A worker host: registers via the REAL client, then runs claim/dispatch
    cycles exactly like agent/main.py's claim loop (minus the thread pool)."""

    def __init__(self, plane: FakeControlPlane, work_parent: Path, name="e2e-host"):
        self.plane = plane
        # Register with a throwaway client first (the fake plane needs no
        # auth for registration, mirroring the provisioning gate).
        bootstrap = ControlPlaneClient(plane.url, "bootstrap")
        reg = bootstrap.register_host(
            name, ["docker", "docker-compose", "static"], "9.9.9-e2e")
        self.host_id = reg["host"]["id"]
        self.host_token = reg["host_token"]
        self.api = ControlPlaneClient(plane.url, self.host_token)
        self.docker = SubprocessDockerClient()
        work_dir = work_parent / f"work-{name}"
        apps_dir = work_parent / f"apps-{name}"
        work_dir.mkdir(parents=True, exist_ok=True)
        apps_dir.mkdir(parents=True, exist_ok=True)
        config = _WorkerConfig(work_dir=str(work_dir), apps_dir=str(apps_dir),
                               host_token=self.host_token)
        self.ctx = WorkerContext(
            config=config,
            api=self.api,
            docker=self.docker,
            log_store=LogStore(str(work_parent / f"logs-{name}")),
            deployment_store=DeploymentStore(str(work_dir)),
        )
        _RIGS.append(self)

    def heartbeat(self):
        return self.api.heartbeat(self.host_id, {
            "cpu_pct": 1.0, "ram_pct": 10.0, "disk_pct": 20.0,
            "docker_status": "ok",
            "running_apps": [],
            "worker_version": "9.9.9-e2e",
        })

    def cycle(self, wait=3):
        """One worker iteration: heartbeat, claim (long-poll), dispatch.

        Returns the dispatch outcome dict, or None when no task was claimed.
        """
        self.heartbeat()
        task = self.api.claim_task(self.host_id, ["docker"], wait=wait)
        if task is None:
            return None
        return TaskDispatcher(self.ctx, self.api).dispatch(task)

    def close(self):
        self.docker.shutdown_all()


_RIGS: list = []


@pytest.fixture
def plane():
    p = FakeControlPlane().start()
    yield p
    p.shutdown()


@pytest.fixture
def rig(plane, tmp_path):
    return WorkerRig(plane, tmp_path / "worker")


@pytest.fixture
def agent(plane):
    return AgentClient(plane)


@pytest.fixture(autouse=True)
def _cleanup_rigs():
    yield
    for r in _RIGS:
        try:
            r.close()
        except Exception:
            pass
    _RIGS.clear()


# ---------------------------------------------------------------------------
# Part 5: artifact builders — real tarballs of the real demo app.
# ---------------------------------------------------------------------------

def make_demo_src(dest: Path, *, app_name="demo-app", version="1.0.0",
                  unhealthy=False, broken_build=False) -> Path:
    """Copy examples/demo-app to dest, patched per variant. Returns src dir."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(DEMO_APP_SRC, dest)
    manifest_path = dest / "agent.deploy.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["name"] = app_name
    env = dict(manifest.get("env") or {})
    env["APP_NAME"] = app_name
    env["APP_VERSION"] = version
    if unhealthy:
        # Drives /health -> 500 via the REAL server.py HEALTH_FAIL switch.
        env["HEALTH_FAIL"] = "1"
    manifest["env"] = env
    manifest_path.write_text(json.dumps(manifest))
    if broken_build:
        # Deterministic, artifact-driven build failure for the fake docker.
        (dest / ".fail-build").write_text(
            "simulated Dockerfile failure for the broken-app scenario\n")
    return dest


def assert_correlation(plane, agent, task_id, deployment_id, project_id,
                       artifact_id, host_id):
    """The correlation chain: task <-> deployment <-> project <-> artifact
    <-> host, on both the task payload and the deployment row."""
    task = agent.get_task(task_id)
    dep = agent.get_deployment(deployment_id)
    payload = task["payload"]
    assert payload["deployment_id"] == deployment_id
    assert payload["project_id"] == project_id
    assert payload["artifact_id"] == artifact_id
    assert payload["artifact_checksum"], "deploy payload must carry the checksum"
    assert dep["project_id"] == project_id
    assert dep["host_id"] == host_id
    assert dep["artifact_id"] == artifact_id
    assert dep["task_id"] == task_id
    # Spot-check the key events carry the full linkage.
    claimed = [e for e in plane.events
               if e["type"] == "task.claimed" and e["task_id"] == task_id]
    assert len(claimed) == 1
    assert claimed[0]["host_id"] == host_id
    completed = [e for e in plane.events
                 if e["type"] == "deployment.completed"
                 and e["deployment_id"] == deployment_id]
    assert len(completed) == 1
    assert completed[0]["task_id"] == task_id
    assert completed[0]["host_id"] == host_id


def _container_name_for(rig, deployment_id):
    matches = [c["name"] for c in rig.docker.run_calls
               if c["name"].startswith("uaht-")]
    assert matches, "no containers were run"
    return matches[-1]


def _wait_http_ok(port, path="/health", timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"http://127.0.0.1:{port}{path}", timeout=3)
            return resp
        except requests.RequestException as exc:
            last = exc
            time.sleep(0.3)
    raise AssertionError(f"GET :{port}{path} never succeeded: {last}")

# ---------------------------------------------------------------------------
# Part 6: scenarios
# ---------------------------------------------------------------------------

# (a) FULL CHAIN — agent uploads artifact -> deployment -> worker claims ->
# downloads -> verifies -> builds -> runs -> healthchecks -> completed.
def test_full_chain(plane, rig, agent, tmp_path):
    project = agent.create_project("demo-app")
    agent.set_project_secrets(project["id"], {"API_TOKEN": "s3cr3t-e2e-value"})
    src = make_demo_src(tmp_path / "src-a", version="1.0.0")
    artifact = agent.upload_artifact(project["id"], src)
    assert artifact["checksum"].startswith("sha256:")
    assert artifact["status"] == "ready"

    deployment, task = agent.create_deployment(
        project["id"], rig.host_id, "1.0.0", artifact["id"], mode="automatic")
    assert task["status"] == "queued"
    assert task["payload"]["artifact_checksum"] == artifact["checksum"]

    outcome = rig.cycle(wait=5)
    assert outcome is not None and outcome["status"] == "completed"
    result = outcome["result"]
    assert result["status"] == "running"
    host_port = int(next(iter(result["ports"])))

    # Control-plane state.
    assert agent.get_task(task["id"])["status"] == "completed"
    assert agent.get_deployment(deployment["id"])["status"] == "running"
    flow = [t for t in plane.event_types() if t != "host.registered"]
    assert flow == [
        "deployment.requested", "task.created",
        "task.claimed",
        "task.started", "deployment.started",
        "task.completed", "deployment.completed",
    ]

    # The app is really serving: real HTTP against the real demo-app code.
    health = _wait_http_ok(host_port, "/health")
    assert health.status_code == 200 and health.json() == {"ok": True}
    index = requests.get(f"http://127.0.0.1:{host_port}/", timeout=5)
    assert index.status_code == 200
    assert "demo-app" in index.text and "1.0.0" in index.text

    # Container lifecycle + logs.
    name = _container_name_for(rig, deployment["id"])
    assert rig.docker.container_status(name) == "running"
    task_log = Path(rig.ctx.log_store.task_log_path(task["id"])).read_text()
    assert len(task_log) > 0
    assert "deploy OK" in task_log

    # Secrets reached the container env, never the state file.
    assert rig.docker.env_of(name)["API_TOKEN"] == "s3cr3t-e2e-value"
    state = rig.ctx.deployment_store.load(deployment["id"])
    assert state["status"] == "running"
    assert "s3cr3t-e2e-value" not in json.dumps(state)

    assert_correlation(plane, agent, task["id"], deployment["id"],
                       project["id"], artifact["id"], rig.host_id)


# (b) AGENT DISAPPEARANCE — the agent drops mid-deploy; the deployment
# completes anyway; a reconnected agent retrieves the final state.
def test_agent_disappearance(plane, rig, agent, tmp_path):
    project = agent.create_project("demo-app")
    src = make_demo_src(tmp_path / "src-b", version="1.0.0")
    artifact = agent.upload_artifact(project["id"], src)
    deployment, task = agent.create_deployment(
        project["id"], rig.host_id, "1.0.0", artifact["id"], mode="automatic")

    outcome_holder = {}

    def worker():
        outcome_holder["outcome"] = rig.cycle(wait=5)

    worker_thread = threading.Thread(target=worker, name="e2e-worker", daemon=True)
    worker_thread.start()
    # The agent polls a couple of times, then disappears while the task is
    # still in flight (never terminal here — the worker hasn't finished).
    for _ in range(6):
        seen = agent.get_task(task["id"])["status"]
        if seen in ("queued", "claimed", "running"):
            break
        time.sleep(0.3)
    assert seen in ("queued", "claimed", "running"), f"task already {seen}"
    agent.disconnect()  # gone: no more polling, no session

    worker_thread.join(timeout=90)
    assert worker_thread.is_alive() is False
    assert outcome_holder["outcome"]["status"] == "completed"

    # The agent comes back later with a fresh client: everything is there.
    agent2 = AgentClient(plane)
    final_task = agent2.get_task(task["id"])
    assert final_task["status"] == "completed"
    assert final_task["result"]["status"] == "running"
    assert final_task["result"]["ports"]
    final_dep = agent2.get_deployment(deployment["id"])
    assert final_dep["status"] == "running"
    host_port = int(next(iter(final_task["result"]["ports"])))
    assert _wait_http_ok(host_port, "/health").status_code == 200
    assert plane.event_types()[-2:] == ["task.completed", "deployment.completed"]


# (c) MANUAL MODE — awaiting_approval gates the worker; approve proceeds,
# reject never starts.
def test_manual_mode(plane, rig, agent, tmp_path):
    project = agent.create_project("demo-app")
    src = make_demo_src(tmp_path / "src-c", version="1.0.0")
    artifact = agent.upload_artifact(project["id"], src)

    deployment, task = agent.create_deployment(
        project["id"], rig.host_id, "1.0.0", artifact["id"], mode="manual")
    assert task["status"] == "awaiting_approval"

    # The worker must not touch an unapproved deployment.
    assert rig.cycle(wait=2) is None
    assert rig.docker.build_calls == [] and rig.docker.run_calls == []

    agent.approve(task["id"])
    assert agent.get_task(task["id"])["status"] == "queued"
    outcome = rig.cycle(wait=5)
    assert outcome["status"] == "completed"
    assert agent.get_deployment(deployment["id"])["status"] == "running"
    flow = [t for t in plane.event_types() if t != "host.registered"]
    assert flow == [
        "deployment.requested", "task.created",
        "deployment.approved", "task.approved",
        "task.claimed",
        "task.started", "deployment.started",
        "task.completed", "deployment.completed",
    ]

    # A rejected deployment never starts: zero docker calls for it.
    n_builds = len(rig.docker.build_calls)
    n_runs = len(rig.docker.run_calls)
    deployment2, task2 = agent.create_deployment(
        project["id"], rig.host_id, "2.0.0", artifact["id"], mode="manual")
    agent.reject(task2["id"])
    assert agent.get_task(task2["id"])["status"] == "cancelled"
    assert rig.cycle(wait=2) is None
    assert len(rig.docker.build_calls) == n_builds
    assert len(rig.docker.run_calls) == n_runs
    assert agent.get_deployment(deployment2["id"])["status"] == "requested"


# (d) BROKEN APP — failing build: task failed, logs retained, the previous
# healthy deployment keeps serving.
def test_broken_app(plane, rig, agent, tmp_path):
    project = agent.create_project("demo-app")
    good_src = make_demo_src(tmp_path / "good", version="1.0.0")
    good_art = agent.upload_artifact(project["id"], good_src)
    dep1, task1 = agent.create_deployment(
        project["id"], rig.host_id, "1.0.0", good_art["id"])
    assert rig.cycle(wait=5)["status"] == "completed"
    v1_name = _container_name_for(rig, dep1["id"])
    v1_port = int(next(iter(agent.get_task(task1["id"])["result"]["ports"])))
    assert _wait_http_ok(v1_port, "/health").status_code == 200

    bad_src = make_demo_src(tmp_path / "bad", version="2.0.0", broken_build=True)
    bad_art = agent.upload_artifact(project["id"], bad_src)
    dep2, task2 = agent.create_deployment(
        project["id"], rig.host_id, "2.0.0", bad_art["id"])
    outcome = rig.cycle(wait=5)
    assert outcome["status"] == "failed"

    assert agent.get_task(task2["id"])["status"] == "failed"
    assert agent.get_deployment(dep2["id"])["status"] == "failed"
    # Logs retained: the task log file exists and has the build failure.
    task_log = Path(rig.ctx.log_store.task_log_path(task2["id"])).read_text()
    assert len(task_log) > 0
    assert "fake docker build failed" in task_log
    # The broken version never ran: no new run calls after v1.
    assert [c["name"] for c in rig.docker.run_calls] == [v1_name]
    # The previous healthy deployment is untouched and still serving.
    assert rig.docker.container_status(v1_name) == "running"
    assert _wait_http_ok(v1_port, "/health").status_code == 200
    assert agent.get_deployment(dep1["id"])["status"] == "running"


# (e) HEALTH-FAIL ROLLBACK — v2 starts but never passes /health: automatic
# rollback, v1 serving again, v2 removed.
def test_health_fail_rollback(plane, rig, agent, tmp_path):
    project = agent.create_project("demo-app")
    v1_src = make_demo_src(tmp_path / "v1", version="1.0.0")
    v1_art = agent.upload_artifact(project["id"], v1_src)
    dep1, task1 = agent.create_deployment(
        project["id"], rig.host_id, "1.0.0", v1_art["id"])
    assert rig.cycle(wait=5)["status"] == "completed"
    v1_name = _container_name_for(rig, dep1["id"])
    v1_port = int(next(iter(agent.get_task(task1["id"])["result"]["ports"])))

    v2_src = make_demo_src(tmp_path / "v2", version="2.0.0", unhealthy=True)
    v2_art = agent.upload_artifact(project["id"], v2_src)
    dep2, task2 = agent.create_deployment(
        project["id"], rig.host_id, "2.0.0", v2_art["id"],
        task_extra={"healthcheck_timeout": 6})
    outcome = rig.cycle(wait=10)
    assert outcome["status"] == "failed"
    assert "healthcheck failed" in (outcome.get("error") or "")

    # v2's container was stopped + removed; v1 was restarted and is healthy.
    v2_names = [c["name"] for c in rig.docker.run_calls if c["name"] != v1_name]
    assert len(v2_names) == 1
    v2_name = v2_names[0]
    assert v2_name not in rig.docker.containers
    assert v2_name in rig.docker.stop_calls and v2_name in rig.docker.rm_calls
    assert v1_name in rig.docker.start_calls
    assert rig.docker.container_status(v1_name) == "running"
    assert _wait_http_ok(v1_port, "/health").status_code == 200
    assert _wait_http_ok(v1_port, "/version").json()["version"] == "1.0.0"

    # Worker-side state: v2 rolled back, v1 running again.
    assert rig.ctx.deployment_store.load(dep2["id"])["status"] == "rolled_back"
    assert rig.ctx.deployment_store.load(dep1["id"])["status"] == "running"
    # Control-plane side: task failed, deployment failed, events recorded.
    assert agent.get_task(task2["id"])["status"] == "failed"
    assert agent.get_deployment(dep2["id"])["status"] == "failed"
    types = plane.event_types()
    assert "deployment.failed" in types and "task.failed" in types
    assert types.index("deployment.started") < types.index("deployment.failed")


# (f) MULTI-APP — three apps, distinct ports/names/dirs; stopping one
# leaves the others alone; logs are separate.
def test_multi_app(plane, rig, agent, tmp_path):
    apps = {}
    for app_name in ("app-a", "app-b", "app-c"):
        project = agent.create_project(app_name)
        src = make_demo_src(tmp_path / app_name, app_name=app_name,
                            version="1.0.0")
        artifact = agent.upload_artifact(project["id"], src)
        deployment, task = agent.create_deployment(
            project["id"], rig.host_id, "1.0.0", artifact["id"])
        outcome = rig.cycle(wait=5)
        assert outcome["status"] == "completed", app_name
        port = int(next(iter(outcome["result"]["ports"])))
        apps[app_name] = {"deployment": deployment, "task": task,
                          "port": port,
                          "name": _container_name_for(rig, deployment["id"]),
                          "project": project, "artifact": artifact}

    names = [a["name"] for a in apps.values()]
    ports = [a["port"] for a in apps.values()]
    assert len(set(names)) == 3, "container names must be distinct"
    assert len(set(ports)) == 3, "host ports must be distinct"
    for app_name, a in apps.items():
        assert rig.docker.container_status(a["name"]) == "running"
        resp = _wait_http_ok(a["port"], "/")
        assert app_name in resp.text, f"{app_name} serves its own page"
        # Distinct state dirs per deployment.
        state_path = Path(rig.ctx.deployment_store.deployment_dir(
            a["deployment"]["id"])) / "state.json"
        assert state_path.is_file()

    # Logs are separate per task.
    log_paths = {app_name: Path(rig.ctx.log_store.task_log_path(a["task"]["id"]))
                 for app_name, a in apps.items()}
    assert len({str(p) for p in log_paths.values()}) == 3
    for app_name, p in log_paths.items():
        assert p.is_file() and len(p.read_text()) > 0, app_name

    # Stop app-b: a and c are unaffected.
    stop_task = agent.create_task(
        "stop", {"deployment_id": apps["app-b"]["deployment"]["id"]},
        assigned_to=rig.host_id)
    stop_outcome = rig.cycle(wait=5)
    assert stop_outcome["status"] == "completed"
    assert rig.docker.container_status(apps["app-b"]["name"]) != "running"
    assert _wait_http_ok(apps["app-a"]["port"], "/health").status_code == 200
    assert _wait_http_ok(apps["app-c"]["port"], "/health").status_code == 200
    with pytest.raises(requests.RequestException):
        requests.get(f"http://127.0.0.1:{apps['app-b']['port']}/health",
                     timeout=5)
    stopped = rig.ctx.deployment_store.load(apps["app-b"]["deployment"]["id"])
    assert stopped["status"] == "stopped"

    for app_name, a in apps.items():
        assert_correlation(plane, agent, a["task"]["id"],
                           a["deployment"]["id"], a["project"]["id"],
                           a["artifact"]["id"], rig.host_id)
