"""uaht_sdk — Python client for the Universal Agent-to-Persistent-Host
Deployment System control plane (wire protocol v1).

Requires Python >= 3.10, depends only on ``requests``.
Implements protocol §1 (auth), §2 (idempotency + error shape),
§3 (endpoints) and §3.8 (SSE stream).
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Callable, Dict, Iterator, Optional
from urllib.parse import quote

import requests

from .errors import UahtError
from .sse import iter_sse_events

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})

# §61: the SDK version has a single source of truth — this constant.
# pyproject.toml "version" must match it (scripts/check-build-consistency.sh
# fails CI on drift); the default User-Agent is derived from it.
__version__ = "1.0.0"


def _rows(body: Any, key: str) -> list:
    """Normalize a list endpoint body to a row list.

    The API wraps rows in ``{key: [...]}``, but some deployments return a
    bare list; handle both shapes (a bare list was previously assumed to be
    a dict, raising AttributeError).
    """
    if isinstance(body, dict):
        return body.get(key, []) or []
    if isinstance(body, list):
        return body
    return []


def _task_logs_text(task: Dict[str, Any]) -> str:
    """Extract the log text from a logs task row (§5: result = {logs: "..."})."""
    result = task.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and isinstance(result.get("logs"), str):
        return result["logs"]
    return ""


class UahtClient:
    """Agent-facing control-plane client (agent API key, §1).

    ``api_key`` is optional: registration flows (``register_agent``,
    ``register_host`` with a provisioning token) run before any agent key
    exists. When omitted, no ``Authorization`` header is sent. The client
    is credential-neutral — a host token can be passed as ``api_key`` for
    host-side operations like ``rotate_host_token``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        user_agent: Optional[str] = None,
    ):
        if not base_url:
            raise TypeError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.user_agent = user_agent if user_agent is not None else f"uaht-sdk/{__version__}"

    # -- internals ----------------------------------------------------
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
    ) -> Any:
        url = f"{self.base_url}/v1{path}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        try:
            base = {"Content-Type": "application/json"} if body is not None else None
            if headers:
                base = dict(base or {})
                base.update(headers)
            resp = self.session.request(
                method,
                url,
                headers=self._headers(base),
                json=body,
                params=params,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise UahtError("connection_error", f"request failed: {exc}") from exc

        if resp.status_code == 204:
            return None
        data = None
        text = resp.text
        if text:
            try:
                data = resp.json()
            except ValueError:
                raise UahtError("bad_response", "server returned non-JSON body", resp.status_code, text) from None
        if not resp.ok:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise UahtError(
                err.get("code", "unknown_error"),
                err.get("message", f"request failed with status {resp.status_code}"),
                resp.status_code,
                data,
            )
        return data

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, body=None, params=None, headers=None):
        return self._request("POST", path, body=body, params=params, headers=headers)

    def _put(self, path, body=None):
        return self._request("PUT", path, body=body)

    def _delete(self, path, body=None):
        return self._request("DELETE", path, body=body)

    # -- §3.1 Agents --------------------------------------------------
    def register_agent(self, name, type=None, capabilities=None, permissions=None, provisioning_token=None):
        """Register a new agent (protocol §3.1).

        The control plane gates this endpoint on the ``X-Provisioning-Token``
        header (bootstrap mode allows the very first registration without
        it). Pass ``provisioning_token``; no agent key is needed — the
        constructor's ``api_key`` may be omitted. Returns ``{"agent": ...,
        "api_key": ...}`` — the api_key is shown once.
        """
        headers = {"X-Provisioning-Token": provisioning_token} if provisioning_token else None
        return self._post(
            "/agents/register",
            {"name": name, "type": type, "capabilities": capabilities, "permissions": permissions},
            headers=headers,
        )

    def me(self):
        return self._get("/agents/me")

    def rotate_agent_key(self):
        """Rotate this agent's own API key (protocol §3.1). The server
        invalidates the old key immediately, so the client adopts the new
        key on success and keeps working without a manual re-auth."""
        res = self._post("/agents/me/rotate", {})
        if isinstance(res, dict) and res.get("api_key"):
            self.api_key = res["api_key"]
        return res

    # -- §3.2 Tasks ---------------------------------------------------
    def create_task(self, type, payload=None, idempotency_key=None, priority=None, host_id=None, mode=None):
        return self._post(
            "/tasks",
            {
                "type": type,
                "payload": payload or {},
                "idempotency_key": idempotency_key,
                "priority": priority,
                "host_id": host_id,
                "mode": mode,
            },
        )

    def get_task(self, task_id):
        return self._get(f"/tasks/{task_id}")

    def list_tasks(self, status=None, type=None, host_id=None, limit=None, cursor=None):
        return self._get("/tasks", {"status": status, "type": type, "host_id": host_id, "limit": limit, "cursor": cursor})

    def cancel_task(self, task_id):
        return self._post(f"/tasks/{task_id}/cancel", {})

    def approve_task(self, task_id):
        return self._post(f"/tasks/{task_id}/approve", {})

    def reject_task(self, task_id):
        return self._post(f"/tasks/{task_id}/reject", {})

    def _resolve_logs_task(self, deployment_id=None, task_id=None):
        """Resolve deployment_id -> a new logs task, or task_id -> the task itself."""
        if not deployment_id and not task_id:
            raise TypeError("deployment_id or task_id is required")
        if task_id:
            cur = self.get_task(task_id)
            return cur.get("task", cur)
        created = self.create_task(type="logs", payload={"deployment_id": deployment_id})
        return created.get("task", created)

    def tail_logs(self, deployment_id=None, task_id=None, poll_interval=2.0):
        """Yield incremental log chunks until the logs task terminates.

        Mirrors the CLI ``logs --follow`` mode. The already-fetched task is
        processed first so its current logs are yielded before the first
        re-poll.
        """
        task = self._resolve_logs_task(deployment_id=deployment_id, task_id=task_id)
        last_len = 0
        while True:
            logs = _task_logs_text(task)
            chunk = logs[last_len:]
            if chunk:
                yield chunk
                last_len = len(logs)
            if task.get("status") in TERMINAL_TASK_STATES:
                break
            time.sleep(poll_interval)
            cur = self.get_task(task["id"])
            task = cur.get("task", cur)

    def get_logs(self, deployment_id=None, task_id=None, follow=False, poll_interval=3.0, timeout=600.0):
        """Create a ``logs`` task for a deployment (or poll an existing logs
        task) until it reaches a terminal state, then return the log text.

        Mirrors the CLI ``logs`` command. With ``follow=True``, yields each
        new log chunk as it arrives (like ``logs --follow``) instead of
        returning the full text.
        """
        if follow:
            return self.tail_logs(deployment_id=deployment_id, task_id=task_id, poll_interval=poll_interval)
        task = self._resolve_logs_task(deployment_id=deployment_id, task_id=task_id)
        started = time.monotonic()
        while True:
            cur = self.get_task(task["id"])
            task = cur.get("task", cur)
            if task.get("status") in TERMINAL_TASK_STATES:
                break
            if time.monotonic() - started > timeout:
                raise UahtError("timeout", f'logs task {task["id"]} did not finish within {timeout}s')
            time.sleep(poll_interval)
        return _task_logs_text(task)

    # -- §3.4 Hosts ---------------------------------------------------
    def list_hosts(self):
        return self._get("/hosts")

    def get_host(self, host_id):
        return self._get(f"/hosts/{host_id}")

    def register_host(self, name, host_type=None, capabilities=None, worker_version=None, provisioning_token=None):
        """Register a host (protocol §3.4).

        Auth: the provisioning token sent as the ``X-Provisioning-Token``
        header (or as Bearer), OR an agent key with the ``deploy``
        permission (then the constructor's ``api_key`` is used). Returns
        ``{"host": ..., "host_token": ...}`` — the host_token is shown once.
        """
        headers = {"X-Provisioning-Token": provisioning_token} if provisioning_token else None
        return self._post(
            "/hosts/register",
            {
                "name": name,
                "host_type": host_type,
                "capabilities": capabilities,
                "worker_version": worker_version,
            },
            headers=headers,
        )

    def rotate_host_token(self, host_id, grace_seconds=None):
        """Rotate a host's own control-plane token (protocol §3.4).

        Auth is the host token itself (pass it as the constructor's
        ``api_key``). The server invalidates the old token immediately
        unless ``grace_seconds`` (1..3600) is given; the client adopts the
        new token on success. Returns ``{"host_token": ...}``.
        """
        body = {}
        if grace_seconds is not None:
            body["grace_seconds"] = grace_seconds
        res = self._post(f"/hosts/{host_id}/rotate-token", body)
        if isinstance(res, dict) and res.get("host_token"):
            self.api_key = res["host_token"]
        return res

    # -- §3.5 Projects & artifacts ------------------------------------
    def create_project(self, name, owner=None, repository=None, runtime=None, configuration=None):
        return self._post(
            "/projects",
            {"name": name, "owner": owner, "repository": repository, "runtime": runtime, "configuration": configuration},
        )

    def list_projects(self):
        # The server does not paginate this endpoint (no limit/cursor on
        # GET /v1/projects), so the SDK exposes none — do not re-add params
        # the wire ignores.
        return self._get("/projects")

    def get_project(self, project_id):
        return self._get(f"/projects/{project_id}")

    def update_project(self, project_id, configuration=None, repository=None, runtime=None):
        """PUT /v1/projects/:id — the wire accepts {configuration,
        repository, runtime}; all three are exposed here."""
        body = {}
        if configuration is not None:
            body["configuration"] = configuration
        if repository is not None:
            body["repository"] = repository
        if runtime is not None:
            body["runtime"] = runtime
        return self._put(f"/projects/{project_id}", body)

    def list_artifacts(self, project_id=None):
        # GET /v1/artifacts supports ?project_id= only (server-side limit is
        # a fixed 500, no cursor) — the SDK exposes exactly that.
        return self._get("/artifacts", {"project_id": project_id})

    def get_artifact(self, artifact_id):
        return self._get(f"/artifacts/{artifact_id}")

    def init_artifact(self, project_id, file_path, version, filename=None, manifest=None):
        """Register an artifact (§3.5 step 1), computing sha256 client-side.

        `manifest` is the optional agent.deploy.json object: when supplied,
        the control plane validates it (same grammar the worker enforces)
        and the scheduler reserves resources from it instead of the project
        configuration (Fix #1).
        """
        size = os.path.getsize(file_path)
        digest = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        body = {
            "project_id": project_id,
            "filename": filename or os.path.basename(file_path),
            "size": size,
            "checksum": f"sha256:{digest.hexdigest()}",
            "version": version,
        }
        if manifest is not None:
            body["manifest"] = manifest
        return self._post("/artifacts/init", body)

    def upload_artifact(self, upload_url, file_path):
        """Stream raw bytes to the upload URL (§3.5 step 2, octet-stream)."""
        try:
            with open(file_path, "rb") as fh:
                resp = self.session.request(
                    "PUT",
                    f"{self.base_url}{upload_url}",
                    headers=self._headers({"Content-Type": "application/octet-stream"}),
                    data=fh,
                )
        except requests.RequestException as exc:
            raise UahtError("connection_error", f"upload failed: {exc}") from exc
        if not resp.ok:
            try:
                data = resp.json()
            except ValueError:
                data = None
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise UahtError(
                err.get("code", "unknown_error"),
                err.get("message", f"artifact upload failed with status {resp.status_code}"),
                resp.status_code,
                data,
            )
        return True

    def download_artifact(self, artifact_id, file_path):
        """Stream an artifact's bytes to a local file."""
        url = f"{self.base_url}/v1/artifacts/{artifact_id}/download"
        try:
            resp = self.session.request("GET", url, headers=self._headers(), stream=True)
        except requests.RequestException as exc:
            raise UahtError("connection_error", f"download failed: {exc}") from exc
        try:
            if not resp.ok:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                err = data.get("error", {}) if isinstance(data, dict) else {}
                raise UahtError(
                    err.get("code", "unknown_error"),
                    err.get("message", f"artifact download failed with status {resp.status_code}"),
                    resp.status_code,
                    data,
                )
            with open(file_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    fh.write(chunk)
        finally:
            resp.close()
        return file_path

    # -- §3.6 Deployments ----------------------------------------------
    def create_deployment(self, project_id, version, host_id=None, artifact_id=None, mode=None,
                          idempotency_key=None, host_port=None):
        return self._post(
            "/deployments",
            {
                "project_id": project_id,
                "version": version,
                "host_id": host_id,
                "artifact_id": artifact_id,
                "mode": mode,
                "idempotency_key": idempotency_key,
                "host_port": host_port,
            },
        )

    def get_deployment(self, deployment_id):
        return self._get(f"/deployments/{deployment_id}")

    def list_deployments(self, project_id=None, host_id=None, status=None, limit=None):
        # GET /v1/deployments supports ?project_id=&host_id=&status=&limit=
        # (no cursor) — the SDK exposes exactly that.
        return self._get(
            "/deployments",
            {"project_id": project_id, "host_id": host_id, "status": status, "limit": limit},
        )

    def rollback_deployment(self, deployment_id):
        return self._post(f"/deployments/{deployment_id}/rollback", {})

    # -- §3.10 Domains --------------------------------------------------
    def add_domain(self, deployment_id, hostname, ingress=None):
        body = {"deployment_id": deployment_id, "hostname": hostname}
        if ingress is not None:
            body["ingress"] = ingress  # 'tunnel' | 'direct' (Phase 7)
        return self._post("/domains", body)

    def list_domains(self, deployment_id):
        return self._get("/domains", {"deployment_id": deployment_id})

    def get_domain(self, hostname):
        """Fetch one domain's lifecycle row: status, dns_configured,
        tunnel_configured, https_reachable, verified_at, error.

        The hostname is URL-encoded to match the JS SDK's
        ``encodeURIComponent`` (safe='' mirrors its unreserved set closely
        enough for DNS names, and encodes everything else)."""
        return self._get(f"/domains/{quote(hostname, safe='')}")

    def remove_domain(self, deployment_id, hostname):
        return self._delete("/domains", {"deployment_id": deployment_id, "hostname": hostname})

    # -- §3.6 Services -------------------------------------------------
    def list_services(self, host_id=None, limit=None):
        return self._get("/services", {"host_id": host_id, "limit": limit})

    def restart_service(self, service_id):
        return self._post(f"/services/{service_id}/restart", {})

    def stop_service(self, service_id):
        return self._post(f"/services/{service_id}/stop", {})

    def start_service(self, service_id):
        return self._post(f"/services/{service_id}/start", {})

    # -- §3.7 Secrets --------------------------------------------------
    def set_secret(self, project_id, name, value):
        return self._post(f"/projects/{project_id}/secrets", {"name": name, "value": value})

    def list_secrets(self, project_id):
        return self._get(f"/projects/{project_id}/secrets")

    def delete_secret(self, project_id, name):
        return self._delete(f"/projects/{project_id}/secrets/{quote(name, safe='')}")

    # -- §3.8 Events ---------------------------------------------------
    def list_events(self, type=None, since=None, limit=None, cursor=None):
        return self._get("/events", {"type": type, "since": since, "limit": limit, "cursor": cursor})

    def stream_events(self, since=None, on_event: Optional[Callable[[Dict], None]] = None) -> Iterator[Dict]:
        """Yield live event dicts from the SSE stream (§3.8)."""
        url = f"{self.base_url}/v1/events/stream"
        params = {"since": since} if since else None
        try:
            resp = self.session.request(
                "GET",
                url,
                headers=self._headers({"Accept": "text/event-stream"}),
                params=params,
                stream=True,
            )
        except requests.RequestException as exc:
            raise UahtError("connection_error", f"event stream failed: {exc}") from exc
        if not resp.ok:
            try:
                data = resp.json()
            except ValueError:
                data = resp.text or None
            raise UahtError("stream_error", f"event stream failed with status {resp.status_code}", resp.status_code, data)
        try:
            for event in iter_sse_events(resp):
                if on_event:
                    on_event(event)
                yield event
        except requests.RequestException as exc:
            raise UahtError("stream_error", f"event stream interrupted: {exc}") from exc
        finally:
            resp.close()

    # -- §3.9 Misc -----------------------------------------------------
    def health(self):
        return self._get("/health")

    # -- convenience ---------------------------------------------------
    def deploy(
        self,
        project,
        version,
        host=None,
        artifact_id=None,
        mode=None,
        host_port=None,
        wait=False,
        poll_interval=3.0,
        timeout=600.0,
    ):
        """Resolve project (and host) names to ids, create a deployment, and
        optionally wait until the backing task reaches a terminal state.

        Returns ``(deployment, task)``.
        """
        if not project:
            raise TypeError("project (name) is required")
        if not version:
            raise TypeError("version is required")

        rows = _rows(self.list_projects(), "projects")
        match = next((p for p in rows if p.get("name") == project or p.get("id") == project), None)
        if not match:
            raise UahtError("not_found", f'project "{project}" not found')

        host_id = None
        if host:
            hrows = _rows(self.list_hosts(), "hosts")
            hmatch = next((h for h in hrows if h.get("name") == host or h.get("id") == host), None)
            if not hmatch:
                raise UahtError("not_found", f'host "{host}" not found')
            host_id = hmatch["id"]

        created = self.create_deployment(
            project_id=match["id"], version=version, host_id=host_id, artifact_id=artifact_id, mode=mode,
            host_port=host_port,
        )
        deployment = created.get("deployment", created)
        task = created.get("task")

        if wait and task and task.get("id"):
            started = time.monotonic()
            while True:
                cur = self.get_task(task["id"])
                task = cur.get("task", cur)
                if task.get("status") in TERMINAL_TASK_STATES:
                    break
                if time.monotonic() - started > timeout:
                    raise UahtError("timeout", f"deploy wait timed out after {timeout}s")
                time.sleep(poll_interval)
            dep = self.get_deployment(deployment["id"])
            deployment = dep.get("deployment", dep)

        return deployment, task
