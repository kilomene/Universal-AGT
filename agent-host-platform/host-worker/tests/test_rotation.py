"""Host token rotation safe sequence (W12 auth hardening).

Covers ControlPlaneClient.rotate_host_token and persist_host_token:
the worker fetches a new token, verifies it authenticates, and only then
adopts + persists it — the old token stays valid during the server grace
window, so a failed rotation never strands the worker.
"""
import json
import os
import stat

import pytest

from agent.api import ControlPlaneClient, RotationError, WorkerAPIError, persist_host_token


OLD_TOKEN = "uagh_oldtoken"
NEW_TOKEN = "uagh_newtoken123"


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeSession:
    """Records calls; serves canned responses per (method, url-suffix)."""

    def __init__(self, routes):
        self.routes = routes  # {(method, suffix): FakeResponse}
        self.calls = []
        self.headers = {}

    def _serve(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp
        raise AssertionError(f"unexpected request: {method} {url}")

    def post(self, url, **kwargs):
        return self._serve("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._serve("GET", url, **kwargs)


def make_client(routes):
    client = ControlPlaneClient.__new__(ControlPlaneClient)
    client.base_url = "https://cp.example.com"
    client._token = OLD_TOKEN
    client.session = FakeSession(routes)
    client.session.headers.update({"Authorization": f"Bearer {OLD_TOKEN}"})
    return client


# -- rotate_host_token: the safe sequence ------------------------------------

def test_rotate_success_verifies_then_adopts():
    client = make_client({
        ("POST", "/v1/hosts/h-1/rotate-token"): FakeResponse(200, {"host_token": NEW_TOKEN}),
        ("GET", "/v1/worker/domains"): FakeResponse(200, {"domains": []}),
    })
    returned = client.rotate_host_token("h-1", grace_seconds=300)
    assert returned == NEW_TOKEN
    # sequence: rotate on the OLD token, verify on the NEW token
    methods = [(m, u.rsplit("/v1", 1)[1]) for m, u, _ in client.session.calls]
    assert methods[0][0] == "POST" and methods[0][1].endswith("/rotate-token")
    rotate_auth = json.loads(client.session.calls[0][2]["data"])["grace_seconds"]
    assert rotate_auth == 300
    verify_headers = client.session.calls[1][2]["headers"]
    assert verify_headers["Authorization"] == f"Bearer {NEW_TOKEN}"
    # adopted in memory only after verification
    assert client._token == NEW_TOKEN
    assert client.session.headers["Authorization"] == f"Bearer {NEW_TOKEN}"


def test_rotate_sends_grace_seconds_in_body():
    client = make_client({
        ("POST", "/v1/hosts/h-1/rotate-token"): FakeResponse(200, {"host_token": NEW_TOKEN}),
        ("GET", "/v1/worker/domains"): FakeResponse(200, {"domains": []}),
    })
    client.rotate_host_token("h-1", grace_seconds=120)
    body = json.loads(client.session.calls[0][2]["data"])
    assert body == {"grace_seconds": 120}


def test_rotate_verify_failure_keeps_old_token():
    """If the new token does not authenticate, the client must NOT adopt
    it — the old token is still valid during the grace window, so the
    worker keeps running and can retry."""
    client = make_client({
        ("POST", "/v1/hosts/h-1/rotate-token"): FakeResponse(200, {"host_token": NEW_TOKEN}),
        ("GET", "/v1/worker/domains"): FakeResponse(401, {"error": {"code": "unauthorized"}}),
    })
    with pytest.raises(RotationError):
        client.rotate_host_token("h-1")
    assert client._token == OLD_TOKEN
    assert client.session.headers["Authorization"] == f"Bearer {OLD_TOKEN}"


def test_rotate_server_rejection_raises_without_adopting():
    client = make_client({
        ("POST", "/v1/hosts/h-1/rotate-token"): FakeResponse(403, {"error": {"code": "forbidden"}}),
    })
    with pytest.raises(WorkerAPIError):
        client.rotate_host_token("h-1")
    assert client._token == OLD_TOKEN


def test_rotate_missing_token_in_response_raises():
    client = make_client({
        ("POST", "/v1/hosts/h-1/rotate-token"): FakeResponse(200, {"ok": True}),
    })
    with pytest.raises(RotationError):
        client.rotate_host_token("h-1")
    assert client._token == OLD_TOKEN


# -- persist_host_token -------------------------------------------------------

def test_persist_replaces_existing_token_line(tmp_path):
    cfg = tmp_path / "worker.env"
    cfg.write_text(
        "# worker config\n"
        "WORKER_CONTROL_PLANE_URL=https://cp.example.com\n"
        f"WORKER_HOST_TOKEN={OLD_TOKEN}\n"
        "WORKER_HOST_ID=h-1\n"
    )
    persist_host_token(str(cfg), NEW_TOKEN)
    text = cfg.read_text()
    assert f"WORKER_HOST_TOKEN={NEW_TOKEN}\n" in text
    assert OLD_TOKEN not in text
    # everything else preserved
    assert "WORKER_CONTROL_PLANE_URL=https://cp.example.com\n" in text
    assert "WORKER_HOST_ID=h-1\n" in text
    assert "# worker config\n" in text


def test_persist_appends_when_missing_and_handles_export_form(tmp_path):
    cfg = tmp_path / "worker.env"
    cfg.write_text(f"export WORKER_HOST_TOKEN={OLD_TOKEN}\nWORKER_HOST_ID=h-1\n")
    persist_host_token(str(cfg), NEW_TOKEN)
    text = cfg.read_text()
    assert text.count("WORKER_HOST_TOKEN=") == 1
    assert f"WORKER_HOST_TOKEN={NEW_TOKEN}\n" in text
    assert OLD_TOKEN not in text


def test_persist_is_atomic_and_owner_only(tmp_path):
    cfg = tmp_path / "worker.env"
    cfg.write_text(f"WORKER_HOST_TOKEN={OLD_TOKEN}\n")
    persist_host_token(str(cfg), NEW_TOKEN)
    assert not os.path.exists(str(cfg) + ".tmp-rotate")
    mode = stat.S_IMODE(os.stat(cfg).st_mode)
    assert mode == 0o600


def test_persist_missing_file_raises():
    with pytest.raises(OSError):
        persist_host_token("/nonexistent-dir/worker.env", NEW_TOKEN)


# -- CLI wiring ----------------------------------------------------------------

def test_rotate_token_flag_parses():
    from agent.main import parse_args
    assert parse_args(["--rotate-token"]).rotate_token is True
    assert parse_args([]).rotate_token is False
