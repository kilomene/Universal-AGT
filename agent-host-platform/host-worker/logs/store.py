"""Per-task and per-deployment log files with size caps and rotation.

Layout under <work_dir>/logs/:
    tasks/<task_id>.log
    deployments/<deployment_id>.log

Each file is capped at max_bytes; when appending would exceed the cap the
current file is rotated to .1 (dropping older rotations). tail() reads the
last N characters for progress chunks / results.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MiB per log file
DEFAULT_ROTATIONS = 3
DEFAULT_TAIL_CHARS = 200_000

# task_id / deployment_id come from the control plane; never let them
# become path components. Anything outside [a-zA-Z0-9_-] is stripped.
_SAFE_LOG_ID = re.compile(r"[^a-zA-Z0-9_-]+")


def sanitize_log_id(value: object) -> str:
    """Make a network-derived id safe for use as a log file name."""
    return _SAFE_LOG_ID.sub("", str(value)) or "unknown"


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
