"""Shared test harness for the recovery/failure-matrix suites (W13a).

This module is infrastructure, not tests: pytest does not collect it
(the filename does not match test_*.py). It provides:

  * RecoveryPlane — an in-memory control plane over REAL HTTP that
    faithfully mirrors the production routes' semantics:
      - agents.ts: register, /me/rotate (new key, old key dead)
      - tasks.ts: create with idempotency_key (replay / 409), agent read
      - deployments.ts: create (+ deploy task), agent read
      - worker.ts: host register, heartbeat, atomic claim (serialized
        under a lock — the production FOR UPDATE SKIP LOCKED equivalent),
        progress (403 unless the reporter holds the claim, lease refresh,
        task state machine, deployment mirroring)
      - taskSweeper.ts: run_sweep() requeues lease-expired tasks
        (deterministic: the test drives it directly, no timers)
      - artifacts: upload + download with failure injection
    Test-only hooks (all under /v1/test/): agent revoke/reprovision,
    lease expiry forcing, latency/failure injection, claim accounting.
  * FakeDocker — a docker-client double that launches a REAL HTTP server
    subprocess per container, so "the app keeps serving traffic" is a
    genuine socket-level assertion. Supports daemon_restart() (all
    containers stop, records kept) and daemon_nuke() (records lost) to
    model a Docker daemon restart / state loss.
  * wait_until / free_port helpers (poll-based synchronization; no
    sleep-based flakiness).

Determinism: every wait is poll-based with a deadline; failure injection
is explicit; the sweeper and lease expiry are driven by the test, never
by wall-clock timers.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05,
               what: str = "condition"):
    """Poll predicate() until truthy; raise AssertionError on deadline."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 — transient probe failures
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what} "
                         f"(last: {last!r})")


def http_get_text(url: str, timeout: float = 3.0) -> str:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# RecoveryPlane
# ---------------------------------------------------------------------------

_TASK_TRANSITIONS = {
    "queued": {"claimed", "cancelled"},
    "claimed": {"running", "failed", "cancelled"},
    "running": {"completed", "failed", "awaiting_approval", "cancelled"},
    "awaiting_approval": {"queued", "cancelled"},
}


class _HttpError(Exception):
    def __init__(self, code: int, errcode: str, message: str):
        super().__init__(message)
        self.code = code
        self.errcode = errcode


class RecoveryPlane:
    """In-memory control plane over real HTTP (see module docstring)."""

    def __init__(self, lease_s: int = 600):
        self.lease_s = lease_s
        self.lock = threading.RLock()
        self._cond = threading.Condition(self.lock)
        self._claim_lock = threading.Lock()  # serializes claims (SKIP LOCKED)
        self.agents: dict = {}          # agent_id -> row
        self._token_to_agent: dict = {}  # token -> agent_id
        self.hosts: dict = {}           # host_id -> row
        self._token_to_host: dict = {}   # token -> host_id
        self.tasks: dict = {}           # task_id -> row
        self.deployments: dict = {}     # deployment_id -> row
        self.artifacts: dict = {}       # artifact_id -> bytes
        self.events: list = []
        self.claim_deliveries: dict = {}  # task_id -> times handed out by claim
        # failure injection
        self.heartbeat_failures = 0     # next N heartbeats -> 503
        self.download_mode = "ok"       # ok | partial-once
        self._download_calls = 0
        self.latency: dict = {}         # path prefix -> (seconds, times)
        self._id = 0
        self._server = None
        self._thread = None
        self._port: int | None = None  # pinned on first bind: restarts reuse it

    # -- lifecycle ------------------------------------------------------
    def start(self):
        class _Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        handler = _PlaneHandler
        self._server = _Server(("127.0.0.1", self._port or 0), handler)
        self._port = self._server.server_address[1]
        self._server.plane = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="recovery-plane", daemon=True)
        self._thread.start()
        return self

    def shutdown(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            finally:
                self._server.server_close()
                self._thread.join(timeout=5)
                self._server = None

    @property
    def url(self) -> str:
        port = self._port or self._server.server_address[1]
        return f"http://127.0.0.1:{port}"

    # -- idents ----------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        with self.lock:
            self._id += 1
            return f"{prefix}-{self._id:06d}"

    def _event(self, type_: str, **kw):
        with self.lock:
            self.events.append({"type": type_, **kw, "seq": len(self.events)})

    # -- test hooks -------------------------------------------------------
    def revoke_agent(self, agent_id: str) -> None:
        """Simulate the agent disappearing: drop ALL of its credentials."""
        with self.lock:
            row = self.agents[agent_id]
            for tok in list(row["tokens"]):
                self._token_to_agent.pop(tok, None)
            row["tokens"] = set()
        self._event("agent.disappeared", agent_id=agent_id)

    def reprovision_agent(self, agent_id: str) -> str:
        """Operator re-provisions the SAME agent identity with a new key."""
        token = "agt-" + secrets.token_hex(16)
        with self.lock:
            row = self.agents[agent_id]
            row["tokens"].add(token)
            self._token_to_agent[token] = agent_id
        self._event("agent.reprovisioned", agent_id=agent_id)
        return token

    def expire_all_leases(self) -> None:
        """Force every outstanding claim lease into the past (deterministic)."""
        with self.lock:
            for task in self.tasks.values():
                if task["status"] in ("claimed", "running") and task.get("lease_expires_at"):
                    task["lease_expires_at"] = time.time() - 1.0

    def run_sweep(self) -> list:
        """The stuck-task sweeper, driven synchronously by the test.

        Mirrors control-plane/api/src/lib/taskSweeper.ts: claimed/running
        tasks whose lease expired -> queued (attempts+1, claimed_by cleared)
        or failed when the retry budget is exhausted.
        """
        requeued = []
        now = time.time()
        with self.lock:
            for task in self.tasks.values():
                if task["status"] not in ("claimed", "running"):
                    continue
                lease = task.get("lease_expires_at")
                if lease is None or lease > now:
                    continue
                task["attempts"] = task.get("attempts", 0) + 1
                if task["attempts"] >= task.get("max_attempts", 3):
                    task["status"] = "failed"
                    task["error"] = "claim lease expired; retry budget exhausted"
                    task["lease_expires_at"] = None
                    self._event("task.failed", task_id=task["id"],
                                reason="claim_lease_expired")
                else:
                    task["status"] = "queued"
                    task["claimed_by"] = None
                    task["lease_expires_at"] = None
                    requeued.append(task["id"])
                    self._event("task.requeued", task_id=task["id"],
                                reason="claim_lease_expired",
                                attempts=task["attempts"])
        return requeued

    def inject_latency(self, path_prefix: str, seconds: float, times: int = 1):
        with self.lock:
            self.latency[path_prefix] = [seconds, times]

    # -- internal: claim ---------------------------------------------------
    def _try_claim(self, host_id: str):
        """Atomic claim: at most one host gets a queued task (serialized)."""
        with self._claim_lock:
            with self.lock:
                for task in self.tasks.values():
                    if task["status"] == "queued" and task.get("assigned_to") in (None, host_id):
                        task["status"] = "claimed"
                        task["claimed_by"] = host_id
                        task["lease_expires_at"] = time.time() + self.lease_s
                        task["attempts"] = task.get("attempts", 0) + 1
                        self.claim_deliveries[task["id"]] = \
                            self.claim_deliveries.get(task["id"], 0) + 1
                        self._event("task.claimed", task_id=task["id"],
                                    host_id=host_id)
                        return dict(task)
        return None


class _PlaneHandler(BaseHTTPRequestHandler):
    server_version = "RecoveryPlane/1.0"

    def log_message(self, *args):  # keep test output clean
        pass

    @property
    def plane(self) -> RecoveryPlane:
        return self.server.plane

    # -- plumbing --------------------------------------------------------
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

    def _agent(self):
        token = self._bearer()
        with self.plane.lock:
            agent_id = self.plane._token_to_agent.get(token)
            return self.plane.agents.get(agent_id) if agent_id else None

    def _host(self):
        token = self._bearer()
        with self.plane.lock:
            host_id = self.plane._token_to_host.get(token)
            return self.plane.hosts.get(host_id) if host_id else None

    def _apply_latency(self, path: str):
        with self.plane.lock:
            for prefix, (secs, times) in list(self.plane.latency.items()):
                if path.startswith(prefix) and times > 0:
                    self.plane.latency[prefix][1] -= 1
                    delay = secs
                    break
            else:
                return
        time.sleep(delay)

    # -- routing ----------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            self._apply_latency(path)
            if path == "/v1/agents/register":
                return self._agent_register()
            if path == "/v1/agents/me/rotate":
                return self._agent_rotate()
            if path == "/v1/test/agent/revoke":
                return self._agent_revoke()
            if path == "/v1/test/agent/reprovision":
                return self._agent_reprovision()
            if path == "/v1/tasks":
                return self._task_create()
            if path == "/v1/deployments":
                return self._deployment_create()
            if path == "/v1/hosts/register":
                return self._host_register()
            if path.startswith("/v1/hosts/") and path.endswith("/heartbeat"):
                return self._heartbeat(path.split("/")[3])
            if path == "/v1/worker/tasks/claim":
                return self._claim(parsed.query)
            if path.startswith("/v1/worker/tasks/") and path.endswith("/progress"):
                return self._progress(path.split("/")[4])
            if path == "/v1/test/artifacts":
                return self._artifact_upload()
            self._send_json(404, {"error": {"code": "not_found", "message": path}})
        except _HttpError as exc:
            self._send_json(exc.code, {"error": {"code": exc.errcode,
                                                "message": str(exc)}})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            self._apply_latency(path)
            if path.startswith("/v1/tasks/"):
                return self._task_get(path.split("/")[3])
            if path.startswith("/v1/deployments/"):
                return self._deployment_get(path.split("/")[3])
            if path.startswith("/v1/artifacts/") and path.endswith("/download"):
                return self._artifact_download(path.split("/")[3])
            self._send_json(404, {"error": {"code": "not_found", "message": path}})
        except _HttpError as exc:
            self._send_json(exc.code, {"error": {"code": exc.errcode,
                                                "message": str(exc)}})

    def do_PUT(self):
        self._send_json(404, {"error": {"code": "not_found",
                                       "message": self.path}})

    # -- agents -------------------------------------------------------------
    def _agent_register(self):
        body = self._read_json()
        name = (body.get("name") or "").strip()
        if not name:
            raise _HttpError(400, "bad_request", "name is required")
        plane = self.plane
        with plane.lock:
            if any(a["name"] == name for a in plane.agents.values()):
                raise _HttpError(409, "conflict", f"agent name {name!r} exists")
            agent_id = plane._new_id("agent")
            token = "agt-" + secrets.token_hex(16)
            plane.agents[agent_id] = {
                "id": agent_id, "name": name,
                "type": body.get("type") or "generic",
                "tokens": {token},
            }
            plane._token_to_agent[token] = agent_id
        plane._event("agent.registered", agent_id=agent_id, name=name)
        self._send_json(201, {"agent": {"id": agent_id, "name": name,
                                       "type": plane.agents[agent_id]["type"]},
                              "api_key": token})

    def _agent_rotate(self):
        agent = self._agent()
        if agent is None:
            raise _HttpError(401, "unauthorized", "bad agent token")
        plane = self.plane
        new_token = "agt-" + secrets.token_hex(16)
        with plane.lock:
            for tok in list(agent["tokens"]):
                plane._token_to_agent.pop(tok, None)
            agent["tokens"] = {new_token}
            plane._token_to_agent[new_token] = agent["id"]
        plane._event("agent.key_rotated", agent_id=agent["id"])
        self._send_json(200, {"api_key": new_token})

    def _agent_revoke(self):
        body = self._read_json()
        self.plane.revoke_agent(body["agent_id"])
        self._send_json(200, {"revoked": body["agent_id"]})

    def _agent_reprovision(self):
        body = self._read_json()
        token = self.plane.reprovision_agent(body["agent_id"])
        agent = self.plane.agents[body["agent_id"]]
        self._send_json(200, {"agent": {"id": agent["id"], "name": agent["name"],
                                       "type": agent["type"]},
                              "api_key": token})

    # -- tasks --------------------------------------------------------------
    def _task_create(self):
        agent = self._agent()
        if agent is None:
            raise _HttpError(401, "unauthorized", "bad agent token")
        body = self._read_json()
        task_type = body.get("type")
        if not task_type:
            raise _HttpError(400, "bad_request", "type is required")
        key = body.get("idempotency_key")
        plane = self.plane
        with plane.lock:
            if key:
                for t in plane.tasks.values():
                    if t.get("idempotency_key") == key:
                        if t["type"] == task_type and t.get("payload") == (body.get("payload") or {}):
                            self._send_json(200, {"task": self._public_task(t),
                                                 "idempotent_replay": True})
                            return
                        raise _HttpError(409, "conflict",
                                         "idempotency key used with different payload")
            task_id = plane._new_id("task")
            task = {
                "id": task_id, "type": task_type,
                "status": "queued",
                "idempotency_key": key,
                "created_by": agent["id"],
                "assigned_to": body.get("host_id"),
                "claimed_by": None,
                "payload": body.get("payload") or {},
                "result": None, "error": None,
                "attempts": 0, "max_attempts": body.get("max_attempts", 3),
                "lease_expires_at": None,
            }
            plane.tasks[task_id] = task
            with plane._cond:
                plane._cond.notify_all()
        plane._event("task.created", task_id=task_id, agent_id=agent["id"])
        self._send_json(201, {"task": self._public_task(task)})

    def _task_get(self, task_id):
        agent = self._agent()
        if agent is None:
            raise _HttpError(401, "unauthorized", "bad agent token")
        with self.plane.lock:
            task = self.plane.tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "not_found", "task not found")
            view = self._public_task(task)
        self._send_json(200, {"task": view})

    def _public_task(self, task):
        return {k: v for k, v in task.items()}

    # -- deployments ---------------------------------------------------------
    def _deployment_create(self):
        agent = self._agent()
        if agent is None:
            raise _HttpError(401, "unauthorized", "bad agent token")
        body = self._read_json()
        for field in ("project_id", "version"):
            if not body.get(field):
                raise _HttpError(400, "bad_request", f"{field} is required")
        key = body.get("idempotency_key")
        plane = self.plane
        with plane.lock:
            if key:
                for d in plane.deployments.values():
                    if d.get("idempotency_key") == key:
                        dep_task = plane.tasks.get(d["task_id"]) if d.get("task_id") else None
                        self._send_json(200, {
                            "deployment": self._public_dep(d),
                            "task": self._public_task(dep_task) if dep_task else None,
                            "idempotent_replay": True})
                        return
            dep_id = plane._new_id("dep")
            task_id = plane._new_id("task")
            deployment = {
                "id": dep_id, "project_id": body["project_id"],
                "project_name": body.get("project_name", body["project_id"]),
                "version": body["version"], "status": "requested",
                "idempotency_key": key, "task_id": task_id,
                "created_by": agent["id"],
            }
            task = {
                "id": task_id, "type": "deploy", "status": "queued",
                "idempotency_key": f"deploy:{key}" if key else None,
                "created_by": agent["id"],
                "assigned_to": body.get("host_id"),
                "claimed_by": None,
                "payload": {"deployment_id": dep_id,
                            "project_id": body["project_id"],
                            "project_name": body.get("project_name", body["project_id"]),
                            "version": body["version"]},
                "result": None, "error": None,
                "attempts": 0, "max_attempts": 3,
                "lease_expires_at": None,
            }
            plane.deployments[dep_id] = deployment
            plane.tasks[task_id] = task
            with plane._cond:
                plane._cond.notify_all()
        plane._event("deployment.requested", deployment_id=dep_id,
                     agent_id=agent["id"])
        self._send_json(201, {"deployment": self._public_dep(deployment),
                              "task": self._public_task(task)})

    def _deployment_get(self, dep_id):
        agent = self._agent()
        if agent is None:
            raise _HttpError(401, "unauthorized", "bad agent token")
        with self.plane.lock:
            dep = self.plane.deployments.get(dep_id)
            if dep is None:
                raise _HttpError(404, "not_found", "deployment not found")
            view = self._public_dep(dep)
        self._send_json(200, {"deployment": view})

    def _public_dep(self, dep):
        return {k: v for k, v in dep.items()}

    # -- hosts / worker -------------------------------------------------------
    def _host_register(self):
        body = self._read_json()
        plane = self.plane
        host_id = plane._new_id("host")
        token = "hst-" + secrets.token_hex(16)
        with plane.lock:
            plane.hosts[host_id] = {
                "id": host_id, "name": body.get("name", host_id),
                "capabilities": body.get("capabilities", []),
                "token": token, "status": "online",
            }
            plane._token_to_host[token] = host_id
        plane._event("host.registered", host_id=host_id)
        self._send_json(201, {"host": {"id": host_id,
                                      "name": plane.hosts[host_id]["name"]},
                              "host_token": token})

    def _heartbeat(self, host_id):
        host = self._host()
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        if host["id"] != host_id:
            raise _HttpError(403, "forbidden", "host token mismatch")
        plane = self.plane
        with plane.lock:
            if plane.heartbeat_failures > 0:
                plane.heartbeat_failures -= 1
                raise _HttpError(503, "bad_gateway",
                                 "injected heartbeat failure")
            pending = sum(1 for t in plane.tasks.values()
                          if t["status"] == "queued"
                          and t.get("assigned_to") in (None, host_id))
        self._send_json(200, {"host": {"id": host_id}, "pending_tasks": pending})

    def _claim(self, query):
        host = self._host()
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        body = self._read_json()
        if body.get("host_id") != host["id"]:
            raise _HttpError(403, "forbidden", "host token does not match host_id")
        try:
            wait_s = max(0, min(int(urllib.parse.parse_qs(query).get("wait", ["0"])[0]), 30))
        except ValueError:
            wait_s = 0
        plane = self.plane
        deadline = time.monotonic() + wait_s
        while True:
            task = plane._try_claim(host["id"])
            if task is not None:
                self._send_json(200, {"task": task})
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.send_response(204)
                self.end_headers()
                return
            with plane._cond:
                plane._cond.wait(timeout=min(0.2, remaining))

    def _progress(self, task_id):
        host = self._host()
        if host is None:
            raise _HttpError(401, "unauthorized", "bad host token")
        body = self._read_json()
        status = body.get("status")
        plane = self.plane
        with plane.lock:
            task = plane.tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "not_found", "task not found")
            # Lease ownership: only the claiming host may report.
            if task.get("claimed_by") != host["id"]:
                raise _HttpError(403, "forbidden",
                                 "only the claiming host may report progress "
                                 "on this task")
            allowed = _TASK_TRANSITIONS.get(task["status"], set())
            if status not in allowed and status != task["status"]:
                raise _HttpError(409, "conflict",
                                 f"cannot transition task from {task['status']} "
                                 f"to {status}")
            if status != task["status"]:
                task["status"] = status
                plane._event(f"task.{status}", task_id=task_id,
                             host_id=host["id"])
            if body.get("result") is not None:
                task["result"] = body["result"]
            if body.get("error"):
                task["error"] = body["error"]
            terminal = status in ("completed", "failed", "cancelled")
            task["lease_expires_at"] = None if terminal else time.time() + plane.lease_s
            # deployment mirroring (production routes/worker.ts)
            dep_id = (task.get("payload") or {}).get("deployment_id")
            if dep_id and dep_id in plane.deployments:
                dep = plane.deployments[dep_id]
                if status == "running" and dep["status"] == "requested":
                    dep["status"] = "building"
                elif status == "completed":
                    dep["status"] = "running"
                elif status == "failed":
                    dep["status"] = "failed"
            view = self._public_task(task)
        self._send_json(200, {"task": view})

    # -- artifacts --------------------------------------------------------------
    def _artifact_upload(self):
        agent = self._agent()
        if agent is None:
            raise _HttpError(401, "unauthorized", "bad agent token")
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        plane = self.plane
        artifact_id = plane._new_id("art")
        with plane.lock:
            plane.artifacts[artifact_id] = data
        self._send_json(201, {
            "artifact": {
                "id": artifact_id,
                "size": len(data),
                "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        })

    def _artifact_download(self, artifact_id):
        plane = self.plane
        with plane.lock:
            data = plane.artifacts.get(artifact_id)
            if data is None:
                raise _HttpError(404, "not_found", "artifact not found")
            plane._download_calls += 1
            mode = plane.download_mode
            if mode == "partial-once":
                plane.download_mode = "ok"  # only the first GET truncates
                truncate = True
            else:
                truncate = False
        if truncate:
            # Lie about the length, then die mid-body: the client must see
            # a broken transfer, never a short-but-"complete" one.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            half = max(1, len(data) // 2)
            self.wfile.write(data[:half])
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# FakeDocker — launches a REAL HTTP server subprocess per container.
# ---------------------------------------------------------------------------

_SERVER_PY = r"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
marker = sys.argv[2]

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = marker.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


class FakeDocker:
    """docker-client double where containers are real local HTTP servers.

    Implements the slice of the docker.client.DockerClient interface the
    worker uses (version/ps/start/stop/rm/run/inspect/logs/
    container_status). `run()` records the full spec so a daemon restart
    or a host reboot can relaunch the identical server.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.containers: dict = {}  # name -> record
        self.run_calls: list = []
        self.start_calls: list = []
        self.stop_calls: list = []
        self.rm_calls: list = []

    # -- interface ------------------------------------------------------
    def version(self, timeout=5):
        return "fake-docker-1.0"

    def compose_available(self):
        return False

    def _spawn_server(self, port: int, marker: str):
        return subprocess.Popen(
            [sys.executable, "-c", _SERVER_PY, str(port), marker],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )

    def _wait_serving(self, port: int, name: str, timeout: float = 15.0):
        def _up():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return True
            except OSError:
                return False

        wait_until(_up, timeout=timeout,
                   what=f"container {name} serving on :{port}")

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        ports = {int(hp): int(cp) for hp, cp in (ports or {}).items()}
        if not ports:
            raise ValueError("FakeDocker.run requires a published host port")
        host_port = next(iter(ports))
        marker = f"fake-app:{name}"
        proc = self._spawn_server(host_port, marker)
        with self._lock:
            self.run_calls.append({"name": name, "image": image,
                                   "ports": dict(ports),
                                   "env": dict(env or {}),
                                   "memory": memory, "cpus": cpus,
                                   "restart": restart})
            self.containers[name] = {
                "name": name, "image": image, "ports": dict(ports),
                "env": dict(env or {}), "memory": memory, "cpus": cpus,
                "restart": restart, "proc": proc, "marker": marker,
            }
        self._wait_serving(host_port, name)
        return f"fake-id-{name}"

    def ps(self, all=False):
        rows = []
        with self._lock:
            items = list(self.containers.items())
        for name, rec in items:
            running = rec["proc"].poll() is None
            if not running and not all:
                continue
            rows.append({"Names": "/" + name,
                         "State": "running" if running else "exited"})
        return rows

    def start(self, name, timeout=120):
        with self._lock:
            self.start_calls.append(name)
            rec = self.containers.get(name)
            if rec is None:
                raise ValueError(f"no such container {name}")
            if rec["proc"].poll() is None:
                return  # already running
            host_port = next(iter(rec["ports"]))
            rec["proc"] = self._spawn_server(host_port, rec["marker"])
        self._wait_serving(host_port, name)

    def stop(self, name, timeout_secs=10, timeout=120):
        with self._lock:
            self.stop_calls.append(name)
            rec = self.containers.get(name)
            if rec is None:
                return
            proc = rec["proc"]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def rm(self, name, force=False, timeout=120):
        with self._lock:
            self.rm_calls.append(name)
            rec = self.containers.pop(name, None)
        if rec is not None:
            proc = rec["proc"]
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def container_status(self, name):
        with self._lock:
            rec = self.containers.get(name)
            if rec is None:
                return None
            return "running" if rec["proc"].poll() is None else "exited"

    def inspect(self, name):
        with self._lock:
            rec = self.containers.get(name)
            if rec is None:
                return []
            running = rec["proc"].poll() is None
            ports = rec["ports"] if running else {}
        bindings = {f"{cp}/tcp": [{"HostIp": "0.0.0.0",
                                   "HostPort": str(hp)}]
                    for hp, cp in ports.items()}
        return [{"Config": {"Image": rec["image"]},
                 "State": {"Status": "running" if running else "exited"},
                 "NetworkSettings": {"Ports": bindings}}]

    def logs(self, name, tail=500):
        return ""

    def container_port(self, name) -> int:
        with self._lock:
            return next(iter(self.containers[name]["ports"]))

    # -- failure injection ------------------------------------------------
    def daemon_restart(self):
        """The Docker daemon restarts: every container process dies, but the
        daemon still knows the containers (they show as exited)."""
        with self._lock:
            for rec in self.containers.values():
                proc = rec["proc"]
                if proc.poll() is None:
                    proc.kill()
            for rec in self.containers.values():
                try:
                    rec["proc"].wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def daemon_nuke(self):
        """Catastrophic daemon state loss: containers vanish entirely."""
        with self._lock:
            names = list(self.containers)
        for name in names:
            self.rm(name, force=True)

    def shutdown_all(self):
        with self._lock:
            names = list(self.containers)
        for name in names:
            try:
                self.rm(name, force=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WorkerContext scaffolding
# ---------------------------------------------------------------------------

class StubConfig:
    def __init__(self, work_dir: str, host_id: str, host_token: str):
        self.work_dir = work_dir
        self.apps_dir = str(Path(work_dir) / "apps")
        self.host_id = host_id
        self.host_token = host_token
        self.host_name = "recovery-test-host"
        self.worker_version = "test-0.0.0"
        self.control_plane_url = ""
        self.capabilities = ["docker"]
        self.heartbeat_interval = 60
        self.poll_wait = 1
        self.crash_loop_threshold = 5
        self.crash_loop_window_s = 300


def make_ctx(work_dir: str, api, docker, host_id: str = "host-1",
             host_token: str = "hst-test"):
    """Build a real WorkerContext against the fake plane/docker."""
    from agent.context import WorkerContext
    from deployments.state import DeploymentStore
    from logs.store import LogStore

    for sub in ("logs", "deployments", "artifacts", "apps"):
        Path(work_dir, sub).mkdir(parents=True, exist_ok=True)
    config = StubConfig(work_dir, host_id, host_token)
    return WorkerContext(
        config=config,
        api=api,
        docker=docker,
        log_store=LogStore(str(Path(work_dir) / "logs")),
        deployment_store=DeploymentStore(work_dir),
    )
