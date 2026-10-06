"""Unified rollback implementation (spec §§1-4, §21).

ONE source of truth for both rollback paths:

  * automatic rollback inside ``deployments.pipeline.deploy()`` — a failed
    healthcheck on a new deployment rolls back to the previous healthy
    generation;
  * explicit rollback via ``executor.handlers.handle_rollback`` — the
    control plane names a target deployment to restore.

Phases, in order::

    prepare/verify-target -> [teardown hook] -> restore ->
    health-check-target (REAL) -> commit state

Port-reservation reconciliation with the control plane rides on the
EXISTING API (``POST /v1/deployments/:id/settle-rollback-ports``) via
:func:`settle_rollback_ports`; no new protocol is invented.

Key guarantees:

  * the restored target's ``health_status`` ALWAYS comes from a REAL
    health check with the same semantics as a normal deployment's
    (``health.checker.wait_for_healthcheck`` on the target's recorded
    host port). It is never marked healthy just because a container
    restarted; a target that cannot become healthy is recorded
    ``"unhealthy"`` honestly, and a target with nothing health-checkable
    (no host port to check against) is recorded ``"unknown"`` — unknown
    is never a success: the rollback fails durably (``rollback_failed``).
  * verify-before-destroy: the target's restorability (container or
    compose stack present — or rebuildable from the persisted deployment
    contract — plus its host ports free) is checked BEFORE anything is
    torn down. A bad target raises :class:`RollbackError` leaving the
    healthy deployment untouched.
  * same-port rollbacks (spec §5): the deployment being replaced
    (``current``) legitimately holds the target's host ports until its
    teardown. The verify phase excludes the current deployment's
    containers/ports from the collision check — a port bound ONLY by the
    about-to-be-torn-down deployment is treated as free-after-teardown,
    while a port held by anything else still fails the rollback loudly.
    Teardown always runs before restore, so two physical containers never
    claim the same host port at once.
  * durable rollback phases (spec §6): the operation moves through
    ``requested`` -> ``restoring`` -> ``health-checking`` ->
    ``succeeded``, or lands in ``failed`` / ``partially_reconciled``.
    The transient phases are recorded on the target row so a worker
    crash mid-rollback is distinguishable on the next reconcile pass;
    callers persist the terminal outcome (never a false success).
  * a port-settle failure never reports success: it is logged and
    returned in the settle outcome (``settled: False`` + reason) so the
    caller records a durable ``rollback_failed`` (``partially_reconciled``)
    instead of claiming success. Docker/OS state and the worker's
    deployment state are reconciled physically by the verify/restore
    phases; the control-plane registry is advisory and the worker's
    physical port checks are the backstop.

Production paths use no mocks: every docker interaction goes through the
injected docker client, the health check is the real
``health.checker.wait_for_healthcheck``, and the port settle is a real
HTTPS POST over the worker's control-plane session.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from health import checker as health_checker


class RollbackError(Exception):
    """A rollback phase (verify or restore) failed.

    Callers translate it into their own fatal error type
    (``DeployError`` in the pipeline, ``HandlerError`` in the executor).
    The health check itself never raises RollbackError: an unhealthy
    target is recorded honestly via its health_status instead.
    """


def is_restorable(state: dict) -> bool:
    """A deployment can be restored by rollback when it has a container
    or a compose stack recorded."""
    return bool(state.get("container_name") or state.get("compose_project"))


def _container_status(docker, name: str) -> Optional[str]:
    """Container status or None; tolerant of doubles without the method."""
    fn = getattr(docker, "container_status", None)
    if fn is None:
        return None
    try:
        return fn(name)
    except Exception:
        return None


def _target_own_ports(target: dict) -> set:
    """Every host port the target's own contract claims — excluded from
    registry-collision checks, since the target legitimately owns them."""
    from deployments.pipeline import run_spec_from_state
    ports: set = set()
    spec = run_spec_from_state(target)
    for host_port in (spec.get("ports") or {}):
        try:
            ports.add(int(host_port))
        except (TypeError, ValueError):
            continue
    for raw in target.get("compose_ports") or []:
        try:
            ports.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ports


def _current_container_names(docker, current: Optional[dict]) -> set:
    """Container names owned by the deployment being replaced.

    The explicit rollback path tears this deployment down in its
    ``before_restore`` hook, so its containers/ports must not read as a
    collision during verify (spec §5, same-port case). Covers both the
    single-container name and every container of a compose project
    (compose names containers ``<project>-<service>-<seq>``).
    """
    names: set = set()
    if not current:
        return names
    cname = current.get("container_name")
    if cname:
        names.add(cname)
    project = current.get("compose_project")
    if project:
        names.update(_compose_stack_container_names(docker, project))
    return names


def _compose_stack_container_names(docker, project: str) -> set:
    """Every container name belonging to a compose project.

    From ``compose ps`` records first, plus a ``docker ps -a`` prefix
    scan (same rule as ``reconcile._compose_name_matches``) in case the
    compose CLI reports nothing. Tolerant of doubles without the method.
    """
    names: set = set()
    try:
        records = docker.compose_ps(project) or []
    except Exception:
        records = []
    for rec in records:
        n = rec.get("Name") or rec.get("ID")
        if n:
            names.add(str(n).lstrip("/"))
    try:
        rows = docker.ps(all=True) or []
    except Exception:
        rows = []
    for row in rows or []:
        n = (row.get("Names") or "").lstrip("/")
        if n == project or n.startswith(project + "-"):
            names.add(n)
    return names


def _current_recorded_ports(current: Optional[dict]) -> set:
    """Host ports the deployment being replaced has recorded.

    Mirrors ``DeploymentStore.used_host_ports`` (host_port + compose
    ports): these registry entries belong to the deployment the rollback
    is tearing down, so they must not read as a collision against the
    target (spec §5, same-port case).
    """
    ports: set = set()
    if not current:
        return ports
    for raw in [current.get("host_port")] + list(
            current.get("compose_ports") or []):
        try:
            ports.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ports


def _mark_target_phase(store, target_id: str, phase: str, log) -> None:
    """Record a transient rollback phase on the target row (spec §6).

    Never fails the rollback: a marker write is best-effort. A worker
    crash between markers leaves a transient phase the next reconcile
    pass detects and marks failed loudly instead of silently
    resurrecting the torn-down deployment.
    """
    try:
        row = store.load(target_id)
    except Exception as exc:
        log(f"rollback: could not read target row for phase {phase!r}: "
            f"{exc}")
        return
    if row is None:
        return
    try:
        row["rollback_status"] = phase
        store.save(row)
    except Exception as exc:
        log(f"rollback: could not record phase {phase!r}: {exc}")


def _verify_container_ports_free(docker, target: dict, target_id,
                                 log, current: Optional[dict] = None
                                 ) -> None:
    """The target's recorded host ports must be free before teardown.

    The target's own (stopped) container and the deployment being
    replaced (``current`` — torn down by the caller's ``before_restore``
    hook) are excluded: in the same-port case the current deployment
    legitimately binds the target's ports until its teardown, so a port
    bound ONLY by it is treated as free-after-teardown. A port held by
    anything else fails the rollback loudly.
    """
    from deployments.pipeline import run_spec_from_state, verify_host_port_free
    spec = run_spec_from_state(target)
    own = {target["container_name"]} if target.get("container_name") else set()
    tolerated = _current_container_names(docker, current)
    for host_port in (spec.get("ports") or {}):
        try:
            verify_host_port_free(docker, int(host_port), log=log,
                                  exclude_names=own | tolerated,
                                  tolerate_bound_by=tolerated)
        except Exception as exc:
            raise RollbackError(
                f"rollback target {target_id} cannot be restored: host port "
                f"{host_port} is not free ({exc}); refusing to tear down "
                f"the current deployment"
            ) from exc


def verify_target_restorable(docker, target: dict, log=None,
                             registry_used: Optional[set] = None,
                             current: Optional[dict] = None) -> None:
    """Fail fast (before the current deployment is touched) when the
    rollback target cannot be brought back up. Raises RollbackError.

    A container target is restorable when its container still exists, or
    when it can be rebuilt from the persisted contract (image recorded
    and present). A compose target needs its compose file. In both cases
    the target's recorded host ports must be free (spec §21: host
    availability verified) — a port squatted by another container would
    make the restore fail AFTER the healthy current deployment was
    destroyed, so the rollback is refused loudly instead. The target's
    own (stopped) container/stack — and the ports its own contract
    claims — are excluded from the collision check: ``docker start``
    reuses its existing port mapping.

    ``current`` (optional): the deployment being replaced. Its
    containers and recorded ports are excluded from the collision check
    as well (spec §5, same-port case): its teardown frees them before
    the restore, so they must not read as a collision. A port held by
    anything else still fails the rollback.
    """
    from deployments.pipeline import (run_spec_from_state,
                                      verify_compose_ports_free)
    log = log or (lambda line: None)
    target_id = target.get("deployment_id")
    if target.get("container_name"):
        name = target["container_name"]
        if docker.container_exists(name):
            # A target whose container is already running needs no port
            # check: `docker start` is a no-op and the port is
            # legitimately held by the target itself.
            if _container_status(docker, name) != "running":
                _verify_container_ports_free(docker, target, target_id, log,
                                             current=current)
            return
        # GC'd generation: rebuildable only when the persisted contract's
        # image is still available.
        spec_image = target.get("image") or run_spec_from_state(target)["image"]
        if spec_image and docker.image_exists(spec_image):
            _verify_container_ports_free(docker, target, target_id, log,
                                         current=current)
            return
        raise RollbackError(
            f"rollback target {target_id} cannot be restored: container "
            f"{name} is gone (beyond the GC window) and image "
            f"{spec_image!r} is unavailable; redeploy required"
        )
    if target.get("compose_project"):
        compose_file = target.get("compose_file")
        if not (compose_file and Path(compose_file).is_file()):
            raise RollbackError(
                f"rollback target {target_id} cannot be restored: compose "
                f"file {compose_file!r} is missing; redeploy required"
            )
        ports = []
        for raw in target.get("compose_ports") or []:
            try:
                ports.append(int(raw))
            except (TypeError, ValueError):
                continue
        # The target's own claimed ports are legitimately its own (the
        # automatic path rolls back while the target row is still
        # "running", so the registry still lists them) — and so are the
        # current deployment's (spec §5: its teardown frees them).
        reg = (set(registry_used or set()) - _target_own_ports(target)
               - _current_recorded_ports(current))
        current_names = _current_container_names(docker, current)
        try:
            verify_compose_ports_free(
                docker, target["compose_project"], ports, log=log,
                registry_used=reg,
                exclude_names=(
                    _compose_stack_container_names(
                        docker, target["compose_project"])
                    | current_names),
                tolerate_bound_by=current_names)
        except Exception as exc:
            raise RollbackError(
                f"rollback target {target_id} cannot be restored: {exc}; "
                f"refusing to tear down the current deployment"
            ) from exc
        return
    raise RollbackError(
        f"rollback target {target_id} has nothing to restore"
    )


def restore_target(docker, target: dict, log=None) -> str:
    """Bring a rollback target back up: ``docker start`` for container
    deployments, ``docker compose up`` with the stored compose file for
    compose deployments, or a rebuild from the persisted deployment
    contract when the container was garbage-collected but its image
    survives.

    Scoped strictly to the target's own recorded names — never by
    project-prefix matching. Raises RollbackError when the target cannot
    be restored. Returns how the target was brought back:
    ``"started" | "rebuilt" | "compose_up"``.
    """
    from deployments.pipeline import run_spec_from_state
    log = log or (lambda line: None)
    target_id = target.get("deployment_id")
    if target.get("container_name"):
        name = target["container_name"]
        if docker.container_exists(name):
            # `docker start` on an already-running container is a no-op
            # success at the Docker level; calling it unconditionally
            # keeps the historical behavior of both rollback paths.
            log(f"rollback: starting container {name}")
            docker.start(name)
            return "started"
        # GC'd generation: rebuild from the stored contract, exactly like
        # reconcile does.
        spec = run_spec_from_state(target)
        image = spec["image"]
        if not image or not docker.image_exists(image):
            raise RollbackError(
                f"rollback target {target_id} container {name} is gone and "
                f"its image {image!r} is unavailable; redeploy required"
            )
        env = dict(target.get("env") or {})  # non-secret env only, as in reconcile
        docker.run(name, image, ports=spec["ports"], env=env or None,
                   memory=spec["memory"], cpus=spec["cpus"],
                   restart=spec["restart"])
        log(f"rollback target container {name} was GC'd; rebuilt from "
            f"image {image} (non-secret env only)")
        return "rebuilt"
    if target.get("compose_project"):
        compose_file = target.get("compose_file")
        if not compose_file or not os.path.isfile(compose_file):
            raise RollbackError(
                f"rollback target {target_id} is a compose deployment but "
                f"its compose file {compose_file!r} is missing; "
                f"redeploy required"
            )
        log(f"rollback: restoring compose stack {target['compose_project']} "
            f"from {compose_file}")
        docker.compose_up(compose_file,
                          project_name=target["compose_project"], build=True)
        return "compose_up"
    raise RollbackError(
        f"rollback target {target_id} has nothing to restore"
    )


def healthcheck_restored_target(docker, target: dict, timeout_secs: int = 60,
                                log=None):
    """Health-check the rollback target after it is restored (spec §21).

    Uses the same semantics as a normal deployment's health check
    (``health.checker.wait_for_healthcheck`` on the target's recorded
    host port and healthcheck path). Returns True (healthy), False
    (unhealthy) or None (no host port could be determined — nothing to
    check against). Never raises: the restore already happened, so an
    unhealthy target is reported honestly via its health_status rather
    than hidden behind an exception.
    """
    log = log or (lambda line: None)
    target_id = target.get("deployment_id")
    host_port = target.get("host_port")
    if not host_port and target.get("compose_project"):
        # Compose targets record their ports in compose_ports; fall back
        # to discovering the live published port.
        from deployments import pipeline as pipeline_mod
        discover = getattr(pipeline_mod, "_discover_compose_port", None)
        if discover is not None:
            host_port = discover(docker, target["compose_project"], log)
    if not host_port:
        log(f"rollback target {target_id}: no host port recorded; "
            f"skipping health check")
        return None
    path = target.get("healthcheck_path") or "/"
    log(f"rollback healthcheck: GET :{host_port}{path} "
        f"(timeout {timeout_secs}s)")
    try:
        ok = health_checker.wait_for_healthcheck(
            int(host_port), path, timeout_secs=timeout_secs, log=log)
    except Exception as exc:
        log(f"rollback target healthcheck error: {exc}")
        return False
    log(f"rollback target health: {'healthy' if ok else 'UNHEALTHY'}")
    return ok


def settle_rollback_ports(ctx, log, deployment_id: str,
                          target_deployment_id: str) -> dict:
    """Port-registry settle after a completed rollback, via the EXISTING
    control-plane API (``POST /v1/deployments/:id/settle-rollback-ports``).

    Asks the control plane to release the rolled-back deployment's port
    reservations and re-reserve the rollback target's host ports for the
    target deployment (its reservation was dropped when the newer
    deployment superseded it). Uses the API client's public session and
    base_url attributes — no new client method, no new protocol.

    Never raises: the registry is advisory and the worker's physical
    port checks are the backstop. A settle failure never reports
    success — it is returned in the settle outcome (``settled: False`` +
    reason) so the caller records it durably (``rollback_failed`` /
    ``partially_reconciled``) instead of silently claiming success.
    Returns ``{"settled": bool, ...}`` with the server-reported
    released/restored/skipped counts (or a ``"reason"`` when
    skipped/failed).
    """
    log = log or (lambda line: None)
    outcome = {"settled": False, "released": None, "restored": None,
               "skipped": None, "reason": None}
    api = getattr(ctx, "api", None)
    session = getattr(api, "session", None) if api is not None else None
    base_url = getattr(api, "base_url", None) if api is not None else None
    if session is None or not base_url:
        outcome["reason"] = "no control-plane session"
        log("rollback port settle skipped: no control-plane session")
        return outcome
    url = (f"{base_url}/v1/deployments/{deployment_id}/"
           f"settle-rollback-ports")
    try:
        resp = session.post(
            url, json={"target_deployment_id": target_deployment_id},
            timeout=30)
    except Exception as exc:
        outcome["reason"] = f"POST failed: {exc}"
        log(f"rollback port settle failed (non-fatal): {exc}")
        return outcome
    if resp.status_code != 200:
        outcome["reason"] = f"HTTP {resp.status_code}"
        log(f"rollback port settle returned HTTP {resp.status_code} "
            f"(non-fatal)")
        return outcome
    try:
        body = resp.json()
    except ValueError:
        body = {}
    outcome.update({"settled": True,
                    "released": body.get("released"),
                    "restored": body.get("restored"),
                    "skipped": body.get("skipped")})
    log(f"rollback ports settled: released={body.get('released')} "
        f"restored={body.get('restored')} skipped={body.get('skipped')}")
    return outcome


def perform_rollback(ctx, docker, *, log: Callable[[str], None],
                     target: dict, healthcheck_timeout: int = 60,
                     verify: bool = True,
                     before_restore: Optional[Callable[[], None]] = None,
                     current: Optional[dict] = None) -> dict:
    """Execute the full rollback against ``target`` — the ONE
    implementation both the automatic (deploy-time) and explicit
    (handler) rollback paths call.

    Phases: prepare/verify-target -> [before_restore hook] -> restore ->
    health-check-target (real) -> commit state.

    * ``verify``: when True, the target's restorability is checked
      BEFORE anything is torn down; a bad target raises RollbackError
      leaving the healthy deployment untouched (verify-before-destroy).
    * ``before_restore``: optional callable invoked after a successful
      verify and before restore. Explicit rollback passes its teardown
      of the current deployment here, so a verified target never
      competes with the deployment it replaces for ports.
    * ``current``: optional state of the deployment being replaced.
      Used ONLY by the verify phase (spec §5, same-port case): its
      containers and recorded ports are excluded from the collision
      check because ``before_restore`` frees them before the restore.
      The automatic path leaves it None — the failed deployment was
      already torn down.

    The transient phases (``restoring``, ``health-checking``) are
    recorded durably on the target row (spec §6): a worker crash
    mid-rollback leaves a marker the next reconcile pass detects instead
    of silently resurrecting the torn-down deployment.

    Commits the target's state row (``status="running"``,
    ``health_status`` from the REAL health check — never assumed) and
    returns an outcome dict::

        {"rolled_back_to": <id>,
         "target_health_status": "healthy" | "unhealthy" | "unknown",
         "restored_via": "started" | "rebuilt" | "compose_up"}

    The caller owns everything else: the automatic path records the
    failed deployment's row and raises DeployError; the explicit path
    marks the current deployment rolled_back and settles port
    reservations with the control plane (see settle_rollback_ports).
    Neither caller may report success unless the target was proven
    healthy — an unhealthy OR unknown target persists rollback_failed,
    never a false "rolled_back".
    """
    store = ctx.deployment_store
    target_id = target.get("deployment_id")
    # -- prepare: work from the freshest persisted state for the target --
    fresh = store.load(target_id) or dict(target)
    # -- verify-target (before anything is torn down) ----------------------
    if verify:
        log(f"rollback: verifying target {target_id} is restorable")
        verify_target_restorable(docker, fresh, log=log,
                                 registry_used=store.used_host_ports(),
                                 current=current)
        log(f"rollback: target {target_id} verified restorable")
    _mark_target_phase(store, target_id, "restoring", log)
    if before_restore is not None:
        before_restore()
    # -- restore ------------------------------------------------------------
    restored_via = restore_target(docker, fresh, log=log)
    # -- health-check-target (REAL; never raises) ---------------------------
    _mark_target_phase(store, target_id, "health-checking", log)
    health = healthcheck_restored_target(
        docker, fresh, timeout_secs=healthcheck_timeout, log=log)
    # -- commit state: health_status comes from the real check result ------
    committed = store.load(target_id) or dict(fresh)
    committed["status"] = "running"
    if health is True:
        committed["health_status"] = "healthy"
        committed["rollback_status"] = "succeeded"
    elif health is False:
        committed["health_status"] = "unhealthy"
        committed["rollback_status"] = "failed"
    else:
        # No host port could be checked against (e.g. the target was
        # restored but exposes no health-checkable endpoint): its health
        # is UNKNOWN — and unknown is never a success. Persist
        # rollback_failed honestly; the caller must not report
        # "rolled_back". (The stale pre-rollback value is overwritten on
        # purpose: it described the target before the restore, not now.)
        committed["health_status"] = "unknown"
        committed["rollback_status"] = "failed"
    store.save(committed)
    log(f"rollback complete: {target_id} restored via {restored_via}, "
        f"health_status={committed['health_status']}")
    return {"rolled_back_to": target_id,
            "target_health_status": committed["health_status"],
            "restored_via": restored_via}
