"""Contract tests for uaht_sdk: mock requests.Session.request and assert
URLs, methods, headers, idempotency_key passthrough, and protocol
error-shape parsing."""

import hashlib
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from uaht_sdk import UahtClient, UahtError
from uaht_sdk.sse import SseParser


def make_response(status=200, body=None, raw_bytes=None):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.text = json.dumps(body) if body is not None else ""
    resp.json = (lambda: body) if body is not None else MagicMock(side_effect=ValueError("no json"))
    if raw_bytes is not None:
        resp.iter_content = lambda chunk_size=1: (raw_bytes[i : i + chunk_size] for i in range(0, len(raw_bytes), chunk_size))
        resp.iter_lines = lambda decode_unicode=True: iter(raw_bytes.decode().splitlines())
    else:
        resp.iter_lines = lambda decode_unicode=True: iter([])
        resp.iter_content = lambda chunk_size=1: iter([])
    return resp


def client_with(mapping):
    """mapping: url_suffix -> response factory; records every call."""

    def fake_request(self, method, url, **kwargs):
        fake_request.calls.append((method, url, kwargs))
        for suffix, factory in mapping.items():
            if url.endswith(suffix):
                return factory()
        raise AssertionError(f"unexpected request: {method} {url}")

    fake_request.calls = []
    session = MagicMock()
    session.request = fake_request.__get__(session, type(session))
    return UahtClient("https://cp.example.com/", "sekret", session=session), fake_request


def test_bearer_auth_and_user_agent():
    client, fake = client_with({"/v1/agents/me": lambda: make_response(200, {"agent": {"name": "a"}})})
    client.me()
    method, url, kwargs = fake.calls[0]
    assert method == "GET"
    assert url == "https://cp.example.com/v1/agents/me"
    assert kwargs["headers"]["Authorization"] == "Bearer sekret"
    assert kwargs["headers"]["User-Agent"].startswith("uaht-sdk/")


def test_query_params_drop_none():
    client, fake = client_with({"/v1/tasks": lambda: make_response(200, {"tasks": []})})
    client.list_tasks(status="running", type=None, limit=10, cursor=None)
    _, url, kwargs = fake.calls[0]
    assert url == "https://cp.example.com/v1/tasks"
    assert kwargs["params"] == {"status": "running", "limit": 10}


def test_error_shape_parsed():
    client, fake = client_with(
        {"/v1/deployments/d1": lambda: make_response(403, {"error": {"code": "forbidden", "message": "missing permission: deploy"}})}
    )
    with pytest.raises(UahtError) as exc:
        client.get_deployment("d1")
    assert exc.value.code == "forbidden"
    assert exc.value.message == "missing permission: deploy"
    assert exc.value.status == 403


def test_connection_error_wrapped():
    session = MagicMock()
    session.request.side_effect = __import__("requests").RequestException("boom")
    client = UahtClient("https://cp.example.com", "k", session=session)
    with pytest.raises(UahtError) as exc:
        client.health()
    assert exc.value.code == "connection_error"


def test_create_task_idempotency_passthrough():
    client, fake = client_with({"/v1/tasks": lambda: make_response(201, {"task": {"id": "t1"}})})
    client.create_task(type="restart", payload={"deployment_id": "d1"}, idempotency_key="idem-123", priority=5)
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    body = kwargs["json"]
    assert body["type"] == "restart"
    assert body["payload"] == {"deployment_id": "d1"}
    assert body["idempotency_key"] == "idem-123"
    assert body["priority"] == 5


def test_task_transitions_hit_right_endpoints():
    client, fake = client_with(
        {
            "/v1/tasks/t1/cancel": lambda: make_response(200, {"task": {}}),
            "/v1/tasks/t2/approve": lambda: make_response(200, {"task": {}}),
            "/v1/tasks/t3/reject": lambda: make_response(200, {"task": {}}),
        }
    )
    client.cancel_task("t1")
    client.approve_task("t2")
    client.reject_task("t3")
    assert fake.calls[0][0] == "POST" and fake.calls[0][1].endswith("/v1/tasks/t1/cancel")
    assert fake.calls[1][1].endswith("/v1/tasks/t2/approve")
    assert fake.calls[2][1].endswith("/v1/tasks/t3/reject")


def test_create_deployment_idempotency_and_mode():
    client, fake = client_with({"/v1/deployments": lambda: make_response(201, {"deployment": {"id": "d1"}, "task": {}})})
    client.create_deployment(project_id="p1", version="1.0.0", mode="manual", idempotency_key="idem-9")
    _, _, kwargs = fake.calls[0]
    assert kwargs["json"]["mode"] == "manual"
    assert kwargs["json"]["idempotency_key"] == "idem-9"


def test_service_endpoints():
    client, fake = client_with(
        {
            "/v1/services/s1/restart": lambda: make_response(200, {}),
            "/v1/services/s2/stop": lambda: make_response(200, {}),
            "/v1/services/s3/start": lambda: make_response(200, {}),
        }
    )
    client.restart_service("s1")
    client.stop_service("s2")
    client.start_service("s3")
    assert fake.calls[0][1].endswith("/v1/services/s1/restart")
    assert fake.calls[1][1].endswith("/v1/services/s2/stop")
    assert fake.calls[2][1].endswith("/v1/services/s3/start")


def test_secret_endpoints():
    client, fake = client_with(
        {
            "/v1/projects/p1/secrets": lambda: make_response(200, {}),
            "/v1/projects/p1/secrets/DB%2FPASS": lambda: make_response(200, {}),
        }
    )
    client.set_secret("p1", "DB_PASS", "x")
    client.list_secrets("p1")
    client.delete_secret("p1", "DB/PASS")
    assert fake.calls[0][1].endswith("/v1/projects/p1/secrets")
    assert fake.calls[0][0] == "POST"
    assert fake.calls[0][2]["json"] == {"name": "DB_PASS", "value": "x"}
    assert fake.calls[1][0] == "GET"
    assert fake.calls[2][1].endswith("/v1/projects/p1/secrets/DB%2FPASS")


def test_init_artifact_computes_sha256():
    content = b"hello artifact bytes"
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(content)
        path = fh.name
    try:
        client, fake = client_with(
            {"/v1/artifacts/init": lambda: make_response(201, {"artifact": {"id": "a1"}, "upload_url": "/v1/artifacts/a1/content"})}
        )
        client.init_artifact("p1", path, version="1.0.0", filename="app.tar.gz")
        body = fake.calls[0][2]["json"]
        assert body["checksum"] == "sha256:" + hashlib.sha256(content).hexdigest()
        assert body["size"] == len(content)
        assert body["filename"] == "app.tar.gz"
        assert body["version"] == "1.0.0"
    finally:
        os.unlink(path)


def test_upload_artifact_octet_stream():
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b"bytes")
        path = fh.name
    try:
        client, fake = client_with({"/v1/artifacts/a1/content": lambda: make_response(200, {})})
        assert client.upload_artifact("/v1/artifacts/a1/content", path) is True
        method, url, kwargs = fake.calls[0]
        assert method == "PUT"
        assert url == "https://cp.example.com/v1/artifacts/a1/content"
        assert kwargs["headers"]["Content-Type"] == "application/octet-stream"
    finally:
        os.unlink(path)


def test_download_artifact_streams_to_file():
    payload = b"artifact-bytes-123"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.bin")
        client, fake = client_with(
            {"/v1/artifacts/a1/download": lambda: make_response(200, raw_bytes=payload)}
        )
        assert client.download_artifact("a1", out) == out
        assert open(out, "rb").read() == payload


def test_sse_parser_frames_and_heartbeat():
    parser = SseParser()
    events = []
    events += parser.feed('data: {"id":1,"type":"task.created"}\n\n')
    events += parser.feed(": heartbeat\n\n")  # SSE comment: no event
    events += parser.feed('data: {"id":2,"type":"deployment.completed"}\n')
    events += parser.feed("\n")  # frame terminator split across chunks
    assert events == [
        {"id": 1, "type": "task.created"},
        {"id": 2, "type": "deployment.completed"},
    ]


def test_deploy_helper_resolves_names_and_waits():
    states = [
        {"task": {"id": "t-1", "status": "running"}},
        {"task": {"id": "t-1", "status": "completed"}},
    ]
    calls = []
    state_idx = [0]

    mapping = {
        "/v1/projects": lambda: make_response(200, {"projects": [{"id": "p-1", "name": "my-api"}]}),
        "/v1/hosts": lambda: make_response(200, {"hosts": [{"id": "h-1", "name": "host-01"}]}),
        "/v1/deployments": lambda: make_response(201, {"deployment": {"id": "d-1"}, "task": {"id": "t-1", "status": "queued"}}),
        "/v1/deployments/d-1": lambda: make_response(200, {"deployment": {"id": "d-1", "status": "running"}}),
    }

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/tasks/t-1"):
            resp = make_response(200, states[state_idx[0]])
            state_idx[0] = min(state_idx[0] + 1, len(states) - 1)
            return resp
        for suffix, factory in mapping.items():
            if url.endswith(suffix):
                return factory()
        raise AssertionError(f"unexpected: {method} {url}")

    session = MagicMock()
    session.request = fake_request.__get__(session, type(session))
    client = UahtClient("https://cp.example.com", "k", session=session)

    deployment, task = client.deploy(project="my-api", version="1.0.0", host="host-01", wait=True, poll_interval=0.001)
    assert task["status"] == "completed"
    assert deployment["status"] == "running"
    dep_body = next(c[2]["json"] for c in calls if c[1].endswith("/v1/deployments") and c[0] == "POST")
    assert dep_body == {
        "project_id": "p-1",
        "version": "1.0.0",
        "host_id": "h-1",
        "artifact_id": None,
        "mode": None,
        "idempotency_key": None,
    }


def test_deploy_helper_missing_project():
    client, fake = client_with({"/v1/projects": lambda: make_response(200, {"projects": []})})
    with pytest.raises(UahtError) as exc:
        client.deploy(project="nope", version="1.0.0")
    assert exc.value.code == "not_found"
