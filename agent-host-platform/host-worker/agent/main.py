"""Host worker main loop.

Startup:
  1. load + validate config (installer provisions the host token/id and
     writes them to worker.env; main.py REQUIRES them — it never mints
     credentials on its own)
  2. build WorkerContext (API client, docker client if present, stores)
  3. post-reboot reconciliation: desired-running deployments in state.json
     vs actual Docker state — restart stopped containers, recreate missing
     ones (corrupt state.json is quarantined, never fatal)
  4. start the heartbeat thread (every HEARTBEAT_INTERVAL seconds; consecutive
     failures back off exponentially with jitter)
  5. claim loop: POST /v1/worker/tasks/claim?wait=POLL_WAIT, dispatch each
     claimed task in a small thread pool, repeat forever (claim failures
     back off exponentially with jitter)

The worker is OUTBOUND ONLY: it opens HTTPS connections to the control
plane and never listens on any port. SIGTERM/SIGINT shut it down cleanly.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from agent.api import ControlPlaneClient, WorkerAPIError
from agent import backoff as backoff_mod
from agent.config import ConfigError, WorkerConfig
from agent.context import WorkerContext
from deployments import crashloop as crashloop_mod
from deployments import reconcile as reconcile_mod
from deployments.state import DeploymentStore
from docker.client import DockerClient, DockerMissing
from executor.dispatcher import TaskDispatcher
from health import collector as health_collector
from logs.store import LogStore
from updater import self_update

LOG = logging.getLogger("agent-host-worker")
MAX_DISPATCH_WORKERS = 2


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Universal AGT host worker")
    parser.add_argument("--config", default=None,
                        help="path to worker.env (default: $WORKER_CONFIG or "
                             "/opt/agent-host/config/worker.env)")
    return parser.parse_args(argv)


def detect_capabilities(docker) -> list:
    caps = []
    if docker is not None:
        caps.append("docker")
        try:
            if docker.compose_available():
                caps.append("docker-compose")
        except Exception:
            pass
    return caps


def build_context(config: WorkerConfig) -> WorkerContext:
    for sub in ("logs", "deployments", "artifacts", "builds",
                "updates", "releases", "quarantine"):
        os.makedirs(os.path.join(config.work_dir, sub), exist_ok=True)
    os.makedirs(config.apps_dir, exist_ok=True)

    api = ControlPlaneClient(config.control_plane_url, config.host_token)
    try:
        docker = DockerClient()
        LOG.info("docker available: %s", _safe_docker_version(docker))
    except DockerMissing as exc:
        LOG.warning("%s", exc)
        docker = None

    log_store = LogStore(os.path.join(config.work_dir, "logs"))
    deployment_store = DeploymentStore(config.work_dir)
    capabilities = list(config.capabilities) or detect_capabilities(docker)
    config.capabilities = capabilities
    LOG.info("capabilities: %s", capabilities)
    return WorkerContext(config=config, api=api, docker=docker,
                         log_store=log_store,
                         deployment_store=deployment_store)


def _safe_docker_version(docker) -> str:
    try:
        return docker.version()
    except Exception as exc:
        return f"unavailable ({exc})"


def heartbeat_loop(ctx: WorkerContext, stop_event: threading.Event) -> None:
    """POST heartbeat every interval; apply advertised worker updates.

    Consecutive failures back off exponentially (5s -> 300s cap, jittered)
    so a dead control plane never hot-spins. Every wait goes through
    stop_event.wait so SIGTERM stays responsive.
    """
    config = ctx.config
    api = ctx.api
    failures = 0
    while not stop_event.is_set():
        wait_secs = config.heartbeat_interval
        try:
            payload = health_collector.collect_metrics(
                docker_client=ctx.docker,
                deployment_store=ctx.deployment_store,
                config=config,
            )
            # One-shot boot reconciliation summary, sent on the first heartbeat.
            if getattr(ctx, "reconciliation_report", None) is not None:
                payload["reconciliation"] = ctx.reconciliation_report
                ctx.reconciliation_report = None
            # Crash-loop observation pass; issues ride along in the payload
            # (the control plane turns them into service.crash_loop events).
            issues = crashloop_mod.evaluate(
                ctx,
                threshold=config.crash_loop_threshold,
                window_s=config.crash_loop_window_s,
            )
            if issues:
                payload["issues"] = issues
            resp = api.heartbeat(config.host_id, payload)
            failures = 0
            LOG.info("heartbeat ok (pending_tasks=%s)",
                     (resp or {}).get("pending_tasks"))
            try:
                outcome = self_update.check_and_apply(resp or {}, ctx, log=LOG.info)
                if outcome == "updated":
                    LOG.info("worker updated; restart requested")
                    return  # the service manager restarts us
            except self_update.UpdateError as exc:
                LOG.error("self-update failed (still on %s): %s",
                          config.worker_version, exc)
        except WorkerAPIError as exc:
            failures += 1
            wait_secs = backoff_mod.backoff_delay(failures)
            LOG.warning("heartbeat failed (%d in a row, retry in %.0fs): %s",
                        failures, wait_secs, exc)
        except Exception:
            failures += 1
            wait_secs = backoff_mod.backoff_delay(failures)
            LOG.error("heartbeat loop error (retry in %.0fs):\n%s",
                      wait_secs, traceback.format_exc(limit=5))
        stop_event.wait(wait_secs)


def claim_loop(ctx: WorkerContext, stop_event: threading.Event) -> None:
    """Long-poll for tasks; consecutive claim failures back off exponentially.

    A 204 (no work) or a claimed task both prove the control plane is
    reachable, so either resets the failure counter. All waits go through
    stop_event.wait so SIGTERM stays responsive during backoff.
    """
    config = ctx.config
    api = ctx.api
    dispatcher = TaskDispatcher(ctx, api)
    failures = 0
    with ThreadPoolExecutor(max_workers=MAX_DISPATCH_WORKERS,
                            thread_name_prefix="dispatch") as pool:
        while not stop_event.is_set():
            try:
                task = api.claim_task(config.host_id, config.capabilities,
                                      config.poll_wait)
            except WorkerAPIError as exc:
                failures += 1
                wait_secs = backoff_mod.backoff_delay(failures)
                LOG.warning("claim failed (%d in a row, retry in %.0fs): %s",
                            failures, wait_secs, exc)
                stop_event.wait(wait_secs)
                continue
            except Exception:
                failures += 1
                wait_secs = backoff_mod.backoff_delay(failures)
                LOG.error("claim loop error (retry in %.0fs):\n%s",
                          wait_secs, traceback.format_exc(limit=5))
                stop_event.wait(wait_secs)
                continue
            failures = 0  # reachable: 204 or a claimed task
            if task is None:
                continue  # 204: nothing within the wait window; long-poll again
            task_id = task.get("id", "?")
            task_type = task.get("type", "?")
            LOG.info("claimed task %s type=%s", task_id, task_type)
            pool.submit(_dispatch_guarded, dispatcher, task)


def _dispatch_guarded(dispatcher: TaskDispatcher, task: dict) -> None:
    try:
        outcome = dispatcher.dispatch(task)
        LOG.info("task %s finished: %s", task.get("id"),
                 outcome.get("status"))
    except Exception:
        LOG.error("dispatcher crashed on task %s:\n%s", task.get("id"),
                  traceback.format_exc(limit=8))


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    try:
        config = WorkerConfig.load(config_path=args.config)
        config.validate()
    except ConfigError as exc:
        LOG.error("bad configuration: %s", exc)
        return 2

    LOG.info("starting host worker %s as %s (config: %s)",
             config.worker_version, config.host_name,
             {k: v for k, v in config.redacted().items() if k != "host_token"})

    ctx = build_context(config)

    # Post-reboot reconciliation: desired state (local state.json) vs actual
    # Docker state, before any claiming starts. The summary rides along on
    # the first heartbeat payload.
    ctx.reconciliation_report = reconcile_mod.reconcile(ctx)

    stop_event = threading.Event()

    def _stop(signum, frame):
        LOG.info("received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    hb_thread = threading.Thread(target=heartbeat_loop,
                                 args=(ctx, stop_event),
                                 name="heartbeat", daemon=True)
    hb_thread.start()
    try:
        claim_loop(ctx, stop_event)
    finally:
        stop_event.set()
    LOG.info("host worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
