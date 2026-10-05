"""Crash-loop detection.

A container that keeps dying under its restart policy would flap forever,
burning CPU and log space. This module watches ``docker inspect``'s
``RestartCount`` per deployment: when restarts grow by >=
``CRASH_LOOP_THRESHOLD`` (env ``WORKER_CRASH_LOOP_THRESHOLD``, default 5)
within ``CRASH_LOOP_WINDOW_S`` (env ``WORKER_CRASH_LOOP_WINDOW_S``,
default 300), the container is explicitly stopped — an explicit
``docker stop`` means ``--restart unless-stopped`` will NOT bring it back —
and the local state is flagged with ``crash_loop=True`` and
``status='crash_loop'``.

Surfaced two ways: the heartbeat payload carries an ``issues`` array entry,
and the control plane translates it into a ``service.crash_loop`` event the
first time it appears. Deployments already flagged keep their issue entry so
dashboards keep showing them; the container is never restarted again by the
worker.

Observations live on the deployment state as
``restart_observations: [[unix_ts, restart_count], ...]`` (pruned to the
window, capped in length).
"""
from __future__ import annotations

import logging
import time

LOG = logging.getLogger("agent-host-worker.crashloop")

DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_S = 300
_MAX_OBSERVATIONS = 32


def is_crash_looping(observations: list, threshold: int = DEFAULT_THRESHOLD,
                     window_s: int = DEFAULT_WINDOW_S, now: float | None = None) -> bool:
    """Pure predicate: did restarts rise by >= threshold inside the window?

    `observations` is a list of ``(unix_ts, restart_count)`` tuples (or
    two-element lists), oldest first not required. RestartCount resets to 0
    when a container is recreated, so a negative delta never counts.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    if window_s <= 0:
        raise ValueError(f"window_s must be > 0, got {window_s}")
    now = time.time() if now is None else now
    recent = sorted(
        (o for o in observations if now - float(o[0]) <= window_s),
        key=lambda o: float(o[0]),
    )
    if len(recent) < 2:
        return False
    delta = int(recent[-1][1]) - int(recent[0][1])
    return delta >= threshold


def _issue(state: dict, restarts: int | None, already: bool) -> dict:
    return {
        "issue": "crash_loop",
        "deployment_id": state.get("deployment_id"),
        "project": state.get("project_name"),
        "container": state.get("container_name"),
        "restarts": restarts,
        "already_flagged": already,
    }


def evaluate(ctx, threshold: int = DEFAULT_THRESHOLD,
             window_s: int = DEFAULT_WINDOW_S, log=None) -> list:
    """One observation pass over running deployments.

    Records a fresh (ts, RestartCount) observation per deployment, stops and
    flags containers that cross the crash-loop threshold, and returns the
    heartbeat ``issues`` entries (new detections + still-flagged).
    """
    log = log or LOG.info
    issues: list = []
    docker = ctx.docker
    store = ctx.deployment_store
    if docker is None:
        return issues
    now = time.time()
    try:
        states = store.list_all()
    except Exception as exc:
        LOG.error("crash-loop check: could not read deployment store: %s", exc)
        return issues

    for state in states:
        if state.get("crash_loop"):
            issues.append(_issue(state, None, already=True))
            continue
        if state.get("status") != "running":
            continue
        name = state.get("container_name")
        if not name:
            continue  # compose projects are tracked via their own healthchecks
        try:
            count = docker.restart_count(name)
        except Exception as exc:
            LOG.warning("crash-loop check: restart_count(%s) failed: %s", name, exc)
            continue
        if count is None:
            continue  # container gone entirely; reconciliation owns that case
        obs = state.get("restart_observations") or []
        obs.append([now, int(count)])
        obs = [o for o in obs if now - float(o[0]) <= 2 * window_s][-_MAX_OBSERVATIONS:]
        state["restart_observations"] = obs
        store.save(state)
        if is_crash_looping(obs, threshold, window_s, now=now):
            log("crash-loop: %s restarted %d times recently; stopping it "
                "(flagged, will not be restarted)", name, count)
            try:
                docker.stop(name)  # explicit stop: unless-stopped stays stopped
            except Exception as exc:
                LOG.warning("crash-loop check: stop(%s) failed: %s", name, exc)
            state["crash_loop"] = True
            state["status"] = "crash_loop"
            store.save(state)
            issues.append(_issue(state, count, already=False))
    return issues


def clear_crash_loop(ctx, deployment_id: str) -> bool:
    """Operator escape hatch: clear the flag so a later start/redeploy can run.

    Returns True if a flag was present and cleared.
    """
    store = ctx.deployment_store
    state = store.load(deployment_id)
    if not state or not state.get("crash_loop"):
        return False
    state["crash_loop"] = False
    state.pop("restart_observations", None)
    if state.get("status") == "crash_loop":
        state["status"] = "stopped"
    store.save(state)
    LOG.info("crash-loop flag cleared for deployment %s", deployment_id)
    return True
