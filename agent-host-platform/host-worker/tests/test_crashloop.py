"""Tests for deployments.crashloop.

Covers the pure predicate is_crash_looping (threshold/window math) and the
evaluate() pass with an in-memory fake docker client and real
DeploymentStore on tmp_path.
"""
import time

import pytest

from deployments import crashloop as crashloop_mod
from deployments.state import DeploymentStore


def _obs(*pairs):
    return [[float(ts), int(n)] for ts, n in pairs]


NOW = 1_000_000.0


def test_no_observations_not_crash_loop():
    assert crashloop_mod.is_crash_looping([], now=NOW) is False


def test_single_observation_not_crash_loop():
    assert crashloop_mod.is_crash_looping(_obs((NOW, 3)), now=NOW) is False


def test_threshold_crossed_within_window():
    obs = _obs((NOW - 200, 0), (NOW - 100, 2), (NOW, 5))
    assert crashloop_mod.is_crash_looping(obs, threshold=5, window_s=300, now=NOW) is True


def test_below_threshold_not_crash_loop():
    obs = _obs((NOW - 200, 0), (NOW, 4))
    assert crashloop_mod.is_crash_looping(obs, threshold=5, window_s=300, now=NOW) is False


def test_stale_observations_do_not_count():
    # 5 restarts happened, but only 1 of them is inside the 300s window.
    obs = _obs((NOW - 900, 0), (NOW - 800, 1), (NOW - 700, 2),
               (NOW - 600, 3), (NOW - 500, 4), (NOW - 10, 5))
    assert crashloop_mod.is_crash_looping(obs, threshold=5, window_s=300, now=NOW) is False


def test_restart_count_reset_by_recreate_is_not_crash_loop():
    obs = _obs((NOW - 100, 9), (NOW, 0))  # container recreated; count reset
    assert crashloop_mod.is_crash_looping(obs, threshold=5, window_s=300, now=NOW) is False


def test_custom_threshold_and_window():
    obs = _obs((NOW - 50, 0), (NOW, 3))
    assert crashloop_mod.is_crash_looping(obs, threshold=3, window_s=60, now=NOW) is True
    assert crashloop_mod.is_crash_looping(obs, threshold=4, window_s=60, now=NOW) is False


def test_invalid_args_rejected():
    with pytest.raises(ValueError):
        crashloop_mod.is_crash_looping([], threshold=0, now=NOW)
    with pytest.raises(ValueError):
        crashloop_mod.is_crash_looping([], window_s=0, now=NOW)


# ---------------------------------------------------------------------------
# evaluate() with fakes
# ---------------------------------------------------------------------------

class FakeDockerClient:
    """restart_count rises on each call to simulate a flapping container."""

    def __init__(self, counts):
        # counts: name -> list of restart counts served in order
        self.counts = {k: list(v) for k, v in counts.items()}
        self.stop_calls = []

    def restart_count(self, name):
        series = self.counts.get(name)
        if series is None:
            return None  # container gone
        if len(series) > 1:
            return series.pop(0)
        return series[0]

    def stop(self, name, timeout_secs=10, timeout=120):
        self.stop_calls.append(name)


class FakeCtx:
    def __init__(self, store, docker):
        self.deployment_store = store
        self.docker = docker


def _save(store, deployment_id, **fields):
    state = {
        "deployment_id": deployment_id,
        "project_id": "proj-1",
        "project_name": "web",
        "version": "1.0.0",
        "container_name": "uaht-web-1-0-0",
        "image": "uaht-web:1.0.0",
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
    }
    state.update(fields)
    store.save(state)
    return state


def test_evaluate_detects_and_stops_crash_loop(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")
    # six observations climbing 0..5 within the window
    docker = FakeDockerClient({"uaht-web-1-0-0": [0, 1, 2, 3, 4, 5, 5]})
    ctx = FakeCtx(store, docker)

    issues = []
    for _ in range(7):  # 6th call detects; 7th reports the standing flag
        issues = crashloop_mod.evaluate(ctx, threshold=5, window_s=300)

    assert docker.stop_calls == ["uaht-web-1-0-0"]  # stopped exactly once
    state = store.load("dep-1")
    assert state["crash_loop"] is True
    assert state["status"] == "crash_loop"
    assert issues and issues[0]["issue"] == "crash_loop"
    assert issues[0]["deployment_id"] == "dep-1"
    assert issues[0]["already_flagged"] is True  # still reported for dashboards


def test_evaluate_stable_container_not_flagged(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")
    docker = FakeDockerClient({"uaht-web-1-0-0": [0, 0, 0, 0, 0, 0]})

    issues = []
    for _ in range(6):
        issues = crashloop_mod.evaluate(FakeCtx(store, docker),
                                        threshold=5, window_s=300)

    assert docker.stop_calls == []
    assert store.load("dep-1").get("crash_loop") is not True
    assert issues == []


def test_evaluate_skips_already_flagged_without_restopping(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", crash_loop=True, status="crash_loop")
    docker = FakeDockerClient({"uaht-web-1-0-0": [99]})

    issues = crashloop_mod.evaluate(FakeCtx(store, docker))

    assert docker.stop_calls == []  # never restarted / re-stopped
    assert len(issues) == 1
    assert issues[0]["already_flagged"] is True


def test_evaluate_ignores_non_running_and_missing_containers(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-stopped", status="stopped")
    _save(store, "dep-gone", container_name="uaht-gone")  # restart_count -> None
    docker = FakeDockerClient({})

    issues = crashloop_mod.evaluate(FakeCtx(store, docker))

    assert docker.stop_calls == []
    assert issues == []


def test_evaluate_without_docker_is_noop(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1")
    assert crashloop_mod.evaluate(FakeCtx(store, None)) == []


def test_clear_crash_loop_flag(tmp_path):
    store = DeploymentStore(str(tmp_path))
    _save(store, "dep-1", crash_loop=True, status="crash_loop",
          restart_observations=[[time.time(), 5]])

    assert crashloop_mod.clear_crash_loop(FakeCtx(store, None), "dep-1") is True
    state = store.load("dep-1")
    assert state["crash_loop"] is False
    assert state["status"] == "stopped"
    assert "restart_observations" not in state
    # second clear is a no-op
    assert crashloop_mod.clear_crash_loop(FakeCtx(store, None), "dep-1") is False
