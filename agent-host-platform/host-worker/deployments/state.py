"""Local deployment state registry.

Each deployment gets a directory:
    <work_dir>/deployments/<deployment_id>/state.json

The registry is the worker's local view of what it runs; the control
plane database remains the source of truth. Env values that came from
task secrets are NEVER persisted here — only non-secret env is stored.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


class DeploymentStore:
    def __init__(self, work_dir: str):
        self.root = Path(work_dir) / "deployments"
        self.root.mkdir(parents=True, exist_ok=True)

    def _state_path(self, deployment_id: str) -> Path:
        return self.root / deployment_id / "state.json"

    def save(self, state: dict) -> Path:
        deployment_id = state["deployment_id"]
        path = self._state_path(deployment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state.setdefault("updated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path

    def load(self, deployment_id: str) -> Optional[dict]:
        path = self._state_path(deployment_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def list_all(self) -> list:
        states = []
        if not self.root.exists():
            return states
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            state = self.load(child.name)
            if state:
                states.append(state)
        states.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return states

    def latest_for_project(self, project_id: str,
                           statuses: tuple = ("running",)) -> Optional[dict]:
        """Newest deployment of a project in one of the given statuses."""
        candidates = [
            s for s in self.list_all()
            if s.get("project_id") == project_id and s.get("status") in statuses
        ]
        return candidates[0] if candidates else None

    def used_host_ports(self) -> set:
        ports = set()
        for state in self.list_all():
            if state.get("status") in ("running", "starting", "healthcheck"):
                port = state.get("host_port")
                if port:
                    ports.add(int(port))
        return ports

    def deployment_dir(self, deployment_id: str) -> Path:
        d = self.root / deployment_id
        d.mkdir(parents=True, exist_ok=True)
        return d
