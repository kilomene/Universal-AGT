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
        assert "manifest" not in body
    finally:
        os.unlink(path)


def test_init_artifact_sends_manifest_when_provided():
    content = b"hello artifact bytes"
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(content)
        path = fh.name
    try:
        client, fake = client_with(
            {"/v1/artifacts/init": lambda: make_response(201, {"artifact": {"id": "a1"}, "upload_url": "/v1/artifacts/a1/content"})}
        )
        manifest = {"name": "p1", "runtime": "docker", "resources": {"cpu": 2, "memory": "1Gi"}}
        client.init_artifact("p1", path, version="1.0.0", manifest=manifest)
        body = fake.calls[0][2]["json"]
        assert body["manifest"] == manifest
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
    # None-valued options are omitted from the wire body (the API rejects
    # explicit nulls for mode/idempotency_key/host_port).
    assert dep_body == {
        "project_id": "p-1",
        "version": "1.0.0",
        "host_id": "h-1",
    }


def test_deploy_helper_missing_project():
    client, fake = client_with({"/v1/projects": lambda: make_response(200, {"projects": []})})
    with pytest.raises(UahtError) as exc:
        client.deploy(project="nope", version="1.0.0")
    assert exc.value.code == "not_found"


def test_add_domain():
    client, fake = client_with(
        {"/v1/domains": lambda: make_response(201, {"domain": {"hostname": "api.example.com", "status": "dns_pending"}})}
    )
    out = client.add_domain("d-1", "api.example.com")
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url == "https://cp.example.com/v1/domains"
    assert kwargs["json"] == {"deployment_id": "d-1", "hostname": "api.example.com"}
    assert out["domain"]["status"] == "dns_pending"


def test_add_domain_with_ingress_tunnel():
    client, fake = client_with(
        {"/v1/domains": lambda: make_response(201, {"domain": {"hostname": "api.example.com", "status": "active", "ingress": "tunnel"}})}
    )
    out = client.add_domain("d-1", "api.example.com", ingress="tunnel")
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert kwargs["json"] == {"deployment_id": "d-1", "hostname": "api.example.com", "ingress": "tunnel"}
    assert out["domain"]["ingress"] == "tunnel"


def test_list_domains():
    client, fake = client_with({"/v1/domains": lambda: make_response(200, {"domains": []})})
    client.list_domains("d-1")
    method, url, kwargs = fake.calls[0]
    assert method == "GET"
    assert url == "https://cp.example.com/v1/domains"
    assert kwargs["params"] == {"deployment_id": "d-1"}


def test_remove_domain():
    client, fake = client_with({"/v1/domains": lambda: make_response(200, {"removed": "api.example.com"})})
    client.remove_domain("d-1", "api.example.com")
    method, url, kwargs = fake.calls[0]
    assert method == "DELETE"
    assert url == "https://cp.example.com/v1/domains"
    assert kwargs["json"] == {"deployment_id": "d-1", "hostname": "api.example.com"}


def test_get_domain():
    client, fake = client_with(
        {"/v1/domains/api.example.com": lambda: make_response(
            200, {"domain": {"hostname": "api.example.com", "status": "active",
                             "dns_configured": True, "tunnel_configured": True,
                             "https_reachable": True}})}
    )
    out = client.get_domain("api.example.com")
    method, url, kwargs = fake.calls[0]
    assert method == "GET"
    assert url == "https://cp.example.com/v1/domains/api.example.com"
    assert out["domain"]["status"] == "active"
    assert out["domain"]["tunnel_configured"] is True


def test_get_domain_encodes_hostname():
    # JS/Python parity: the hostname segment is URL-encoded (mirrors the JS
    # SDK's encodeURIComponent), so exotic-but-legal names don't corrupt the
    # path. A plain DNS name must pass through unchanged.
    client, fake = client_with(
        {"/v1/domains/api.example.com": lambda: make_response(
            200, {"domain": {"hostname": "api.example.com", "status": "active"}})}
    )
    client.get_domain("api.example.com")
    _, url, _ = fake.calls[0]
    assert url == "https://cp.example.com/v1/domains/api.example.com"

    client2, fake2 = client_with(
        {"/v1/domains/idn%20space.example.com": lambda: make_response(
            200, {"domain": {"hostname": "idn space.example.com", "status": "active"}})}
    )
    client2.get_domain("idn space.example.com")
    _, url2, _ = fake2.calls[0]
    assert url2 == "https://cp.example.com/v1/domains/idn%20space.example.com"


def test_get_logs_creates_task_and_polls_to_terminal():
    states = [
        {"task": {"id": "t-9", "status": "running", "result": {"logs": "line1\n"}}},
        {"task": {"id": "t-9", "status": "completed", "result": {"logs": "line1\nline2\n"}}},
    ]
    idx = [0]

    def fake_request(self, method, url, **kwargs):
        fake_request.calls.append((method, url, kwargs))
        if url.endswith("/v1/tasks") and method == "POST":
            return make_response(201, {"task": {"id": "t-9", "status": "queued"}})
        if url.endswith("/v1/tasks/t-9"):
            resp = make_response(200, states[idx[0]])
            idx[0] = min(idx[0] + 1, len(states) - 1)
            return resp
        raise AssertionError(f"unexpected: {method} {url}")

    fake_request.calls = []
    session = MagicMock()
    session.request = fake_request.__get__(session, type(session))
    client = UahtClient("https://cp.example.com", "k", session=session)

    logs = client.get_logs(deployment_id="d-1", poll_interval=0.001)
    assert logs == "line1\nline2\n"
    method, url, kwargs = fake_request.calls[0]
    assert method == "POST" and url.endswith("/v1/tasks")
    # None-valued options are omitted from the wire body.
    assert kwargs["json"] == {"type": "logs", "payload": {"deployment_id": "d-1"}}


def test_get_logs_polls_existing_task_id():
    client, fake = client_with(
        {"/v1/tasks/t-7": lambda: make_response(200, {"task": {"id": "t-7", "status": "completed",
                                                              "result": {"logs": "boot ok"}}})}
    )
    logs = client.get_logs(task_id="t-7", poll_interval=0.001)
    assert logs == "boot ok"
    assert fake.calls[0][1].endswith("/v1/tasks/t-7")


def test_get_logs_requires_an_id():
    client, _ = client_with({})
    with pytest.raises(TypeError):
        client.get_logs()


def test_tail_logs_yields_incremental_chunks():
    states = [
        {"task": {"id": "t-8", "status": "running", "result": {"logs": "a"}}},
        {"task": {"id": "t-8", "status": "running", "result": {"logs": "ab"}}},
        {"task": {"id": "t-8", "status": "completed", "result": {"logs": "abc"}}},
    ]
    idx = [0]

    def fake_request(self, method, url, **kwargs):
        if url.endswith("/v1/tasks/t-8"):
            resp = make_response(200, states[idx[0]])
            idx[0] = min(idx[0] + 1, len(states) - 1)
            return resp
        raise AssertionError(f"unexpected: {method} {url}")

    session = MagicMock()
    session.request = fake_request.__get__(session, type(session))
    client = UahtClient("https://cp.example.com", "k", session=session)

    chunks = list(client.get_logs(task_id="t-8", follow=True, poll_interval=0.001))
    assert chunks == ["a", "b", "c"]


def test_get_logs_timeout():
    client, fake = client_with(
        {"/v1/tasks/t-6": lambda: make_response(200, {"task": {"id": "t-6", "status": "running", "result": {}}})}
    )
    with pytest.raises(UahtError) as exc:
        client.get_logs(task_id="t-6", poll_interval=0.001, timeout=0.01)
    assert exc.value.code == "timeout"


def test_deploy_helper_handles_bare_list_projects():
    # list_projects returning a bare list (not {"projects": [...]}) must not
    # raise AttributeError.
    client, fake = client_with(
        {
            "/v1/projects": lambda: make_response(200, [{"id": "p-1", "name": "bare-api"}]),
            "/v1/deployments": lambda: make_response(
                201, {"deployment": {"id": "d-1"}, "task": {"id": "t-1", "status": "queued"}}
            ),
        }
    )
    deployment, task = client.deploy(project="bare-api", version="1.0.0")
    assert deployment["id"] == "d-1"
    assert task["id"] == "t-1"


# -- JS/Python parity (Phase 8) -----------------------------------------

# Canonical agent-facing method table. The JS SDK must expose the camelCase
# twin of every entry; the parity test in client.test.js asserts the same
# table. Add new API methods to BOTH clients and extend this table.
PARITY_METHODS = [
    "register_agent", "me", "rotate_agent_key",
    "create_task", "get_task", "list_tasks", "cancel_task", "approve_task", "reject_task",
    "get_logs", "tail_logs",
    "list_hosts", "get_host", "register_host", "rotate_host_token",
    "create_project", "list_projects", "get_project", "update_project",
    "list_artifacts", "get_artifact", "init_artifact", "upload_artifact", "download_artifact",
    "create_deployment", "get_deployment", "list_deployments", "rollback_deployment",
    "list_services", "restart_service", "stop_service", "start_service",
    "set_secret", "list_secrets", "delete_secret",
    "add_domain", "list_domains", "get_domain", "remove_domain",
    "list_events", "stream_events",
    "deploy", "health",
]


def test_parity_method_table():
    client = UahtClient("https://cp.example.com", "k", session=MagicMock())
    missing = [m for m in PARITY_METHODS if not callable(getattr(client, m, None))]
    assert not missing, f"methods missing from the Python SDK: {missing}"


def test_rotate_agent_key_posts_and_adopts_new_key():
    client, fake = client_with(
        {"/v1/agents/me/rotate": lambda: make_response(200, {"api_key": "new-key"})}
    )
    res = client.rotate_agent_key()
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url == "https://cp.example.com/v1/agents/me/rotate"
    assert kwargs["headers"]["Authorization"] == "Bearer sekret"
    assert res == {"api_key": "new-key"}
    # the server invalidates the old key immediately, so the client must
    # adopt the new one or every later call breaks
    assert client.api_key == "new-key"


def test_rotate_agent_key_error_keeps_old_key():
    client, fake = client_with(
        {"/v1/agents/me/rotate": lambda: make_response(403, {"error": {"code": "forbidden", "message": "missing or invalid bearer token"}})}
    )
    with pytest.raises(UahtError) as exc:
        client.rotate_agent_key()
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403
    # a failed rotation must not clobber the working key
    assert client.api_key == "sekret"


def test_permission_error_surfaces_cleanly():
    client, fake = client_with(
        {"/v1/deployments": lambda: make_response(403, {"error": {"code": "forbidden", "message": "missing permission: deploy"}})}
    )
    with pytest.raises(UahtError) as exc:
        client.create_deployment("p1", "v1")
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403
    assert exc.value.message == "missing permission: deploy"


# -- W15 contract conformance (2026-10-05) -------------------------------
# The wire protocol is authoritative: SDK method signatures must expose
# exactly the params the server implements (no dead params the server
# ignores), and every stable capability must exist in both SDKs.

def test_create_deployment_sends_host_port():
    client, fake = client_with({"/v1/deployments": lambda: make_response(201, {"deployment": {"id": "d1"}, "task": {}})})
    client.create_deployment(project_id="p1", version="1.0.0", host_id="h1", host_port=8080)
    _, url, kwargs = fake.calls[0]
    assert url.endswith("/v1/deployments")
    assert kwargs["json"]["host_port"] == 8080
    assert kwargs["json"]["host_id"] == "h1"


def test_update_project_sends_repository_and_runtime():
    client, fake = client_with({"/v1/projects/p1": lambda: make_response(200, {"project": {}})})
    client.update_project("p1", {"env": {"A": "1"}}, repository="https://example.com/r.git", runtime="docker")
    method, url, kwargs = fake.calls[0]
    assert method == "PUT"
    assert url.endswith("/v1/projects/p1")
    assert kwargs["json"] == {
        "configuration": {"env": {"A": "1"}},
        "repository": "https://example.com/r.git",
        "runtime": "docker",
    }


def test_update_project_omits_unset_fields():
    client, fake = client_with({"/v1/projects/p1": lambda: make_response(200, {"project": {}})})
    client.update_project("p1", {"env": {}})
    _, _, kwargs = fake.calls[0]
    assert kwargs["json"] == {"configuration": {"env": {}}}


def test_list_projects_sends_no_dead_params():
    # GET /v1/projects has no limit/cursor server-side; the SDK must not
    # send params the wire ignores.
    client, fake = client_with({"/v1/projects": lambda: make_response(200, {"projects": []})})
    client.list_projects()
    _, url, kwargs = fake.calls[0]
    assert url.endswith("/v1/projects")
    # no params at all on the wire: the endpoint takes none
    assert kwargs.get("params") in (None, {})


def test_list_artifacts_sends_only_project_id():
    client, fake = client_with({"/v1/artifacts": lambda: make_response(200, {"artifacts": []})})
    client.list_artifacts(project_id="p1")
    _, _, kwargs = fake.calls[0]
    assert kwargs["params"] == {"project_id": "p1"}


def test_list_deployments_sends_no_cursor():
    client, fake = client_with({"/v1/deployments": lambda: make_response(200, {"deployments": []})})
    client.list_deployments(project_id="p1", status="running", limit=10)
    _, _, kwargs = fake.calls[0]
    assert kwargs["params"] == {"project_id": "p1", "status": "running", "limit": 10}


def test_list_services_supports_limit():
    client, fake = client_with({"/v1/services": lambda: make_response(200, {"services": []})})
    client.list_services(host_id="h1", limit=5)
    _, _, kwargs = fake.calls[0]
    assert kwargs["params"] == {"host_id": "h1", "limit": 5}


def test_deploy_helper_passes_host_port():
    client, fake = client_with(
        {
            "/v1/projects": lambda: make_response(200, {"projects": [{"id": "p-1", "name": "web"}]}),
            "/v1/deployments": lambda: make_response(
                201, {"deployment": {"id": "d-1"}, "task": {"id": "t-1", "status": "queued"}}
            ),
        }
    )
    deployment, task = client.deploy(project="web", version="1.0.0", host_port=9090)
    deploy_call = [c for c in fake.calls if c[1].endswith("/v1/deployments")][0]
    assert deploy_call[2]["json"]["host_port"] == 9090
    assert deployment["id"] == "d-1"


# -- registration & host token rotation ----------------------------------


def test_register_agent_sends_provisioning_token_header():
    session = MagicMock()
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return make_response(201, {"agent": {"id": "a1"}, "api_key": "fresh"})

    session.request = fake_request.__get__(session, type(session))
    # no agent key exists before registration: api_key may be omitted
    client = UahtClient("https://cp.example.com", session=session)
    res = client.register_agent(name="ci-bot", type="ci", provisioning_token="prov-123")
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://cp.example.com/v1/agents/register"
    assert kwargs["headers"]["X-Provisioning-Token"] == "prov-123"
    assert "Authorization" not in kwargs["headers"]
    assert res["api_key"] == "fresh"


def test_register_host_posts_with_provisioning_header():
    client, fake = client_with(
        {"/v1/hosts/register": lambda: make_response(201, {"host": {"id": "h1"}, "host_token": "uagh_fresh"})}
    )
    res = client.register_host(name="edge-01", provisioning_token="prov-123")
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url == "https://cp.example.com/v1/hosts/register"
    assert kwargs["headers"]["X-Provisioning-Token"] == "prov-123"
    assert res["host_token"] == "uagh_fresh"


def test_rotate_host_token_posts_grace_and_adopts_new_token():
    client, fake = client_with(
        {"/v1/hosts/h1/rotate-token": lambda: make_response(200, {"host_token": "uagh_new"})}
    )
    # synthetic fixture, not a real credential: the _not_real suffix keeps
    # the secrets sweep from flagging it as a leaked api_key value.
    client.api_key = "uagh_old_not_real"
    res = client.rotate_host_token("h1", grace_seconds=300)
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url == "https://cp.example.com/v1/hosts/h1/rotate-token"
    assert kwargs["headers"]["Authorization"] == "Bearer uagh_old_not_real"
    assert kwargs["json"] == {"grace_seconds": 300}
    assert res["host_token"] == "uagh_new"
    # the client must adopt the new token or later host calls break
    assert client.api_key == "uagh_new"


def test_version_coherence():
    """§61: one version source — __init__.__version__ == client.__version__
    == pyproject version, and the default User-Agent derives from it."""
    import re
    from pathlib import Path
    import uaht_sdk
    from uaht_sdk import client as client_mod

    assert uaht_sdk.__version__ == client_mod.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", uaht_sdk.__version__)

    pyproject = Path(uaht_sdk.__file__).parents[2] / "pyproject.toml"
    m = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
    assert m, "pyproject.toml has no version"
    assert m.group(1) == uaht_sdk.__version__

    c = uaht_sdk.UahtClient("https://cp.example.com/", "sekret")
    assert c.user_agent == f"uaht-sdk/{uaht_sdk.__version__}"
