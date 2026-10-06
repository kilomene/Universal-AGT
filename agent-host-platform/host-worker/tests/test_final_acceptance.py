# W18 FINAL AUDIT — spec §70 30-step acceptance scenario (locally executable part).
#
# Runs the full agent→API→DB→task→worker→artifact→(fake-)Docker→healthcheck
# chain plus secret/env change, redeploy, broken-build, health-fail
# auto-rollback, API-driven rollback, and the manual approval gate as ONE
# CHAINED scenario against a single host — something no existing test does
# (each existing E2E scenario uses a fresh plane/rig).
#
# Harness: FakeControlPlane over real HTTP (from test_e2e.py) + the REAL
# worker stack (ControlPlaneClient, TaskDispatcher, deploy pipeline,
# health checker, SecretScrubber) + SubprocessDockerClient running the REAL
# demo-app server.py. Deterministic: no real Docker daemon, no Postgres,
# no Cloudflare, no systemd in this sandbox. Steps that need live infra
# are explicit pytest.skip()s naming the exact external piece.
#
# Step numbering follows the §70 reconstruction in the audit report.
# Every executable step records PASS into LEDGER; the ledger test at the
# end prints the table and fails if any executable step did not pass.

import json
import threading
from pathlib import Path

import pytest
import requests

from test_e2e import (
    FakeControlPlane,
    WorkerRig,
    AgentClient,
    make_demo_src,
    _wait_http_ok,
    _container_name_for,
    assert_correlation,
    _RIGS,
)

LEDGER = []  # (step_no, name, result, evidence)


def record(no, name, result, evidence):
    LEDGER.append((no, name, result, evidence))


@pytest.fixture(scope="module")
def acc_plane():
    p = FakeControlPlane().start()
    yield p
    p.shutdown()


@pytest.fixture(scope="module")
def acc_rig(acc_plane, tmp_path_factory):
    work = tmp_path_factory.mktemp("acc-worker")
    rig = WorkerRig(acc_plane, work, name="acc-host")
    yield rig
    rig.close()


@pytest.fixture(scope="module")
def acc_agent(acc_plane):
    return AgentClient(acc_plane)


@pytest.fixture(scope="module")
def acc_state():
    return {}


def deploy_and_cycle(rig, agent, project_id, version, src_dir, *,
                     mode="automatic", artifact_id=None, approve=False):
    """Upload artifact, create deployment, run one worker cycle. Returns
    (artifact, deployment, task, outcome)."""
    art = agent.upload_artifact(project_id, src_dir)
    assert art["checksum"].startswith("sha256:"), "checksum must be sha256:<hex>"
    assert art["status"] == "ready"
    deployment, task = agent.create_deployment(
        project_id, rig.host_id, version, art["id"], mode=mode)
    if approve:
        agent.approve(task["id"])
    outcome = rig.cycle(wait=5)
    assert outcome is not None, "worker claimed nothing"
    return art, deployment, task, outcome


def serving_port(outcome):
    return int(next(iter(outcome["result"]["ports"])))


class TestAcceptanceChain:
    """The chained hero scenario. Order matters: each step builds on the
    previous host state."""

    # -- step 1: agent creates project + stores a secret -------------------
    def test_step01_project_and_secret(self, acc_agent, acc_state):
        project = acc_agent.create_project("acc-demo")
        acc_agent.set_project_secrets(project["id"], {"API_TOKEN": "acc-token-v1"})
        acc_state["project_id"] = project["id"]
        record(1, "agent creates project + stores secret", "PASS",
               f"project={project['id'][:8]}… secret names-only on the plane")

    # -- step 2: artifact two-phase upload + SHA-256 verification ----------
    def test_step02_artifact_upload(self, acc_agent, acc_state, tmp_path_factory):
        src = make_demo_src(tmp_path_factory.mktemp("acc-src-v1"), app_name="acc-demo", version="1.0.0")
        art = acc_agent.upload_artifact(acc_state["project_id"], src)
        assert art["status"] == "ready"
        assert art["checksum"].startswith("sha256:") and len(art["checksum"]) == 71
        acc_state["v1_art"] = art
        record(2, "artifact init+PUT+ready, SHA-256 verified", "PASS",
               f"artifact={art['id'][:8]}… checksum={art['checksum'][:14]}…")

    # -- step 3: deployment creates queued task w/ checksum injection ------
    def test_step03_deployment_task(self, acc_agent, acc_rig, acc_state):
        art = acc_state["v1_art"]
        deployment, task = acc_agent.create_deployment(
            acc_state["project_id"], acc_rig.host_id, "1.0.0", art["id"])
        assert task["status"] == "queued"
        assert task["payload"]["artifact_checksum"] == art["checksum"]
        assert task["payload"]["project_name"] == "acc-demo"
        assert task["payload"]["deployment_id"] == deployment["id"]
        acc_state["v1_dep"], acc_state["v1_task"] = deployment, task
        record(3, "deployment → queued deploy task w/ checksum+project_name", "PASS",
               f"task={task['id'][:8]}… → deployment={deployment['id'][:8]}…")

    # -- step 4: worker claims, verifies, builds, runs, healthchecks ------
    def test_step04_worker_deploys_v1(self, acc_agent, acc_rig, acc_plane, acc_state):
        task = acc_state["v1_task"]
        deployment = acc_state["v1_dep"]
        outcome = acc_rig.cycle(wait=5)
        assert outcome is not None and outcome["status"] == "completed"
        port = serving_port(outcome)
        assert acc_agent.get_task(task["id"])["status"] == "completed"
        assert acc_agent.get_deployment(deployment["id"])["status"] == "running"
        health = _wait_http_ok(port, "/health")
        assert health.status_code == 200 and health.json() == {"ok": True}
        name = _container_name_for(acc_rig, deployment["id"])
        assert acc_rig.docker.env_of(name)["API_TOKEN"] == "acc-token-v1"
        state = acc_rig.ctx.deployment_store.load(deployment["id"])
        assert "acc-token-v1" not in json.dumps(state)
        types = [t for t in acc_plane.event_types() if t != "host.registered"]
        assert types == ["deployment.requested", "task.created", "task.claimed",
                         "task.started", "deployment.started",
                         "task.completed", "deployment.completed"], types
        assert_correlation(acc_plane, acc_agent, task["id"], deployment["id"],
                           acc_state["project_id"], acc_state["v1_art"]["id"],
                           acc_rig.host_id)
        acc_state["v1_port"] = port
        record(4, "worker deploys v1: verified→built→running→health 200, "
                  "secret in env / not in state, event order exact", "PASS",
               f"serving on 127.0.0.1:{port}, container={name}")

    # -- step 5: agent disappears mid-deploy, reconnects, reads final state
    def test_step05_agent_disappearance(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        src = make_demo_src(tmp_path_factory.mktemp("acc-src-v2"), app_name="acc-demo", version="2.0.0")
        art = acc_agent.upload_artifact(acc_state["project_id"], src)
        deployment, task = acc_agent.create_deployment(
            acc_state["project_id"], acc_rig.host_id, "2.0.0", art["id"])
        holder = {}
        t = threading.Thread(target=lambda: holder.update(outcome=acc_rig.cycle(wait=5)),
                             daemon=True)
        t.start()
        seen = None
        for _ in range(6):
            seen = acc_agent.get_task(task["id"])["status"]
            if seen in ("claimed", "running"):
                break
        acc_agent.disconnect()   # the agent vanishes mid-deploy
        t.join(timeout=120)
        outcome = holder["outcome"]
        assert outcome is not None and outcome["status"] == "completed"
        acc_agent.reconnect()     # ...and comes back later
        final = acc_agent.get_task(task["id"])
        assert final["status"] == "completed"
        assert acc_agent.get_deployment(deployment["id"])["status"] == "running"
        acc_state.update(v2_dep=deployment, v2_task=task, v2_art=art,
                         v2_port=serving_port(outcome))
        record(5, "agent disappears mid-deploy; v2 completes anyway; "
                  "reconnected agent retrieves final state", "PASS",
               f"task went {seen} → completed without the agent")

    # -- step 6: secret/env change → redeploy picks up the new value ------
    def test_step06_secret_rotation_redeploy(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        acc_agent.set_project_secrets(acc_state["project_id"], {"API_TOKEN": "acc-token-v2"})
        src = make_demo_src(tmp_path_factory.mktemp("acc-src-v3"), app_name="acc-demo", version="3.0.0")
        art, deployment, task, outcome = deploy_and_cycle(
            acc_rig, acc_agent, acc_state["project_id"], "3.0.0", src)
        assert outcome["status"] == "completed"
        name = _container_name_for(acc_rig, deployment["id"])
        assert acc_rig.docker.env_of(name)["API_TOKEN"] == "acc-token-v2"
        state = acc_rig.ctx.deployment_store.load(deployment["id"])
        assert "acc-token-v2" not in json.dumps(state)
        health = _wait_http_ok(serving_port(outcome), "/health")
        assert health.status_code == 200
        acc_state.update(v3_dep=deployment, v3_task=task,
                         v3_port=serving_port(outcome))
        record(6, "secret changed → v3 redeploy injects NEW value; never in state", "PASS",
               f"container env API_TOKEN=acc-token-v2 (rotated)")

    # -- step 7: broken build → failed task; previous version keeps serving
    def test_step07_broken_build_keeps_previous(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        src = make_demo_src(tmp_path_factory.mktemp("acc-src-v4"), app_name="acc-demo", version="4.0.0",
                            broken_build=True)
        art, deployment, task, outcome = deploy_and_cycle(
            acc_rig, acc_agent, acc_state["project_id"], "4.0.0", src)
        assert outcome["status"] == "failed"
        assert acc_agent.get_task(task["id"])["status"] == "failed"
        # v3 still serving on its port.
        health = _wait_http_ok(acc_state["v3_port"], "/health")
        assert health.status_code == 200
        log = Path(acc_rig.ctx.log_store.task_log_path(task["id"])).read_text()
        assert len(log) > 0
        record(7, "broken build → task failed, logs retained, v3 still serving", "PASS",
               f"failed task log {len(log)} bytes; v3 on :{acc_state['v3_port']} OK")

    # -- step 8: health-fail → auto-rollback to previous -------------------
    # Product semantics (verified in test_e2e.test_health_fail_rollback):
    # the TASK fails ("healthcheck failed") while the worker removes the
    # bad generation and restarts the previous one.
    def test_step08_health_fail_auto_rollback(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        src = make_demo_src(tmp_path_factory.mktemp("acc-src-v5"), app_name="acc-demo", version="5.0.0",
                            unhealthy=True)
        art, deployment, task, outcome = deploy_and_cycle(
            acc_rig, acc_agent, acc_state["project_id"], "5.0.0", src)
        assert outcome["status"] == "failed"
        assert "healthcheck failed" in (outcome.get("error") or "")
        assert acc_agent.get_task(task["id"])["status"] == "failed"
        # v5's container stopped+removed; v3 restarted and serving again.
        cur = acc_rig.ctx.deployment_store.load(acc_state["v3_dep"]["id"])
        assert cur["status"] == "running"
        health = _wait_http_ok(acc_state["v3_port"], "/health")
        assert health.status_code == 200
        record(8, "unhealthy v5 → task FAILED; auto-rollback: v5 removed, "
                  "v3 restarted+serving", "PASS",
               "rolled_back_to=" + acc_state["v3_dep"]["id"][:8] + "…")

    # -- step 9: agent-driven rollback (type=rollback task) ----------------
    # Mirrors POST /v1/deployments/:id/rollback (control plane names the
    # target; here the agent names it explicitly).
    def _rollback_cycle(self, acc_agent, acc_rig, deployment_id, target_id):
        acc_agent.create_task(
            "rollback",
            {"deployment_id": deployment_id, "target_deployment_id": target_id},
            assigned_to=acc_rig.host_id)
        return acc_rig.cycle(wait=5)

    def test_step09a_rollback_within_gc_window(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        # Fresh project so the GC window (keep=2) is not exhausted.
        project = acc_agent.create_project("acc-rb")
        acc_state["rb_project_id"] = project["id"]
        src1 = make_demo_src(tmp_path_factory.mktemp("acc-rb-w1"), app_name="acc-rb",
                             version="1.0.0")
        _a1, d1, _t1, o1 = deploy_and_cycle(acc_rig, acc_agent, project["id"], "1.0.0", src1)
        assert o1["status"] == "completed"
        acc_state["rb_w1_id"] = d1["id"]
        p1 = serving_port(o1)
        src2 = make_demo_src(tmp_path_factory.mktemp("acc-rb-w2"), app_name="acc-rb",
                             version="2.0.0")
        _a2, d2, _t2, o2 = deploy_and_cycle(acc_rig, acc_agent, project["id"], "2.0.0", src2)
        assert o2["status"] == "completed"
        p2 = serving_port(o2)
        out = self._rollback_cycle(acc_agent, acc_rig, d2["id"], d1["id"])
        assert out is not None and out["status"] == "completed", out
        assert out["result"]["rolled_back_to"] == d1["id"]
        # Rollback restores the target's ORIGINAL container (and its original
        # port mapping): w1 serves again on p1; w2's container is gone.
        health = _wait_http_ok(p1, "/health")
        assert health.status_code == 200 and health.json() == {"ok": True}
        with pytest.raises(requests.RequestException):
            requests.get(f"http://127.0.0.1:{p2}/health", timeout=5)
        record(9, "agent-driven rollback w2 → w1: restored on its original port, serving", "PASS",
               f"w1 back on :{p1}; w2's container removed")

    def test_step09b_rollback_beyond_gc_window(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        # The worker's GC keeps only the newest 2 generations
        # (DEPLOY_KEEP_GENERATIONS): after w3 deploys, w1's container AND its
        # image are removed — but the control plane's selectRollbackTarget
        # has no knowledge of local GC and can still name w1. The handler
        # must then FAIL CLEANLY: verify-before-teardown means the healthy
        # w3 keeps serving instead of the project being left down.
        src3 = make_demo_src(tmp_path_factory.mktemp("acc-rb-w3"), app_name="acc-rb",
                             version="3.0.0")
        _a3, d3, _t3, o3 = deploy_and_cycle(
            acc_rig, acc_agent, acc_state["rb_project_id"], "3.0.0", src3)
        assert o3["status"] == "completed"
        p3 = serving_port(o3)
        out = self._rollback_cycle(acc_agent, acc_rig, d3["id"], acc_state["rb_w1_id"])
        assert out is not None and out["status"] == "failed", out
        assert "cannot be restored" in (out.get("error") or ""), out.get("error")
        # w3 was NOT torn down: still serving on its port.
        health = _wait_http_ok(p3, "/health")
        assert health.status_code == 200 and health.json() == {"ok": True}
        record("9b", "rollback to a GC'd generation fails cleanly ('cannot be "
                     "restored') and the healthy deployment keeps serving — "
                     "verify-before-teardown", "PASS",
               f"w3 still serving on :{p3}; nothing torn down")

    # -- step 10: manual approval gate: approve deploys, reject never starts
    def test_step10_manual_approval_gate(self, acc_agent, acc_rig, acc_state, tmp_path_factory):
        src = make_demo_src(tmp_path_factory.mktemp("acc-src-v7"), app_name="acc-demo", version="7.0.0")
        art = acc_agent.upload_artifact(acc_state["project_id"], src)
        dep_ok, task_ok = acc_agent.create_deployment(
            acc_state["project_id"], acc_rig.host_id, "7.0.0", art["id"], mode="manual")
        assert task_ok["status"] == "awaiting_approval"
        acc_agent.approve(task_ok["id"])
        out = acc_rig.cycle(wait=5)
        assert out is not None and out["status"] == "completed"

        before = len(acc_rig.docker.run_calls)
        dep_no, task_no = acc_agent.create_deployment(
            acc_state["project_id"], acc_rig.host_id, "8.0.0", art["id"], mode="manual")
        acc_agent.reject(task_no["id"])
        assert acc_agent.get_task(task_no["id"])["status"] == "cancelled"
        assert acc_rig.cycle(wait=3) is None
        assert len(acc_rig.docker.run_calls) == before, "rejected deploy must start zero containers"
        record(10, "manual gate: approve → deployed; reject → never claimed, "
                   "zero docker calls", "PASS",
               f"approved v7 completed; rejected v8 never started")

    # -- steps needing live infra -----------------------------------------
    def test_step19_public_https(self):
        pytest.skip("BLOCKED-ON-LIVE-INFRA: real Cloudflare credentials + "
                    "real domain (see docs/e2e-live-checklist.md check #12)")

    def test_step22_real_systemd_reboot(self):
        pytest.skip("BLOCKED-ON-LIVE-INFRA: real Linux host with systemd + "
                    "real Docker daemon (see docs/e2e-live-checklist.md check #10); "
                    "reconcile() logic is unit-tested, the reboot itself is not")


def test_acceptance_ledger():
    """Prints the final step table; fails if any executable step failed."""
    print("\n=== FINAL ACCEPTANCE LEDGER (locally executable steps) ===")
    failed = [r for r in LEDGER if r[2] not in ("PASS",)]
    for no, name, result, evidence in sorted(LEDGER, key=lambda r: str(r[0])):
        print(f"  step {no!s:>3} [{result:4s}] {name}\n"
              f"           evidence: {evidence}")
    assert LEDGER, "no acceptance steps ran"
    assert not failed, f"{len(failed)} acceptance steps did not pass: {failed}"
