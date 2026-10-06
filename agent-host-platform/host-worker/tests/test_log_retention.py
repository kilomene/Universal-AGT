"""Spec §34 — bounded log retention.

Per-file caps bound each log's SIZE, but every task leaves its
tasks/<task_id>.log behind: without a sweeper the FILE COUNT grows
without bound on a long-lived host. LogStore.prune() deletes logs older
than the retention window; deployment logs survive only when their
deployment is gone from the store.
"""
import os
import time

import pytest

from deployments.state import DeploymentStore
from logs.store import LogStore, retention_days


@pytest.fixture
def store(tmp_path):
    return LogStore(str(tmp_path / "logs"))


def _write(store, kind, log_id, age_days, with_rotations=False):
    path = (store.task_log_path(log_id) if kind == "task"
            else store.deployment_log_path(log_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 100)
    if with_rotations:
        for i in (1, 2):
            rot = path.with_name(path.name + f".{i}")
            rot.write_text("old rotation")
            # rotations are older than the base by construction
            old = time.time() - (age_days + i) * 86400
            os.utime(rot, (old, old))
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))
    return path


def _exists(store, kind, log_id):
    path = (store.task_log_path(log_id) if kind == "task"
            else store.deployment_log_path(log_id))
    return [p.name for p in store.read_rotations(path)]


def test_old_task_log_and_rotations_pruned(store):
    _write(store, "task", "task-old", age_days=45, with_rotations=True)
    _write(store, "task", "task-recent", age_days=2)
    summary = store.prune(max_age_days=30)
    assert _exists(store, "task", "task-old") == []
    assert _exists(store, "task", "task-recent") == ["task-recent.log"]
    assert "task-old.log" in summary["deleted"]
    # the whole rotation set went together
    assert "task-old.log.1" in summary["deleted"]
    assert "task-old.log.2" in summary["deleted"]
    assert "task-recent.log" in summary["kept"]


def test_old_deployment_log_pruned_when_deployment_gone(tmp_path, store):
    dep_store = DeploymentStore(str(tmp_path / "work"))
    _write(store, "deployment", "dep-gone", age_days=60)
    summary = store.prune(max_age_days=30, deployment_store=dep_store)
    assert _exists(store, "deployment", "dep-gone") == []
    assert "dep-gone.log" in summary["deleted"]


def test_old_deployment_log_kept_when_deployment_known(tmp_path, store):
    dep_store = DeploymentStore(str(tmp_path / "work"))
    dep_store.save({
        "deployment_id": "dep-keep",
        "project_id": "proj-1",
        "status": "superseded",  # old rollback target: log must survive
        "created_at": "2026-01-01T00:00:00Z",
    })
    _write(store, "deployment", "dep-keep", age_days=400)
    summary = store.prune(max_age_days=30, deployment_store=dep_store)
    assert _exists(store, "deployment", "dep-keep") == ["dep-keep.log"]
    assert "dep-keep.log" not in summary["deleted"]


def test_deployment_logs_skipped_without_store(store):
    _write(store, "deployment", "dep-orphan", age_days=400)
    summary = store.prune(max_age_days=30, deployment_store=None)
    # fail closed: active set unknown, so nothing pruned
    assert _exists(store, "deployment", "dep-orphan") == ["dep-orphan.log"]
    assert summary["deleted"] == []


def test_prune_never_raises_on_broken_state(tmp_path, store):
    dep_store = DeploymentStore(str(tmp_path / "work"))
    # corrupt the deployments dir with an unreadable entry is hard as
    # root; instead poison list_all via a broken store double
    class _BrokenStore:
        def list_all(self):
            raise RuntimeError("disk on fire")

    _write(store, "task", "task-old", age_days=45)
    summary = store.prune(max_age_days=30, deployment_store=_BrokenStore())
    # task logs still pruned; the store error is recorded, not raised
    assert _exists(store, "task", "task-old") == []
    assert any("disk on fire" in e for e in summary["errors"])


def test_retention_days_env_override(monkeypatch):
    monkeypatch.setenv("WORKER_LOG_RETENTION_DAYS", "7")
    assert retention_days() == 7
    monkeypatch.setenv("WORKER_LOG_RETENTION_DAYS", "0")
    assert retention_days() == 1  # clamped: never wipe today's logs
    monkeypatch.setenv("WORKER_LOG_RETENTION_DAYS", "not-a-number")
    assert retention_days() == 30  # invalid -> default


def test_retention_days_default(monkeypatch):
    monkeypatch.delenv("WORKER_LOG_RETENTION_DAYS", raising=False)
    assert retention_days() == 30


def test_prune_uses_env_default_when_not_passed(tmp_path, store, monkeypatch):
    monkeypatch.setenv("WORKER_LOG_RETENTION_DAYS", "10")
    _write(store, "task", "task-11d", age_days=11)
    _write(store, "task", "task-9d", age_days=9)
    summary = store.prune()
    assert _exists(store, "task", "task-11d") == []
    assert _exists(store, "task", "task-9d") == ["task-9d.log"]
    assert "task-11d.log" in summary["deleted"]


# ---------------------------------------------------------------------------
# Spec §60 (WS-C verification): a checksum-failed artifact download must
# not leave its partial bytes behind.
# ---------------------------------------------------------------------------
def test_failed_artifact_download_removes_partial(tmp_path):
    import hashlib
    from pathlib import Path as _Path

    from executor import handlers

    apps = tmp_path / "apps"
    apps.mkdir()

    class _Cfg:
        work_dir = str(tmp_path)
        apps_dir = str(apps)

    class _Api:
        def download_artifact(self, artifact_id, dest_path,
                              expected_size=None):
            _Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            _Path(dest_path).write_bytes(b"corrupt-partial-bytes")

    class _Ctx:
        config = _Cfg()
        api = _Api()

        def log(self, task_id, line):
            pass

    good = "sha256:" + hashlib.sha256(b"the real bytes").hexdigest()
    task = {"id": "t1", "type": "artifact-download",
            "payload": {"artifact_id": "art-1", "artifact_checksum": good}}
    with pytest.raises(handlers.HandlerError, match="checksum"):
        handlers.handle_artifact_download(_Ctx(), task)
    assert not (apps / "art-1.bin").exists(), \
        "failed download left a partial artifact behind"
