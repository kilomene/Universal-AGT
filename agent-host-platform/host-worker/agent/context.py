"""Shared per-worker context passed to every task handler.

WorkerContext bundles config, API client, docker client, log store and the
local deployment registry, plus a per-task log() helper that scrubs secret
values before anything hits disk.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


def _identity(text: str) -> str:
    return text


@dataclass
class WorkerContext:
    config: object          # agent.config.WorkerConfig (duck-typed to avoid import cycle)
    api: object             # agent.api.ControlPlaneClient
    docker: object          # docker.client.DockerClient or None
    log_store: object       # logs.store.LogStore
    deployment_store: object  # deployments.state.DeploymentStore
    scrub: Callable[[str], str] = field(default=_identity)

    def log(self, task_id: str, line: str) -> str:
        """Timestamped, secret-scrubbed append to the task log. Returns the line."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        clean = self.scrub(f"[{ts}] {line}")
        try:
            self.log_store.append_task(task_id, clean)
        except OSError:
            pass
        return clean

    def require_docker(self):
        if self.docker is None:
            from docker.client import DockerMissing
            raise DockerMissing(
                "docker is not available on this host; "
                "container task types cannot run"
            )
        return self.docker
