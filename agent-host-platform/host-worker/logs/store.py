"""Per-task and per-deployment log files with size caps and rotation.

Layout under <work_dir>/logs/:
    tasks/<task_id>.log
    deployments/<deployment_id>.log

Each file is capped at max_bytes; when appending would exceed the cap the
current file is rotated to .1 (dropping older rotations). tail() reads the
last N characters for progress chunks / results.

Bounded retention (spec §34): per-file caps bound each file's SIZE, but
the FILE COUNT grows without bound — every task leaves its
tasks/<task_id>.log behind. LogStore.prune() deletes log files older than
WORKER_LOG_RETENTION_DAYS (default 30); deployment logs are only pruned
when their deployment is gone from the deployment store, so logs of known
deployments (active or rollback targets) are kept regardless of age.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MiB per log file
DEFAULT_ROTATIONS = 3
DEFAULT_TAIL_CHARS = 200_000

# Task logs older than this are pruned (env-overridable). Deployment logs
# follow the same window but only for deployments no longer in the store.
DEFAULT_RETENTION_DAYS = 30
RETENTION_ENV_VAR = "WORKER_LOG_RETENTION_DAYS"

# task_id / deployment_id come from the control plane; never let them
# become path components. Anything outside [a-zA-Z0-9_-] is stripped.
_SAFE_LOG_ID = re.compile(r"[^a-zA-Z0-9_-]+")


def sanitize_log_id(value: object) -> str:
    """Make a network-derived id safe for use as a log file name."""
    return _SAFE_LOG_ID.sub("", str(value)) or "unknown"


def retention_days() -> int:
    """Log retention window in days. Always >= 1 so a misconfigured env
    can never wipe today's logs."""
    try:
        return max(1, int(os.environ.get(RETENTION_ENV_VAR,
                                         DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


class LogStore:
    def __init__(self, base_dir: str,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 rotations: int = DEFAULT_ROTATIONS):
        self.base_dir = Path(base_dir)
        self.tasks_dir = self.base_dir / "tasks"
        self.deployments_dir = self.base_dir / "deployments"
        self.max_bytes = max_bytes
        self.rotations = rotations
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.deployments_dir.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------------
    def task_log_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{sanitize_log_id(task_id)}.log"

    def deployment_log_path(self, deployment_id: str) -> Path:
        return self.deployments_dir / f"{sanitize_log_id(deployment_id)}.log"

    # -- writing ---------------------------------------------------------
    def _rotate(self, path: Path) -> None:
        oldest = path.with_name(path.name + f".{self.rotations}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self.rotations - 1, 0, -1):
            src = path.with_name(path.name + f".{i}")
            if src.exists():
                src.rename(path.with_name(path.name + f".{i + 1}"))
        if path.exists():
            path.rename(path.with_name(path.name + ".1"))

    def append(self, path: Path, text: str) -> None:
        data = text if text.endswith("\n") else text + "\n"
        encoded = data.encode("utf-8", errors="replace")
        try:
            if path.exists() and path.stat().st_size + len(encoded) > self.max_bytes:
                self._rotate(path)
        except OSError:
            pass
        with open(path, "ab") as fh:
            fh.write(encoded)

    def append_task(self, task_id: str, text: str) -> None:
        self.append(self.task_log_path(task_id), text)

    def append_deployment(self, deployment_id: str, text: str) -> None:
        self.append(self.deployment_log_path(deployment_id), text)

    # -- reading ---------------------------------------------------------
    def tail(self, path: Path, max_chars: int = DEFAULT_TAIL_CHARS) -> str:
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        read_from = max(0, size - max_chars * 4)
        with open(path, "rb") as fh:
            fh.seek(read_from)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[-max_chars:]
        # avoid starting mid-line when we seeked into the middle
        if read_from > 0:
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]
        return text

    def tail_task(self, task_id: str, max_chars: int = DEFAULT_TAIL_CHARS) -> str:
        return self.tail(self.task_log_path(task_id), max_chars)

    def tail_deployment(self, deployment_id: str,
                        max_chars: int = DEFAULT_TAIL_CHARS) -> str:
        return self.tail(self.deployment_log_path(deployment_id), max_chars)

    def read_rotations(self, path: Path) -> list:
        """All log files for a given base path, newest last."""
        found = []
        if path.exists():
            found.append(path)
        for i in range(1, self.rotations + 1):
            rot = path.with_name(path.name + f".{i}")
            if rot.exists():
                found.append(rot)
        return found

    # -- retention -------------------------------------------------------
    def prune(self, max_age_days: int | None = None,
              deployment_store=None, log=None) -> dict:
        """Delete log files older than the retention window (spec §34).

        Task logs: deleted when older than ``max_age_days`` — a task's log
        is only useful while the task is recent; the control plane already
        received the log chunks via progress reports.

        Deployment logs: deleted only when older than the window AND the
        deployment is gone from ``deployment_store``. Logs of known
        deployments (running, stopped, or kept as rollback targets) are
        never pruned by age. When ``deployment_store`` is None, deployment
        logs are skipped entirely (active set unknown — fail closed).

        Rotation sets are deleted as a unit: the base .log's mtime decides
        (rotations are always older than their base by construction).

        Returns {"deleted": [...], "kept": [...], "errors": [...]}; never
        raises — a prune failure must not break the worker loop.
        """
        if max_age_days is None:
            max_age_days = retention_days()
        max_age_days = max(1, max_age_days)
        cutoff = time.time() - max_age_days * 86400
        summary = {"deleted": [], "kept": [], "errors": []}

        def _note(msg: str) -> None:
            if log:
                log(msg)

        known_deployments: set | None = None
        if deployment_store is not None:
            try:
                known_deployments = {
                    sanitize_log_id(s.get("deployment_id"))
                    for s in deployment_store.list_all()
                    if s.get("deployment_id")
                }
            except Exception as exc:
                summary["errors"].append(f"deployment store unreadable: {exc}")
                known_deployments = None

        for base in sorted(self.tasks_dir.glob("*.log")):
            self._prune_one(base, cutoff, summary, _note)
        if known_deployments is None:
            _note("prune: deployment logs skipped (no deployment store)")
        else:
            for base in sorted(self.deployments_dir.glob("*.log")):
                if base.stem in known_deployments:
                    continue  # known deployment: keep regardless of age
                self._prune_one(base, cutoff, summary, _note)
        return summary

    def _prune_one(self, base: Path, cutoff: float, summary: dict,
                   note) -> None:
        """Delete one log's rotation set when its base mtime is past cutoff."""
        try:
            mtime = base.stat().st_mtime
        except OSError as exc:
            summary["errors"].append(f"{base.name}: {exc}")
            return
        if mtime >= cutoff:
            summary["kept"].append(base.name)
            return
        for path in self.read_rotations(base):
            try:
                path.unlink()
                summary["deleted"].append(path.name)
            except OSError as exc:
                summary["errors"].append(f"{path.name}: {exc}")
        note(f"prune: deleted {base.name} (+rotations; "
             f"older than retention window)")
