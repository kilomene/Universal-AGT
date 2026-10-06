"""Post-reboot reconciliation: desired state vs actual Docker state.

Runs once at worker startup, before the claim loop starts. The local
<work_dir>/deployments/*/state.json registry is the desired state; `docker`
is the actual state. For every deployment marked ``running`` locally:

  * container present and running        -> nothing to do
  * container present but not running    -> ``docker.start`` (the same call
    the ``start`` task handler uses)
  * container missing entirely          -> ``docker.run`` rebuilt from the
    stored deployment contract (deployments.pipeline.run_spec_from_state:
    stored image, stored host->container port mapping, stored non-secret
    env, stored restart policy, stored cpu/memory limits — the same call
    the deploy pipeline uses). Secret env is deliberately NOT restored: a
    rebooted host does not re-inject secrets on its own; the next redeploy
    or an ``environment-update`` task does.
  * ``docker-compose`` runtime           -> containers are matched by compose
    project name in ``docker ps -a``; stopped ones are started. A project
    with no containers at all is recreated with ``compose up`` from the
    compose file recorded at deploy time (it lives under the deployment's
    own state dir, so it survives a reboot) — the compose analogue of
    container recreation, and the same call the rollback path uses. Only a
    stack whose compose file is ALSO gone is reported as missing and left
    for the operator (redeploy required).

After the desired-state pass, containers Docker knows that NO deployment
state claims are reported as ``unexpected`` — never destroyed. Reconcile
must not blindly destroy containers; strays (crash-mid-deploy leftovers,
manually started containers, another tool's work) are surfaced for the
operator instead.

Every action is logged. The returned summary dict is attached to the next
heartbeat payload so the control plane sees what the reboot recovered.
"""
from __future__ import annotations

import logging
import os
import time

from deployments.pipeline import run_spec_from_state, verify_compose_ports_free

LOG = logging.getLogger("agent-host-worker.reconcile")

# Fallback for states written before the full deployment contract was
# persisted (see run_spec_from_state); new states always carry the policy.
DEFAULT_RESTART_POLICY = "unless-stopped"


def reconcile(ctx, log=None) -> dict:
    """Reconcile desired-running deployments with Docker. Returns a summary."""
    log = log or LOG.info
    summary = {
        "reconciled": 0,   # containers started or recreated
        "already_running": 0,
        "missing": 0,      # desired-running but could not be restored
        "skipped": 0,      # not in a reconcilable state
        "unexpected": [],  # containers in Docker claimed by no deployment
        "actions": [],     # human-readable log of what happened
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    def note(msg: str) -> None:
        summary["actions"].append(msg)
        log("reconcile: %s", msg)

    store = ctx.deployment_store
    docker = ctx.docker
    if docker is None:
        note("docker unavailable; reconciliation skipped")
        summary["skipped"] = len(_desired_running(store))
        return summary

    try:
        ps_all = {row.get("Names", "").lstrip("/"): row
                  for row in docker.ps(all=True)}
        ps_running = {row.get("Names", "").lstrip("/"): row
                      for row in docker.ps(all=False)}
    except Exception as exc:
        note(f"could not list containers ({exc}); reconciliation aborted")
        summary["skipped"] = len(_desired_running(store))
        return summary

    for state in _desired_running(store):
        deployment_id = state.get("deployment_id", "?")
        project = state.get("project_name", "?")
        name = state.get("container_name")
        if not name:
            _reconcile_compose(ctx, state, ps_all, summary, note)
            continue
        if name in ps_running:
            summary["already_running"] += 1
            note(f"{project} ({deployment_id}): container {name} already running")
        elif name in ps_all:
            try:
                docker.start(name)
                summary["reconciled"] += 1
                note(f"{project} ({deployment_id}): container {name} was "
                     f"stopped; started it")
            except Exception as exc:
                summary["missing"] += 1
                note(f"{project} ({deployment_id}): failed to start "
                     f"stopped container {name}: {exc}")
        else:
            _recreate_container(ctx, state, summary, note)
    _report_unexpected(ps_all, store, summary, note)
    return summary


def _desired_running(store) -> list:
    try:
        return [s for s in store.list_all() if s.get("status") == "running"]
    except Exception as exc:
        LOG.error("reconcile: could not read deployment store: %s", exc)
        return []


def _report_unexpected(ps_all: dict, store, summary: dict, note) -> None:
    """Report containers Docker knows that no deployment state claims.

    NEVER destroys them: reconcile must not blindly destroy containers.
    Strays — leftovers of a crash mid-deployment, manually started
    containers, another tool's work on this host — are surfaced in the
    summary (which rides the heartbeat to the control plane) for the
    operator instead.
    """
    claimed: set = set()
    compose_projects: list = []
    try:
        states = store.list_all()
    except Exception as exc:
        LOG.error("reconcile: could not read deployment store: %s", exc)
        states = []
    for state in states or []:
        name = state.get("container_name")
        if name:
            claimed.add(name)
        project = state.get("compose_project")
        if project:
            compose_projects.append(project)
    for name in sorted(ps_all):
        if name in claimed:
            continue
        if any(_compose_name_matches(p, name) for p in compose_projects):
            continue
        state_desc = ps_all[name].get("State") or "?"
        summary["unexpected"].append({"name": name, "state": state_desc})
        note(f"unexpected container {name} (state={state_desc}) is not "
             f"claimed by any deployment; left untouched")


def _compose_name_matches(compose_project: str, container_name: str) -> bool:
    """True when a container belongs to the compose project.

    Compose names containers <project>-<service>-<seq>; a bare substring
    check would wrongly match a project that is a prefix of another
    (uaht-proj vs uaht-proj2). Pure function — unit tested.
    """
    return (container_name == compose_project
            or container_name.startswith(compose_project + "-"))


def _recreate_container(ctx, state: dict, summary: dict, note) -> None:
    """Recreate a missing container from its stored deployment contract.

    The run spec comes from deployments.pipeline.run_spec_from_state —
    stored image, ports, restart policy and cpu/memory limits — so a
    rebooted worker restores exactly what was deployed, never a
    hard-coded default.
    """
    deployment_id = state.get("deployment_id", "?")
    project = state.get("project_name", "?")
    name = state.get("container_name")
    docker = ctx.docker
    spec = run_spec_from_state(state)
    image = spec["image"]
    if not image:
        summary["missing"] += 1
        note(f"{project} ({deployment_id}): container {name} missing and no "
             f"image recorded in state; cannot recreate (redeploy required)")
        return
    env = dict(state.get("env") or {})  # non-secret env only; secrets are not persisted
    try:
        docker.run(name, image, ports=spec["ports"], env=env or None,
                   memory=spec["memory"], cpus=spec["cpus"],
                   restart=spec["restart"])
        summary["reconciled"] += 1
        note(f"{project} ({deployment_id}): container {name} was missing; "
             f"recreated from image {image}"
             + (f" on {spec['ports']}" if spec["ports"] else "")
             + f" (restart={spec['restart']})")
    except Exception as exc:
        summary["missing"] += 1
        note(f"{project} ({deployment_id}): failed to recreate container "
             f"{name} from {image}: {exc}")


def _reconcile_compose(ctx, state: dict, ps_all: dict, summary: dict, note) -> None:
    """Best-effort reconcile for docker-compose deployments."""
    deployment_id = state.get("deployment_id", "?")
    project = state.get("project_name", "?")
    compose_project = state.get("compose_project")
    if not compose_project:
        summary["skipped"] += 1
        note(f"{project} ({deployment_id}): no container_name and no "
             f"compose_project recorded; skipping")
        return
    docker = ctx.docker
    matched = [n for n in ps_all if _compose_name_matches(compose_project, n)]
    if not matched:
        _recreate_compose_stack(ctx, state, summary, note)
        return
    started = 0
    for cname in matched:
        status = (ps_all[cname].get("State") or "").lower()
        if status != "running":
            try:
                docker.start(cname)
                started += 1
            except Exception as exc:
                note(f"{project} ({deployment_id}): failed to start compose "
                     f"container {cname}: {exc}")
    if started:
        summary["reconciled"] += 1
        note(f"{project} ({deployment_id}): started {started} stopped "
             f"compose container(s) of project {compose_project}")
    else:
        summary["already_running"] += 1
        note(f"{project} ({deployment_id}): compose project {compose_project} "
             f"already running")


def _recreate_compose_stack(ctx, state: dict, summary: dict, note) -> None:
    """Recreate a compose deployment whose containers are all gone.

    The compose file is extracted under the deployment's own state dir at
    deploy time, so it survives a worker restart — this is the compose
    analogue of _recreate_container, and the same ``compose up`` call the
    rollback path uses. The stack's recorded host ports are verified free
    first (the same check the deploy pipeline runs before `compose up`),
    excluding nothing: there are no containers of this project left, so a
    port held by anything else is a genuine collision.

    A stack with no containers AND no compose file cannot be rebuilt —
    that one is reported as missing, honestly, for the operator.
    """
    deployment_id = state.get("deployment_id", "?")
    project = state.get("project_name", "?")
    compose_project = state.get("compose_project")
    compose_file = state.get("compose_file")
    docker = ctx.docker
    if not compose_file or not os.path.isfile(compose_file):
        summary["missing"] += 1
        note(f"{project} ({deployment_id}): compose project {compose_project} "
             f"has no containers and its compose file {compose_file!r} is "
             f"missing (redeploy required)")
        return
    wanted: list[int] = []
    for p in state.get("compose_ports") or []:
        try:
            wanted.append(int(p))
        except (TypeError, ValueError):
            continue
    try:
        verify_compose_ports_free(docker, compose_project, wanted,
                                  registry_used=_registry_ports(ctx, state))
    except Exception as exc:
        summary["missing"] += 1
        note(f"{project} ({deployment_id}): cannot recreate compose "
             f"project {compose_project}: {exc}")
        return
    try:
        docker.compose_up(compose_file, project_name=compose_project,
                          build=True)
    except Exception as exc:
        summary["missing"] += 1
        note(f"{project} ({deployment_id}): failed to recreate compose "
             f"project {compose_project} from {compose_file}: {exc}")
        return
    summary["reconciled"] += 1
    note(f"{project} ({deployment_id}): compose project {compose_project} "
         f"had no containers; recreated via compose up {compose_file}")


def _registry_ports(ctx, state: dict) -> set:
    """Host ports reserved by OTHER live deployments (persisted contract).

    This deployment's own recorded ports are excluded: its containers are
    gone, so its own record must not read as a collision against itself.
    """
    try:
        ports = set(ctx.deployment_store.used_host_ports())
    except Exception:
        return set()
    for p in [state.get("host_port")] + list(state.get("compose_ports") or []):
        try:
            ports.discard(int(p))
        except (TypeError, ValueError):
            continue
    return ports
