"""Tests for deployments.state quarantine and logs.store id sanitization."""
import json
import re

from deployments.state import DeploymentStore
from logs.store import LogStore, sanitize_log_id


def _write(store, deployment_id, payload):
    d = store.deployment_dir(deployment_id)
    with open(d / "state.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_corrupt_state_is_quarantined_not_fatal(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _write(store, "dep-ok", {"deployment_id": "dep-ok", "status": "running"})
    bad_dir = store.deployment_dir("dep-bad")
    (bad_dir / "state.json").write_text("{ this is not json", encoding="utf-8")

    assert store.load("dep-bad") is None  # must not raise

    remaining = list(bad_dir.iterdir())
    assert len(remaining) == 1
    assert re.fullmatch(r"state\.json\.corrupt-\d{8}T\d{6}Z", remaining[0].name), \
        remaining[0].name
    # the quarantined file still holds the original bytes for forensics
    assert remaining[0].read_text(encoding="utf-8") == "{ this is not json"

    # good deployments are unaffected
    assert store.load("dep-ok")["status"] == "running"
    assert [s["deployment_id"] for s in store.list_all()] == ["dep-ok"]


def test_empty_state_file_is_quarantined(tmp_path):
    store = DeploymentStore(str(tmp_path))
    bad_dir = store.deployment_dir("dep-empty")
    (bad_dir / "state.json").write_text("", encoding="utf-8")
    assert store.load("dep-empty") is None
    assert not (bad_dir / "state.json").exists()


def test_missing_state_still_returns_none(tmp_path):
    store = DeploymentStore(str(tmp_path))
    assert store.load("dep-nope") is None


def test_save_after_quarantine_recovers(tmp_path):
    store = DeploymentStore(str(tmp_path))
    bad_dir = store.deployment_dir("dep-bad")
    (bad_dir / "state.json").write_text("garbage", encoding="utf-8")
    assert store.load("dep-bad") is None
    store.save({"deployment_id": "dep-bad", "status": "running"})
    assert store.load("dep-bad")["status"] == "running"


# ---------------------------------------------------------------------------
# sanitize_log_id
# ---------------------------------------------------------------------------

def test_sanitize_log_id_keeps_safe_ids():
    assert sanitize_log_id("abc-123_XYZ") == "abc-123_XYZ"
    # plain UUIDs (the normal case) pass through untouched
    uid = "123e4567-e89b-12d3-a456-426614174000"
    assert sanitize_log_id(uid) == uid


def test_sanitize_log_id_strips_traversal():
    assert sanitize_log_id("../../../etc/passwd") == "etcpasswd"
    assert sanitize_log_id("a/b\\c:d.e f") == "abcdef"


def test_sanitize_log_id_empty_becomes_unknown():
    assert sanitize_log_id("") == "unknown"
    assert sanitize_log_id("///") == "unknown"


def test_task_log_path_cannot_escape(tmp_path):
    store = LogStore(str(tmp_path / "logs"))
    path = store.task_log_path("../../../../etc/cron.d/evil")
    assert path.resolve().parent == store.tasks_dir.resolve()
    assert path.name == "etccrondevil.log"


def test_deployment_log_path_cannot_escape(tmp_path):
    store = LogStore(str(tmp_path / "logs"))
    path = store.deployment_log_path("x/../../y")
    assert path.resolve().parent == store.deployments_dir.resolve()
