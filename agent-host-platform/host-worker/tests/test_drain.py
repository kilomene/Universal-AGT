"""Draining (§37) + heartbeat §9 envelope: worker-side behavior.

Covers:
  * WORKER_DRAINING config parsing (env + file, invalid rejected)
  * apply_server_state(): heartbeat-advertised host.status=draining latches
    ctx.draining; a cleared status unlatches; malformed responses are ignored
  * claims_paused(): local config OR server latch pauses new claims
  * enrich_heartbeat_payload(): worker_status/draining/host_name/
    capabilities/ingress present, and NO secrets (host token, tunnel token)
  * claim_loop(): while draining, claim_task is never called; undisrupted
    otherwise
"""
import json
import threading
import time

import pytest

import agent.main as main_mod
from agent.config import ConfigError, WorkerConfig
from agent.context import WorkerContext


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("WORKER_DRAINING", "WORKER_CONFIG"):
        monkeypatch.delenv(key, raising=False)


def _config(**overrides):
    kw = {
        "control_plane_url": "https://cp.example.test",
        "host_name": "drain-test-host",
        "host_token": "super-secret-host-token",
        "host_id": "11111111-2222-3333-4444-555555555555",
        "poll_wait": 1,
        "tunnel_token": "tun-secret-xyz",
    }
    kw.update(overrides)
    return WorkerConfig(**kw)


def _ctx(**overrides):
    return WorkerContext(config=_config(**overrides), api=None, docker=None,
                         log_store=None, deployment_store=None)


# -- config parsing -----------------------------------------------------------


def test_draining_defaults_off(monkeypatch):
    cfg = WorkerConfig.load(config_path="/nonexistent-worker.env")
    assert cfg.draining is False


def test_draining_from_env_true(monkeypatch):
    monkeypatch.setenv("WORKER_DRAINING", "true")
    cfg = WorkerConfig.load(config_path="/nonexistent-worker.env")
    assert cfg.draining is True


def test_draining_from_env_file(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text("WORKER_DRAINING=1\n", encoding="utf-8")
    cfg = WorkerConfig.load(config_path=str(path))
    assert cfg.draining is True


def test_draining_invalid_rejected(monkeypatch):
    monkeypatch.setenv("WORKER_DRAINING", "maybe")
    with pytest.raises(ConfigError):
        WorkerConfig.load(config_path="/nonexistent-worker.env")


def test_draining_in_redacted_not_secret():
    cfg = _config(draining=True)
    assert cfg.redacted()["draining"] is True


# -- apply_server_state --------------------------------------------------------


def test_apply_server_state_latches_draining():
    ctx = _ctx()
    assert not ctx.draining.is_set()
    main_mod.apply_server_state(ctx, {"host": {"status": "draining"}})
    assert ctx.draining.is_set()


def test_apply_server_state_clears_when_operator_unsets():
    ctx = _ctx()
    ctx.draining.set()
    main_mod.apply_server_state(ctx, {"host": {"status": "online"}})
    assert not ctx.draining.is_set()


@pytest.mark.parametrize("resp", [None, {}, {"host": {}}, {"host": {"status": None}},
                                  {"host": {"status": 42}}])
def test_apply_server_state_ignores_malformed(resp):
    ctx = _ctx()
    main_mod.apply_server_state(ctx, resp)  # must not raise
    assert not ctx.draining.is_set()


def test_apply_server_state_does_not_clear_local_drain():
    # A locally-configured WORKER_DRAINING=true stays in effect even when
    # the server advertises online: claims_paused() consults both.
    ctx = _ctx(draining=True)
    main_mod.apply_server_state(ctx, {"host": {"status": "online"}})
    assert main_mod.claims_paused(ctx) is True


# -- claims_paused --------------------------------------------------------------


def test_claims_paused_neither():
    assert main_mod.claims_paused(_ctx()) is False


def test_claims_paused_server_latch():
    ctx = _ctx()
    ctx.draining.set()
    assert main_mod.claims_paused(ctx) is True


def test_claims_paused_local_config():
    assert main_mod.claims_paused(_ctx(draining=True)) is True


# -- enrich_heartbeat_payload ----------------------------------------------------


def test_enrich_heartbeat_payload_fields():
    ctx = _ctx()
    ctx.config.capabilities = ["docker", "docker-compose"]
    payload = {"cpu_pct": 12.5}
    out = main_mod.enrich_heartbeat_payload(ctx, payload)
    assert out["host_name"] == "drain-test-host"
    assert out["worker_version"] == "0.1.0"  # default when unconfigured
    assert out["worker_status"] == "running"
    assert out["draining"] is False
    assert out["capabilities"] == ["docker", "docker-compose"]
    assert out["ingress"] == {"enabled": False, "provider": ""}
    assert out["cpu_pct"] == 12.5  # original metrics preserved


def test_enrich_heartbeat_payload_reports_configured_version():
    ctx = _ctx(worker_version="1.2.3")
    out = main_mod.enrich_heartbeat_payload(ctx, {})
    assert out["worker_version"] == "1.2.3"


def test_apply_server_state_warns_once_on_worker_outdated(caplog):
    ctx = _ctx()
    with caplog.at_level("WARNING"):
        main_mod.apply_server_state(ctx, {"worker_outdated": True, "host": {"status": "online"}})
        main_mod.apply_server_state(ctx, {"worker_outdated": True, "host": {"status": "online"}})
    outdated = [r for r in caplog.records if "OUTDATED" in r.message]
    assert len(outdated) == 1  # warned once, not on every heartbeat
    # clears when the plane stops flagging us
    with caplog.at_level("WARNING"):
        caplog.clear()
        main_mod.apply_server_state(ctx, {"host": {"status": "online"}})
    assert not [r for r in caplog.records if "OUTDATED" in r.message]


def test_enrich_heartbeat_payload_draining_and_ingress():
    ctx = _ctx(ingress_enabled=True, ingress_provider="cloudflare-tunnel")
    ctx.draining.set()
    out = main_mod.enrich_heartbeat_payload(ctx, {})
    assert out["worker_status"] == "draining"
    assert out["draining"] is True
    assert out["ingress"] == {"enabled": True, "provider": "cloudflare-tunnel"}


def test_enrich_heartbeat_payload_has_no_secrets():
    ctx = _ctx()
    out = main_mod.enrich_heartbeat_payload(ctx, {})
    blob = json.dumps(out)
    assert "super-secret-host-token" not in blob
    assert "tun-secret-xyz" not in blob
    assert not any("token" in key.lower() for key in out)


# -- heartbeat_loop log-prune wiring ------------------------------------------------


class _FakeHeartbeatAPI:
    def __init__(self):
        self.heartbeats = 0

    def heartbeat(self, host_id, payload):
        self.heartbeats += 1
        return {"pending_tasks": 0, "host": {"status": "online"}}


class _FakeLogStore:
    def __init__(self):
        self.prune_calls = 0

    def prune(self, max_age_days=None, deployment_store=None, log=None):
        self.prune_calls += 1
        return {"deleted": [], "kept": [], "errors": []}


def test_heartbeat_loop_prunes_every_20th_heartbeat(monkeypatch):
    ctx = _ctx(heartbeat_interval=1)
    api = _FakeHeartbeatAPI()
    store = _FakeLogStore()
    ctx.api = api
    ctx.log_store = store
    monkeypatch.setattr(main_mod.health_collector, "collect_metrics",
                        lambda **kw: {})
    monkeypatch.setattr(main_mod.crashloop_mod, "evaluate",
                        lambda ctx, **kw: [])
    monkeypatch.setattr(main_mod.self_update, "check_and_apply",
                        lambda resp, ctx, log=None: None)
    # drive 21 heartbeats quickly: interval=1s would be slow, so patch the
    # wait to return immediately and stop after 21 beats
    original_wait = threading.Event.wait
    def fast_wait(self, timeout=None):
        if api.heartbeats >= 21:
            self.set()
        return original_wait(self, 0.001)
    monkeypatch.setattr(threading.Event, "wait", fast_wait)
    stop = threading.Event()
    thread = threading.Thread(target=main_mod.heartbeat_loop,
                              args=(ctx, stop), daemon=True)
    thread.start()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert api.heartbeats >= 20
    # prune fires on the 20th heartbeat only
    assert store.prune_calls == 1


class _FakeAPI:
    def __init__(self):
        self.claim_calls = []

    def claim_task(self, host_id, capabilities, wait):
        self.claim_calls.append((host_id, capabilities, wait))
        return None  # 204: nothing to do


class _FakeDispatcher:
    def __init__(self, ctx, api):
        pass

    def dispatch(self, task):
        return {"status": "completed"}


def _run_claim_loop(ctx, seconds=0.6):
    stop = threading.Event()
    thread = threading.Thread(target=main_mod.claim_loop, args=(ctx, stop),
                              daemon=True)
    thread.start()
    time.sleep(seconds)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return stop


def test_claim_loop_skips_claim_while_server_draining(monkeypatch):
    monkeypatch.setattr(main_mod, "TaskDispatcher", _FakeDispatcher)
    ctx = _ctx()
    ctx.draining.set()
    api = _FakeAPI()
    ctx.api = api
    _run_claim_loop(ctx)
    assert api.claim_calls == []


def test_claim_loop_skips_claim_while_locally_draining(monkeypatch):
    monkeypatch.setattr(main_mod, "TaskDispatcher", _FakeDispatcher)
    ctx = _ctx(draining=True)
    api = _FakeAPI()
    ctx.api = api
    _run_claim_loop(ctx)
    assert api.claim_calls == []


def test_claim_loop_claims_when_not_draining(monkeypatch):
    monkeypatch.setattr(main_mod, "TaskDispatcher", _FakeDispatcher)
    ctx = _ctx()
    api = _FakeAPI()
    ctx.api = api
    _run_claim_loop(ctx)
    assert len(api.claim_calls) >= 1
    host_id, capabilities, wait = api.claim_calls[0]
    assert host_id == "11111111-2222-3333-4444-555555555555"
    assert wait == 1
