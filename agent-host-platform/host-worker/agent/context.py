"""Shared per-worker context passed to every task handler.

WorkerContext bundles config, API client, docker client, log store and the
local deployment registry, plus a per-task log() helper that scrubs secret
values before anything hits disk.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator


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
    ingress: object = field(default=None)  # ingress.IngressProvider or None
    # Server-advertised drain state (§37): set when a heartbeat response
    # carries host.status == "draining" (operator-set at the control plane,
    # never auto-flipped server-side); cleared when the operator clears it.
    # While set, the claim loop pauses NEW claims, heartbeats continue, and
    # in-flight dispatches finish. A locally configured WORKER_DRAINING=true
    # (config.draining) is sticky and independent of this latch.
    draining: threading.Event = field(default_factory=threading.Event,
                                      repr=False, compare=False)
    # Thread-local scrubber override. The production claim loop dispatches
    # tasks on a ThreadPoolExecutor against ONE shared ctx; a per-task
    # scrubber must therefore be thread-local (see scrub_scope) — swapping
    # the shared `scrub` attribute would let a concurrent task un-scrub
    # another task's secrets in its own log lines.
    _scrub_tls: threading.local = field(default_factory=threading.local,
                                        repr=False, compare=False)

    def effective_scrub(self) -> Callable[[str], str]:
        """The scrubber in effect for the calling thread."""
        override = getattr(self._scrub_tls, "fn", None)
        return override if override is not None else self.scrub

    @contextlib.contextmanager
    def scrub_scope(self, fn: Callable[[str], str]) -> Iterator[Callable[[str], str]]:
        """Install `fn` as this thread's scrubber until the block exits.

        Nesting-safe: the previous thread-local scrubber (if any) is
        restored afterwards. Other threads are unaffected.
        """
        prev = getattr(self._scrub_tls, "fn", None)
        self._scrub_tls.fn = fn
        try:
            yield fn
        finally:
            if prev is None:
                try:
                    del self._scrub_tls.fn
                except AttributeError:
                    pass
            else:
                self._scrub_tls.fn = prev

    def extend_scrub(self, fn: Callable[[str], str]) -> None:
        """Install `fn` as this thread's scrubber, without auto-restore.

        For handlers that learn new secret values mid-task (the deploy
        pipeline fetching project secrets over its API channel): the
        chained scrubber stays installed for the rest of the dispatch —
        deliberately NOT restored on raise paths, so the dispatcher's
        failure logging keeps redacting these values too. The dispatcher's
        scrub_scope() restores the thread's pre-task scrubber when the
        task ends, so nothing leaks across tasks or threads. Thread-local:
        concurrent dispatches on the shared ctx never see each other's
        extensions.
        """
        self._scrub_tls.fn = fn

    def log(self, task_id: str, line: str) -> str:
        """Timestamped, secret-scrubbed append to the task log. Returns the line."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        clean = self.effective_scrub()(f"[{ts}] {line}")
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
