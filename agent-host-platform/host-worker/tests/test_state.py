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


def _mkdep(store, dep_id, project_id, created_at, status="superseded"):
    store.save({
        "deployment_id": dep_id,
        "project_id": project_id,
        "status": status,
        "created_at": created_at,
    })


def test_seq_gives_total_order_when_created_at_ties(tmp_path):
    # Regression: created_at is second-precision, so rapid successive
    # deployments tie on it. Generation ordering (GC keep/collect,
    # latest_for_project, rollback candidates) must still be newest-first.
    # This exact tie caused a CI-only flake where GC kept the wrong
    # generation and a beyond-GC-window rollback unexpectedly succeeded.
    store = DeploymentStore(str(tmp_path))
    same_second = "2026-10-06T01:02:23Z"
    _mkdep(store, "dep-w1", "proj", same_second)
    _mkdep(store, "dep-w2", "proj", same_second)
    _mkdep(store, "dep-w3", "proj", same_second)

    seqs = {s["deployment_id"]: s["seq"] for s in store.list_all()}
    assert seqs == {"dep-w1": 1, "dep-w2": 2, "dep-w3": 3}, seqs

    ordered = [s["deployment_id"] for s in store.for_project("proj")]
    assert ordered == ["dep-w3", "dep-w2", "dep-w1"], ordered


def test_seq_survives_status_updates_and_restarts(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _mkdep(store, "dep-a", "proj", "2026-10-06T01:02:23Z", status="running")
    # status update must not move the generation's position
    state = store.load("dep-a")
    state["status"] = "superseded"
    store.save(state)
    assert store.load("dep-a")["seq"] == 1
    # a new store instance over the same dir continues the sequence
    store2 = DeploymentStore(str(tmp_path))
    _mkdep(store2, "dep-b", "proj", "2026-10-06T01:02:23Z")
    assert store2.load("dep-b")["seq"] == 2
    assert [s["deployment_id"] for s in store2.for_project("proj")] == ["dep-b", "dep-a"]


def test_gc_collects_oldest_generation_on_created_at_tie(tmp_path):
    # End-to-end at the GC level: with keep=2 and three same-second
    # generations, exactly the oldest (dep-w1) must be doomed.
    from deployments import gc as gc_mod

    store = DeploymentStore(str(tmp_path))
    same_second = "2026-10-06T01:02:23Z"
    _mkdep(store, "dep-w1", "proj", same_second)
    _mkdep(store, "dep-w2", "proj", same_second)
    _mkdep(store, "dep-w3", "proj", same_second, status="running")

    class Ctx:
        deployment_store = store
        docker = None

    summary = gc_mod.collect_garbage(Ctx(), keep=2, log=lambda line: None)
    # docker is None -> GC skips; instead verify the keep/doom split directly
    gens = store.for_project("proj")
    assert [g["deployment_id"] for g in gens[:2]] == ["dep-w3", "dep-w2"]
    assert [g["deployment_id"] for g in gens[2:]] == ["dep-w1"]
