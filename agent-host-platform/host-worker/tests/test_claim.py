"""Claim + progress request builder tests (PROTOCOL §3.3)."""
import pytest

from agent.api import (build_claim_request, build_progress_payload,
                       validate_status_transition)


def _config_kwargs():
    return {
        "base_url": "https://cp.example.com",
        "token": "host-token-abc",
        "host_id": "host-uuid-1",
        "capabilities": ["docker", "docker-compose"],
        "wait": 25,
    }


def test_claim_url_and_method():
    req = build_claim_request(**_config_kwargs())
    assert req["method"] == "POST"
    assert req["url"] == \
        "https://cp.example.com/v1/worker/tasks/claim?wait=25"


def test_claim_auth_header_from_config_token():
    req = build_claim_request(**_config_kwargs())
    assert req["headers"]["Authorization"] == "Bearer host-token-abc"
    assert req["headers"]["Content-Type"] == "application/json"


def test_claim_body_carries_host_id_and_capabilities():
    req = build_claim_request(**_config_kwargs())
    assert req["body"] == {
        "host_id": "host-uuid-1",
        "capabilities": ["docker", "docker-compose"],
    }


def test_claim_wait_clamped_to_protocol_max():
    req = build_claim_request(**{**_config_kwargs(), "wait": 999})
    assert "?wait=30" in req["url"]
    assert req["timeout"] >= 30


def test_claim_timeout_covers_wait_window():
    req = build_claim_request(**{**_config_kwargs(), "wait": 25})
    assert req["timeout"] > 25


def test_claim_base_url_trailing_slash_tolerated():
    kw = _config_kwargs()
    kw["base_url"] = "https://cp.example.com/"
    req = build_claim_request(**kw)
    assert "//v1/" not in req["url"]


# -- progress payload + client-side status machine ---------------------------


def test_progress_claimed_to_running():
    payload = build_progress_payload("claimed", "running",
                                     log_chunk="starting")
    assert payload == {"status": "running", "log_chunk": "starting"}


def test_progress_running_to_completed_with_result():
    payload = build_progress_payload("running", "completed",
                                     result={"ok": True})
    assert payload["status"] == "completed"
    assert payload["result"] == {"ok": True}


def test_progress_running_to_failed_with_error():
    payload = build_progress_payload("running", "failed", error="boom")
    assert payload["error"] == "boom"


def test_progress_self_transition_allowed_for_log_streaming():
    # log_chunk updates stream as running -> running
    payload = build_progress_payload("running", "running", log_chunk="x")
    assert payload["status"] == "running"


def test_progress_awaiting_approval_path():
    payload = build_progress_payload("running", "awaiting_approval")
    assert payload["status"] == "awaiting_approval"
    # awaiting_approval -> queued happens via the approve endpoint, not via
    # worker progress (queued is not a progress-reportable status), so only
    # the transition itself is validated here:
    validate_status_transition("awaiting_approval", "queued")


@pytest.mark.parametrize("current,new", [
    ("queued", "running"),      # must go through claim first
    ("queued", "completed"),
    ("claimed", "completed"),   # must go through running first
    ("completed", "failed"),    # terminal
    ("failed", "running"),      # terminal
    ("cancelled", "claimed"),   # terminal
    ("running", "queued"),
    ("bogus", "running"),       # unknown status
    ("running", "bogus"),
])
def test_illegal_transitions_rejected(current, new):
    with pytest.raises(ValueError):
        validate_status_transition(current, new)


@pytest.mark.parametrize("current,new", [
    ("claimed", "claimed"),
    ("claimed", "running"),
    ("claimed", "failed"),
    ("running", "completed"),
    ("running", "failed"),
])
def test_legal_transitions_accepted(current, new):
    validate_status_transition(current, new)  # must not raise
