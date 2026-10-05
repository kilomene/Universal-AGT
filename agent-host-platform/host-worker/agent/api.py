"""Outbound-only control-plane HTTP client (host token).

Implements the worker side of PROTOCOL §3.3 (register, heartbeat,
claim with ?wait=, progress) and §3.5 (artifact download). The worker
never listens on a port; every method below opens an outbound HTTPS
connection.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import requests

REQUEST_TIMEOUT = 60          # normal API calls
CLAIM_TIMEOUT_PAD = 10        # claim long-polls server-side; allow wait + pad


class WorkerAPIError(Exception):
    """Raised when the control plane cannot be reached or rejects a call."""


# Client-side mirror of the task status machine (schema.sql + PROTOCOL §3.2).
# The progress endpoint accepts: claimed | running | awaiting_approval |
# completed | failed. Self-transitions on non-terminal states are allowed so
# log_chunk updates can stream without changing state.
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "queued": ("claimed", "cancelled"),
    "claimed": ("claimed", "running", "failed", "cancelled"),
    "running": ("running", "completed", "failed", "awaiting_approval", "cancelled"),
    "awaiting_approval": ("awaiting_approval", "queued", "cancelled"),
    "retrying": ("retrying", "queued"),
    "completed": (),
    "failed": (),
    "cancelled": (),
}

PROGRESS_STATUSES = ("claimed", "running", "awaiting_approval", "completed", "failed")


def validate_status_transition(current: str, new: str) -> None:
    """Raise ValueError if the task status machine forbids current -> new."""
    allowed = STATUS_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(f"unknown task status {current!r}")
    if new not in allowed:
        raise ValueError(
            f"illegal task status transition: {current!r} -> {new!r} "
            f"(allowed: {list(allowed) or 'none — terminal'})"
        )


def build_claim_request(base_url: str, token: str, host_id: str,
                        capabilities: list, wait: int) -> dict:
    """Build the long-poll claim request (method, url, headers, body)."""
    wait = max(1, min(int(wait), 30))  # protocol max for ?wait=
    url = f"{base_url.rstrip('/')}/v1/worker/tasks/claim?wait={wait}"
    return {
        "method": "POST",
        "url": url,
        "headers": {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        "body": {"host_id": host_id, "capabilities": list(capabilities)},
        "timeout": wait + CLAIM_TIMEOUT_PAD,
    }


def build_progress_payload(current_status: str, new_status: str,
                           log_chunk: Optional[str] = None,
                           result: Optional[dict] = None,
                           error: Optional[str] = None) -> dict:
    """Build a POST /v1/worker/tasks/:id/progress body, validated client-side."""
    validate_status_transition(current_status, new_status)
    if new_status not in PROGRESS_STATUSES:
        raise ValueError(
            f"{new_status!r} is not a worker-reportable progress status "
            f"(allowed: {list(PROGRESS_STATUSES)})"
        )
    payload: dict = {"status": new_status}
    if log_chunk:
        payload["log_chunk"] = log_chunk
    if result is not None:
        payload["result"] = result
    if error:
        payload["error"] = error
    return payload


class ControlPlaneClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "uaht-host-worker",
        })

    # -- helpers ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _raise(self, resp: requests.Response, action: str) -> None:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        raise WorkerAPIError(f"{action} failed: HTTP {resp.status_code}: {detail}")

    # -- §3.3 worker endpoints -------------------------------------------
    def register_host(self, name: str, capabilities: list,
                      worker_version: str, host_type: str = "persistent-linux-host") -> dict:
        resp = self.session.post(
            self._url("/v1/hosts/register"),
            data=json.dumps({
                "name": name,
                "host_type": host_type,
                "capabilities": list(capabilities),
                "worker_version": worker_version,
            }),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 201:
            self._raise(resp, "host registration")
        return resp.json()

    def heartbeat(self, host_id: str, payload: dict) -> dict:
        resp = self.session.post(
            self._url(f"/v1/hosts/{host_id}/heartbeat"),
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            self._raise(resp, "heartbeat")
        return resp.json()

    def claim_task(self, host_id: str, capabilities: list, wait: int) -> Optional[dict]:
        """Long-poll for a task. Returns the task dict, or None on 204."""
        req = build_claim_request(self.base_url, self._token, host_id, capabilities, wait)
        resp = self.session.request(
            req["method"], req["url"], data=json.dumps(req["body"]),
            timeout=req["timeout"],
        )
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            self._raise(resp, "task claim")
        body = resp.json()
        return body.get("task")

    def progress(self, task_id: str, payload: dict) -> dict:
        resp = self.session.post(
            self._url(f"/v1/worker/tasks/{task_id}/progress"),
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            self._raise(resp, f"progress for task {task_id}")
        return resp.json()

    # -- §3.5 artifact download ------------------------------------------
    def download_artifact(self, artifact_id: str, dest_path: str,
                          expected_size: Optional[int] = None) -> str:
        """Stream GET /v1/artifacts/:id/download to dest_path. Returns dest_path."""
        tmp_path = dest_path + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with self.session.get(
            self._url(f"/v1/artifacts/{artifact_id}/download"),
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                self._raise(resp, f"artifact {artifact_id} download")
            received = 0
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
                        received += len(chunk)
        if expected_size is not None and received != expected_size:
            os.remove(tmp_path)
            raise WorkerAPIError(
                f"artifact {artifact_id} size mismatch: got {received}, "
                f"expected {expected_size}"
            )
        os.replace(tmp_path, dest_path)
        return dest_path

    def upload_artifact_content(self, artifact_id: str, file_path: str) -> dict:
        """Stream a local file to PUT /v1/artifacts/:id/content."""
        size = os.path.getsize(file_path)
        headers = {"Content-Type": "application/octet-stream",
                   "Content-Length": str(size)}
        with open(file_path, "rb") as fh:
            resp = self.session.put(
                self._url(f"/v1/artifacts/{artifact_id}/content"),
                data=fh, headers=headers, timeout=max(REQUEST_TIMEOUT, size // 65536 + 60),
            )
        if resp.status_code not in (200, 201):
            self._raise(resp, f"artifact {artifact_id} upload")
        return resp.json()

    def get_project_secrets(self, project_id: str) -> dict:
        """Fetch decrypted project secrets for a project this host is
        actively deploying (GET /v1/worker/projects/:id/secrets, host
        token). The server only serves them when this host has a
        claimed/active task or deployment for the project."""
        resp = self.session.get(
            self._url(f"/v1/worker/projects/{project_id}/secrets"),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 403:
            return {}  # no active work for this project: no secrets
        if resp.status_code != 200:
            self._raise(resp, f"project {project_id} secrets")
        body = resp.json()
        secrets = (body or {}).get("secrets")
        return secrets if isinstance(secrets, dict) else {}

    # -- §3.10 ingress ----------------------------------------------------
    def list_host_domains(self) -> dict:
        """List domain entries on this host's live deployments
        (GET /v1/worker/domains, host token). Used by the ingress route
        sync to map hostnames -> local container ports."""
        resp = self.session.get(
            self._url("/v1/worker/domains"),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            self._raise(resp, "host domains")
        return resp.json()
