"""Outbound-only control-plane HTTP client (host token).

Implements the worker side of PROTOCOL §3.3 (register, heartbeat,
claim with ?wait=, progress) and §3.5 (artifact download). The worker
never listens on a port; every method below opens an outbound HTTPS
connection.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests

REQUEST_TIMEOUT = 60          # normal API calls
CLAIM_TIMEOUT_PAD = 10        # claim long-polls server-side; allow wait + pad


class WorkerAPIError(Exception):
    """Raised when the control plane cannot be reached or rejects a call."""


def _transport(action: str, fn):
    """Run an HTTP call, converting transport failures (DNS, refused,
    reset, timeout — anything below the HTTP layer) into WorkerAPIError.

    This gives every network failure mode a single typed error carrying
    the action that failed, so the retry/backoff loops key on one
    exception family instead of WorkerAPIError-vs-requests internals.
    """
    try:
        return fn()
    except WorkerAPIError:
        raise
    except requests.RequestException as exc:
        raise WorkerAPIError(f"{action} failed: transport error: {exc}") from exc


class RotationError(WorkerAPIError):
    """Raised when host-token rotation cannot be completed safely.

    The client NEVER adopts a new token it has not verified: if rotation
    fails after the server issued a new token, the OLD token is still valid
    for the remainder of the grace window, so the worker keeps running and
    the operator can simply retry.
    """


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
        resp = _transport("host registration", lambda: self.session.post(
            self._url("/v1/hosts/register"),
            data=json.dumps({
                "name": name,
                "host_type": host_type,
                "capabilities": list(capabilities),
                "worker_version": worker_version,
            }),
            timeout=REQUEST_TIMEOUT,
        ))
        if resp.status_code != 201:
            self._raise(resp, "host registration")
        return resp.json()

    def heartbeat(self, host_id: str, payload: dict) -> dict:
        resp = _transport("heartbeat", lambda: self.session.post(
            self._url(f"/v1/hosts/{host_id}/heartbeat"),
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        ))
        if resp.status_code != 200:
            self._raise(resp, "heartbeat")
        return resp.json()

    def claim_task(self, host_id: str, capabilities: list, wait: int) -> Optional[dict]:
        """Long-poll for a task. Returns the task dict, or None on 204."""
        req = build_claim_request(self.base_url, self._token, host_id, capabilities, wait)
        resp = _transport("task claim", lambda: self.session.request(
            req["method"], req["url"], data=json.dumps(req["body"]),
            timeout=req["timeout"],
        ))
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            self._raise(resp, "task claim")
        body = resp.json()
        return body.get("task")

    def progress(self, task_id: str, payload: dict) -> dict:
        resp = _transport(f"progress for task {task_id}", lambda: self.session.post(
            self._url(f"/v1/worker/tasks/{task_id}/progress"),
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        ))
        if resp.status_code != 200:
            self._raise(resp, f"progress for task {task_id}")
        return resp.json()

    # -- §3.5 artifact download ------------------------------------------
    def download_artifact(self, artifact_id: str, dest_path: str,
                          expected_size: Optional[int] = None) -> str:
        """Stream GET /v1/artifacts/:id/download to dest_path. Returns dest_path."""
        # §16: artifact_id comes from the task payload (agent-controlled).
        # makedirs() below runs BEFORE the server's UUID check can 400, so
        # validate here — "../.." must never steer directory creation
        # outside the work dir.
        if (not isinstance(artifact_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}",
                                    artifact_id)):
            raise WorkerAPIError(
                f"refusing to download artifact {artifact_id!r}: invalid "
                f"artifact_id")
        tmp_path = dest_path + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        get = _transport(f"artifact {artifact_id} download", lambda: self.session.get(
            self._url(f"/v1/artifacts/{artifact_id}/download"),
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ))
        with get as resp:
            if resp.status_code != 200:
                self._raise(resp, f"artifact {artifact_id} download")
            received = 0
            try:
                with open(tmp_path, "wb") as fh:
                    try:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                fh.write(chunk)
                                received += len(chunk)
                    except requests.RequestException as exc:
                        raise WorkerAPIError(
                            f"artifact {artifact_id} download failed: "
                            f"transport error: {exc}"
                        ) from exc
            except Exception:
                # A truncated/reset transfer must never leave a partial file
                # behind: the next attempt starts clean (it re-creates
                # tmp_path) and os.replace only ever promotes a complete,
                # size-verified download. Re-raise unchanged so the caller's
                # retry/backoff policy sees the original failure.
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
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
            resp = _transport(f"artifact {artifact_id} upload", lambda: self.session.put(
                self._url(f"/v1/artifacts/{artifact_id}/content"),
                data=fh, headers=headers, timeout=max(REQUEST_TIMEOUT, size // 65536 + 60),
            ))
        if resp.status_code not in (200, 201):
            self._raise(resp, f"artifact {artifact_id} upload")
        return resp.json()

    def get_project_secrets(self, project_id: str) -> dict:
        """Fetch decrypted project secrets for a project this host is
        actively deploying (GET /v1/worker/projects/:id/secrets, host
        token). The server only serves them when this host has a
        claimed/active task or deployment for the project."""
        resp = _transport(f"project {project_id} secrets", lambda: self.session.get(
            self._url(f"/v1/worker/projects/{project_id}/secrets"),
            timeout=REQUEST_TIMEOUT,
        ))
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
        resp = _transport("host domains", lambda: self.session.get(
            self._url("/v1/worker/domains"),
            timeout=REQUEST_TIMEOUT,
        ))
        if resp.status_code != 200:
            self._raise(resp, "host domains")
        return resp.json()

    # -- token rotation (safe sequence) ------------------------------------
    def rotate_host_token(self, host_id: str, grace_seconds: int = 300) -> str:
        """Rotate this host's token without ever disconnecting the worker.

        Safe sequence (mirrors POST /v1/hosts/:id/rotate-token):
          1. authenticate current: the rotation call goes out on the
             CURRENT token (self-match enforced server-side);
          2. the server generates a new token, stores its hash, and — with
             grace_seconds > 0 — keeps the OLD token valid for a grace
             window instead of killing it instantly;
          3. VERIFY the new token authenticates (a side-effect-free
             GET /v1/worker/domains on a throwaway header set) BEFORE
             adopting it — an unverified token is never used or stored;
          4. adopt the new token in-memory (session Authorization <redacted>
             and only then return it so the caller can persist it to
             disk (persist_host_token).

        On any failure after issuance the old token is still valid until
        the grace window expires, so a RotationError here never strands
        the worker: the caller can retry with the old token. The plaintext
        token is never logged.
        """
        resp = _transport("host token rotation", lambda: self.session.post(
            self._url(f"/v1/hosts/{host_id}/rotate-token"),
            data=json.dumps({"grace_seconds": int(grace_seconds)}),
            timeout=REQUEST_TIMEOUT,
        ))
        if resp.status_code != 200:
            self._raise(resp, "host token rotation")
        body = resp.json() or {}
        new_token = body.get("host_token")
        if not isinstance(new_token, str) or not new_token:
            raise RotationError(
                "host token rotation failed: server did not return a host_token"
            )
        # Verify the new token authenticates before adopting it.
        verify = _transport("host token rotation verify", lambda: self.session.get(
            self._url("/v1/worker/domains"),
            headers={"Authorization": f"Bearer {new_token}"},
            timeout=REQUEST_TIMEOUT,
        ))
        if verify.status_code != 200:
            raise RotationError(
                "host token rotation failed: the new token did not "
                f"authenticate (HTTP {verify.status_code}); the old token "
                "remains valid for the rest of the grace window — retry"
            )
        # Adopt in memory only after a successful verification.
        self._token = new_token
        self.session.headers.update({"Authorization": f"Bearer {new_token}"})
        return new_token


def persist_host_token(config_path: str, new_token: str) -> None:
    """Atomically rewrite the WORKER_HOST_TOKEN line in a worker.env file.

    Preserves every other line (comments, ordering). Uses write-to-temp +
    os.replace so a crash can never leave a half-written config. The file
    is chmod'd 0600 (owner-only) since it holds a live credential. The
    token value is never logged or printed by this function.
    """
    if not config_path or not os.path.isfile(config_path):
        raise OSError(f"config file not found: {config_path!r}")
    with open(config_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        bare = stripped[len("export "):] if stripped.startswith("export ") else stripped
        if bare.startswith("WORKER_HOST_TOKEN=") and not replaced:
            out.append(f"WORKER_HOST_TOKEN={new_token}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"WORKER_HOST_TOKEN={new_token}\n")
    tmp_path = config_path + ".tmp-rotate"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, config_path)
