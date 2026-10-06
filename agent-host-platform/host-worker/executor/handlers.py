"""One handler per protocol task type (PROTOCOL §5).

Every handler has the signature ``handler(ctx, task) -> dict`` where
``ctx`` is a WorkerContext and ``task`` is the claimed task dict from the
control plane. Handlers build their own argv lists and never use
shell=True; anything outside the allowlist in agent.policy never reaches
here.

Filesystem paths taken from payloads are confined under the worker's
work_dir / apps_dir (no absolute escapes, no ".." traversal).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from deployments import pipeline as deploy_pipeline
from ingress import sync as ingress_sync


class HandlerError(Exception):
    """A handler-level failure; the dispatcher reports the task as failed."""


def _payload(task: dict) -> dict:
    return task.get("payload") or {}


def _task_id(task: dict) -> str:
    return task.get("id", "unknown")


def _validated_artifact_id(payload: dict) -> str:
    """artifact_id from the payload, validated as a path component (§16).

    Callers build artifacts/<id>.bin paths from this value; an unvalidated
    id ("../../..") would steer makedirs/file writes outside the work dir.
    """
    try:
        return deploy_pipeline.validate_artifact_id(payload.get("artifact_id"))
    except ValueError as exc:
        raise HandlerError(str(exc)) from exc


def _require(payload: dict, *keys: str) -> None:
    missing = [k for k in keys if payload.get(k) in (None, "")]
    if missing:
        raise HandlerError(f"payload missing required fields: {missing}")


def _confined_path(base: Path, requested: str | None, what: str) -> Path:
    """Resolve `requested` strictly inside `base`; reject escapes."""
    base = base.resolve()
    target = (base / (requested or ".")).resolve()
    if target != base and base not in target.parents:
        raise HandlerError(f"{what} escapes the allowed directory: {requested!r}")
    return target


def _deployment_state(ctx, deployment_id: str) -> dict:
    state = ctx.deployment_store.load(deployment_id)
    if not state:
        raise HandlerError(f"unknown deployment_id: {deployment_id!r}")
    return state


def _restorable(state: dict) -> bool:
    """A deployment can be restored by rollback when it has a container or
    a compose stack recorded."""
    return bool(state.get("container_name") or state.get("compose_project"))


def _restore_deployment(ctx, docker, state: dict) -> None:
    """Bring a rollback target back up: `docker start` for container
    deployments, `docker compose up` with the stored compose file for
    compose deployments. Scoped strictly to the target's own recorded
    names — never by project-prefix matching.

    If the target's container was garbage-collected (older than the
    DEPLOY_KEEP_GENERATIONS window), fall back to rebuilding it from the
    persisted deployment contract, exactly like reconcile does. Callers
    must run _verify_target_restorable() BEFORE tearing down the current
    deployment so a failed rollback never leaves the project down.
    """
    from deployments.pipeline import run_spec_from_state
    task_id = "rollback"
    if state.get("container_name"):
        name = state["container_name"]
        if docker.container_exists(name):
            docker.start(name)
            return
        # GC'd generation: rebuild from the stored contract.
        spec = run_spec_from_state(state)
        image = spec["image"]
        if not image or not docker.image_exists(image):
            raise HandlerError(
                f"rollback target {state.get('deployment_id')} container "
                f"{name} is gone and its image {image!r} is unavailable; "
                f"redeploy required"
            )
        env = dict(state.get("env") or {})  # non-secret env only, as in reconcile
        docker.run(name, image, ports=spec["ports"], env=env or None,
                   memory=spec["memory"], cpus=spec["cpus"],
                   restart=spec["restart"])
        ctx.log(task_id,
                f"rollback target container {name} was GC'd; rebuilt from "
                f"image {image} (non-secret env only)")
    elif state.get("compose_project"):
        compose_file = state.get("compose_file")
        if not compose_file or not Path(compose_file).is_file():
            raise HandlerError(
                f"rollback target {state.get('deployment_id')} is a compose "
                f"deployment but its compose file {compose_file!r} is "
                f"missing; redeploy required"
            )
        docker.compose_up(compose_file,
                          project_name=state["compose_project"], build=True)
    else:  # pragma: no cover — callers check _restorable first
        raise HandlerError(
            f"rollback target {state.get('deployment_id')} has nothing to restore"
        )


def _docker_status(docker, name):
    """Container status or None; tolerant of doubles without the method."""
    fn = getattr(docker, "container_status", None)
    if fn is None:
        return None
    try:
        return fn(name)
    except Exception:
        return None


def _verify_target_restorable(ctx, docker, target: dict) -> None:
    """Fail fast (before the current deployment is touched) when the
    rollback target cannot be brought back up.

    A container target is restorable when its container still exists, or
    when it can be rebuilt from the persisted contract (image recorded and
    present). A compose target needs its compose file. In both cases the
    target's recorded host ports must be free (spec §21: host availability
    verified) — a port squatted by another container would make the restore
    fail AFTER the healthy current deployment was destroyed, so the
    rollback is refused loudly instead. The target's own (stopped)
    container/stack is excluded from the collision check: `docker start`
    reuses its existing port mapping.
    """
    from deployments.pipeline import run_spec_from_state
    target_id = target.get("deployment_id")
    if target.get("container_name"):
        name = target["container_name"]
        if docker.container_exists(name):
            # A target whose container is already running needs no port
            # check: `docker start` is a no-op and the port is legitimately
            # held by the target itself.
            if _docker_status(docker, name) != "running":
                _verify_target_ports_free(ctx, docker, target, target_id)
            return
        spec = run_spec_from_state(target)
        image = spec["image"]
        if image and docker.image_exists(image):
            _verify_target_ports_free(ctx, docker, target, target_id)
            return
        raise HandlerError(
            f"rollback target {target_id} cannot be restored: container "
            f"{name} is gone (beyond the GC window) and image {image!r} is "
            f"unavailable; redeploy required"
        )
    if target.get("compose_project"):
        compose_file = target.get("compose_file")
        if not (compose_file and Path(compose_file).is_file()):
            raise HandlerError(
                f"rollback target {target_id} cannot be restored: compose file "
                f"{compose_file!r} is missing; redeploy required"
            )
        _verify_target_compose_ports_free(ctx, docker, target, target_id)
        return
    raise HandlerError(  # pragma: no cover — callers check _restorable first
        f"rollback target {target_id} has nothing to restore"
    )


def _verify_target_ports_free(ctx, docker, target: dict, target_id) -> None:
    """The target's recorded host ports must be free before teardown."""
    from deployments.pipeline import run_spec_from_state, verify_host_port_free
    spec = run_spec_from_state(target)
    own = {target["container_name"]} if target.get("container_name") else set()
    for host_port in (spec.get("ports") or {}):
        try:
            verify_host_port_free(docker, int(host_port),
                                  exclude_names=own)
        except Exception as exc:
            raise HandlerError(
                f"rollback target {target_id} cannot be restored: host port "
                f"{host_port} is not free ({exc}); refusing to tear down "
                f"the current deployment"
            ) from exc


def _verify_target_compose_ports_free(ctx, docker, target: dict,
                                      target_id) -> None:
    """The target compose stack's declared ports must be free before teardown."""
    from deployments.pipeline import verify_compose_ports_free
    ports = []
    for p in target.get("compose_ports") or []:
        try:
            ports.append(int(p))
        except (TypeError, ValueError):
            continue
    try:
        verify_compose_ports_free(
            docker, target["compose_project"], ports,
            registry_used=ctx.deployment_store.used_host_ports())
    except Exception as exc:
        raise HandlerError(
            f"rollback target {target_id} cannot be restored: {exc}; "
            f"refusing to tear down the current deployment"
        ) from exc


def _healthcheck_restored_target(ctx, docker, target: dict, payload: dict,
                                 task_id: str):
    """Health-check the rollback target after it is restored (spec §21).

    Returns True (healthy), False (unhealthy) or None (no host port
    recorded — nothing to check against). Never raises: the restore
    already happened, so an unhealthy target is reported honestly via its
    health_status rather than hidden behind a task failure.
    """
    from health import checker as health_checker
    host_port = target.get("host_port")
    if not host_port:
        return None
    path = target.get("healthcheck_path") or "/"
    timeout = int(payload.get("healthcheck_timeout", 60))
    ctx.log(task_id,
            f"rollback healthcheck: GET :{host_port}{path} "
            f"(timeout {timeout}s)")
    try:
        ok = health_checker.wait_for_healthcheck(
            int(host_port), path, timeout_secs=timeout,
            log=lambda line: ctx.log(task_id, line))
    except Exception as exc:
        ctx.log(task_id, f"rollback target healthcheck error: {exc}")
        return False
    ctx.log(task_id,
            f"rollback target health: {'healthy' if ok else 'UNHEALTHY'}")
    return ok


def _teardown_current(ctx, docker, state: dict) -> None:
    """Stop/remove the current deployment's own containers, scoped to its
    recorded names only."""
    if state.get("container_name"):
        docker.stop(state["container_name"])
        docker.rm(state["container_name"], force=True)
    elif state.get("compose_project"):
        compose_file = state.get("compose_file")
        if compose_file and Path(compose_file).is_file():
            docker.compose_down(compose_file,
                                project_name=state["compose_project"])


def _container_name(ctx, deployment_id: str) -> str:
    state = _deployment_state(ctx, deployment_id)
    name = state.get("container_name")
    if not name:
        raise HandlerError(
            f"deployment {deployment_id} has no container "
            f"(status={state.get('status')})"
        )
    return name


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------
def handle_deploy(ctx, task: dict) -> dict:
    result = deploy_pipeline.deploy(ctx, task)
    _ingress_after_change(ctx, _task_id(task), result.get("deployment_id"))
    return result


def _ingress_after_change(ctx, task_id: str, deployment_id) -> None:
    """Best-effort in-process ingress sync after a successful deploy/remove.

    Runs only when ingress is enabled on this host; never raises, so it
    cannot fail the task that triggered it.
    """
    if not deployment_id:
        return
    try:
        outcome = ingress_sync.maybe_sync_on_change(ctx, deployment_id)
    except Exception as exc:  # pragma: no cover - defensive
        ctx.log(task_id, f"ingress sync hook failed: {exc}")
        return
    if outcome is not None:
        ctx.log(task_id,
                f"ingress sync after change: {outcome.get('status')} "
                f"({len(outcome.get('routes', []))} routes)")


# ---------------------------------------------------------------------------
# lifecycle: restart | stop | start | remove | rollback
# ---------------------------------------------------------------------------
def handle_restart(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    name = _container_name(ctx, payload["deployment_id"])
    ctx.log(_task_id(task), f"restarting container {name}")
    docker.restart_container(name)
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": "restarted"}


def handle_stop(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    name = _container_name(ctx, payload["deployment_id"])
    ctx.log(_task_id(task), f"stopping container {name}")
    docker.stop(name)
    state = ctx.deployment_store.load(payload["deployment_id"])
    state["status"] = "stopped"
    ctx.deployment_store.save(state)
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": "stopped"}


def handle_start(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    name = _container_name(ctx, payload["deployment_id"])
    ctx.log(_task_id(task), f"starting container {name}")
    docker.start(name)
    state = ctx.deployment_store.load(payload["deployment_id"])
    state["status"] = "running"
    ctx.deployment_store.save(state)
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": "running"}


def handle_remove(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    state = _deployment_state(ctx, payload["deployment_id"])
    name = state.get("container_name")
    if name:
        ctx.log(_task_id(task), f"removing container {name}")
        docker.stop(name)
        docker.rm(name, force=True)
    if state.get("compose_project"):
        # Prefer the compose file recorded at deploy time (it lives under
        # the deployment's own state dir); fall back to a payload-supplied
        # path for deployments recorded before Phase 4.
        compose_file = state.get("compose_file") or payload.get("compose_file")
        if compose_file:
            base = Path(ctx.config.work_dir).resolve()
            cf = _confined_path(base, compose_file, "compose_file")
            docker.compose_down(str(cf), project_name=state["compose_project"])
        else:
            ctx.log(_task_id(task),
                    f"compose deployment {payload['deployment_id']} has no "
                    f"compose file recorded; skipping compose down")
    state["status"] = "removed"
    state["container_name"] = None
    ctx.deployment_store.save(state)
    _ingress_after_change(ctx, _task_id(task), payload["deployment_id"])
    return {"deployment_id": payload["deployment_id"], "status": "removed"}


def handle_rollback(ctx, task: dict) -> dict:
    """Roll back to a previous deployment of the same project.

    The control plane's POST /v1/deployments/:id/rollback names the target
    explicitly (payload.target_deployment_id — the last healthy deployment
    of the same project+host). When absent (a hand-built type=rollback
    task), fall back to the newest non-current local deployment with a
    container in a restorable state.
    """
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    current = _deployment_state(ctx, payload["deployment_id"])
    project_id = current.get("project_id")

    target = None
    target_id = payload.get("target_deployment_id")
    if target_id:
        candidate = ctx.deployment_store.load(target_id)
        if candidate is None:
            raise HandlerError(
                f"rollback target deployment {target_id} not found in the "
                f"local registry"
            )
        if candidate.get("project_id") != project_id:
            raise HandlerError(
                f"rollback target {target_id} belongs to a different project"
            )
        if not _restorable(candidate):
            raise HandlerError(
                f"rollback target {target_id} has no container or compose "
                f"stack recorded"
            )
        ctx.log(_task_id(task),
                f"rollback target (control plane): {target_id}")
        target = candidate
    else:
        candidates = [
            s for s in ctx.deployment_store.list_all()
            if s.get("project_id") == project_id
            and s.get("deployment_id") != current["deployment_id"]
            and _restorable(s)
            and s.get("status") in ("running", "superseded", "stopped")
        ]
        if not candidates:
            raise HandlerError(
                f"no rollback candidate for project {current.get('project_name')}"
            )
        target = candidates[0]  # list_all is newest-first
    ctx.log(_task_id(task),
            f"rolling back {current['deployment_id']} -> {target['deployment_id']}")
    # Verify BEFORE teardown: a rollback must never destroy the healthy
    # current deployment when the target cannot be restored (e.g. its
    # container was garbage-collected beyond the keep-generations window)
    # or when the target's host ports are no longer free.
    _verify_target_restorable(ctx, docker, target)
    _teardown_current(ctx, docker, current)
    _restore_deployment(ctx, docker, target)
    # Spec §21: the restored target is health-checked before it is
    # promoted. An unhealthy target is reported honestly (health_status)
    # rather than hidden behind a task failure — the restore happened.
    target_health = _healthcheck_restored_target(
        ctx, docker, target, payload, _task_id(task))
    current["status"] = "rolled_back"
    ctx.deployment_store.save(current)
    target["status"] = "running"
    target["health_status"] = (
        "healthy" if target_health else
        "unhealthy" if target_health is False else
        target.get("health_status", "unknown"))
    ctx.deployment_store.save(target)
    ports_settled = _settle_rollback_ports(
        ctx, _task_id(task), current["deployment_id"], target["deployment_id"])
    return {"deployment_id": current["deployment_id"], "status": "rolled_back",
            "rolled_back_to": target["deployment_id"],
            "target_health_status": target["health_status"],
            "ports_settled": ports_settled}


def _settle_rollback_ports(ctx, task_id: str, deployment_id: str,
                           target_deployment_id: str) -> bool:
    """Best-effort port-registry settle after a completed rollback.

    Tells the control plane to release the rolled-back deployment's port
    reservations and re-reserve the rollback target's host ports for the
    target deployment (its reservation was dropped when the newer
    deployment superseded it). Uses the API client's public session and
    base_url attributes — no new client method (agent.api is owned
    elsewhere). Never raises: the registry is advisory and the worker's
    physical port checks are the backstop, so a settle failure must not
    fail an already-completed rollback.
    """
    api = getattr(ctx, "api", None)
    session = getattr(api, "session", None) if api is not None else None
    base_url = getattr(api, "base_url", None) if api is not None else None
    if session is None or not base_url:
        ctx.log(task_id,
                "rollback port settle skipped: no control-plane session")
        return False
    url = (f"{base_url}/v1/deployments/{deployment_id}/"
           f"settle-rollback-ports")
    try:
        resp = session.post(
            url, json={"target_deployment_id": target_deployment_id},
            timeout=30)
    except Exception as exc:
        ctx.log(task_id, f"rollback port settle failed (non-fatal): {exc}")
        return False
    if resp.status_code != 200:
        ctx.log(task_id,
                f"rollback port settle returned HTTP {resp.status_code} "
                f"(non-fatal)")
        return False
    try:
        body = resp.json()
    except ValueError:
        body = {}
    ctx.log(task_id,
            f"rollback ports settled: released={body.get('released')} "
            f"restored={body.get('restored')} skipped={body.get('skipped')}")
    return True


# ---------------------------------------------------------------------------
# introspection: logs | status | healthcheck | system-info
# ---------------------------------------------------------------------------
def handle_logs(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    name = _container_name(ctx, payload["deployment_id"])
    tail = int(payload.get("tail", 500))
    tail = max(1, min(tail, 5000))
    logs = docker.logs(name, tail=tail)
    return {"deployment_id": payload["deployment_id"], "logs": logs}


def handle_status(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    name = _container_name(ctx, payload["deployment_id"])
    status = docker.container_status(name)
    info = docker.inspect(name)
    ports = {}
    try:
        bindings = (info[0].get("NetworkSettings") or {}).get("Ports") or {}
        for cport, binds in bindings.items():
            if binds:
                ports[cport] = binds[0].get("HostPort")
    except (IndexError, AttributeError):
        pass
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": status, "ports": ports}


def handle_healthcheck(ctx, task: dict) -> dict:
    from health import checker as health_checker
    payload = _payload(task)
    _require(payload, "deployment_id")
    state = _deployment_state(ctx, payload["deployment_id"])
    host_port = state.get("host_port")
    if not host_port:
        raise HandlerError(
            f"deployment {payload['deployment_id']} exposes no host port")
    path = payload.get("path") or state.get("healthcheck_path") or "/"
    ok = health_checker.wait_for_healthcheck(
        int(host_port), path,
        timeout_secs=int(payload.get("timeout", 60)),
        log=lambda line: ctx.log(_task_id(task), line))
    return {"deployment_id": payload["deployment_id"],
            "health_status": "healthy" if ok else "unhealthy"}


def handle_system_info(ctx, task: dict) -> dict:
    from health import collector as health_collector
    info = health_collector.collect_metrics(
        docker_client=ctx.docker,
        deployment_store=ctx.deployment_store,
        config=ctx.config,
    )
    info["host_name"] = ctx.config.host_name
    info["worker_version"] = ctx.config.worker_version
    return info


# ---------------------------------------------------------------------------
# build | docker-build
# ---------------------------------------------------------------------------
def handle_build(ctx, task: dict) -> dict:
    """Download an artifact and build a docker image (no run)."""
    payload = _payload(task)
    _require(payload, "project_id", "artifact_id")
    artifact_id = _validated_artifact_id(payload)
    docker = ctx.require_docker()
    task_id = _task_id(task)

    expected_checksum = payload.get("artifact_checksum")
    if not expected_checksum:
        raise HandlerError("refusing to build: no artifact_checksum in payload")
    work_dir = Path(ctx.config.work_dir)
    artifacts_dir = work_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dest = str(artifacts_dir / f"{artifact_id}.bin")
    ctx.log(task_id, f"downloading artifact {artifact_id}")
    ctx.api.download_artifact(
        artifact_id, dest, expected_size=payload.get("artifact_size"))
    if not deploy_pipeline.verify_checksum(dest, expected_checksum):
        quarantine = work_dir / deploy_pipeline.QUARANTINE_DIRNAME
        quarantine.mkdir(parents=True, exist_ok=True)
        qpath = quarantine / f"{artifact_id}-{int(time.time())}.bin"
        import shutil
        shutil.move(dest, qpath)
        raise HandlerError(
            f"artifact checksum mismatch; quarantined at {qpath}")
    build_dir = str(work_dir / "builds" / task_id)
    deploy_pipeline.extract_archive(dest, build_dir)

    dockerfile = payload.get("dockerfile") or "Dockerfile"
    df_path = _confined_path(Path(build_dir), dockerfile, "dockerfile")
    if not df_path.is_file():
        raise HandlerError(f"Dockerfile not found in artifact: {dockerfile}")
    context = _confined_path(Path(build_dir), payload.get("context") or ".", "context")
    tag = payload.get("tag") or (
        f"uaht-build-{payload['project_id'][:8]}:{int(time.time())}")
    ctx.log(task_id, f"docker build tag={tag}")
    docker.build(str(context), str(df_path), tag,
                 timeout=int(payload.get("build_timeout", 1200)))
    return {"image": tag, "status": "built"}


def handle_docker_build(ctx, task: dict) -> dict:
    return handle_build(ctx, task)


# ---------------------------------------------------------------------------
# docker-run | docker-compose
# ---------------------------------------------------------------------------
def handle_docker_run(ctx, task: dict) -> dict:
    """Run a container from an already-present image (no build)."""
    payload = _payload(task)
    _require(payload, "image", "name")
    docker = ctx.require_docker()
    from docker.client import sanitize_ident
    name = sanitize_ident(payload["name"])
    image = str(payload["image"])
    ports = {}
    for mapping in payload.get("ports", []) or []:
        # each mapping: "host:container" or {"host": h, "container": c}
        if isinstance(mapping, dict):
            ports[int(mapping["host"])] = int(mapping["container"])
        else:
            host, _, container = str(mapping).partition(":")
            ports[int(host)] = int(container)
    env = deploy_pipeline.sanitize_env(
        {**(payload.get("env") or {}), **(payload.get("secrets") or {})})
    if docker.container_exists(name):
        ctx.log(_task_id(task), f"removing existing container {name}")
        docker.stop(name)
        docker.rm(name, force=True)
    ctx.log(_task_id(task), f"docker run name={name} image={image}")
    # NOTE (security, 2026-10-05): payload 'extra_args' was removed from the
    # protocol — it allowed arbitrary docker flags (-> host root). Policy
    # rejects any docker-run task carrying it before this handler runs, so
    # it is never forwarded here.
    cid = docker.run(
        name, image, ports=ports, env=env,
        memory=payload.get("memory"), cpus=payload.get("cpus"),
        restart=payload.get("restart", "unless-stopped"),
    )
    return {"container": name, "container_id": cid, "status": "running",
            "ports": ports}


def handle_docker_compose(ctx, task: dict) -> dict:
    payload = _payload(task)
    docker = ctx.require_docker()
    if not docker.compose_available():
        raise HandlerError("'docker compose' is unavailable on this host")
    base = Path(ctx.config.work_dir).resolve()
    compose_file = _confined_path(
        base, payload.get("compose_file"), "compose_file")
    if not compose_file.is_file():
        raise HandlerError(f"compose file not found: {payload.get('compose_file')!r}")
    project_name = payload.get("project_name") or f"uaht-task-{_task_id(task)[:8]}"
    # W5/W7: validate the normalized compose model against the explicit
    # DENY list BEFORE `compose up`. A task-controlled compose file must
    # not smuggle privileged mode, host namespaces, host devices, added
    # capabilities, custom security options, or bind mounts escaping the
    # compose file's directory. Refusing here means zero docker side
    # effects for a malicious file.
    try:
        model = docker.compose_config_json(str(compose_file))
    except Exception as exc:
        raise HandlerError(
            f"cannot validate compose file: 'compose config' failed: {exc}")
    from deployments import compose_validate
    errors = compose_validate.validate_compose_model(
        model, workspace=compose_file.parent, compose_file=compose_file)
    if errors:
        raise HandlerError(
            "compose file rejected by security policy: " + "; ".join(errors))
    # Ports through the same registry-aware free-port verification the
    # deploy pipeline uses — no silent squatting on another deployment's
    # reserved port.
    wanted_ports = deploy_pipeline.parse_compose_published_ports(model)
    deploy_pipeline.verify_compose_ports_free(
        docker, project_name, wanted_ports,
        registry_used=ctx.deployment_store.used_host_ports())
    ctx.log(_task_id(task),
            f"docker compose up {compose_file} (security validation passed)")
    docker.compose_up(str(compose_file), project_name=project_name,
                      build=bool(payload.get("build")))
    return {"project_name": project_name, "compose_file": str(compose_file),
            "status": "running"}


# ---------------------------------------------------------------------------
# environment-update
# ---------------------------------------------------------------------------
def handle_environment_update(ctx, task: dict) -> dict:
    """Recreate a deployment's container with merged env (stop -> rm -> run).

    The replacement container is rebuilt from the deployment's persisted
    contract (deployments.pipeline.run_spec_from_state): restart policy,
    cpu/memory limits, image and port mapping are deployment-time facts
    and must survive an env update unchanged. They are NEVER re-derived
    from `docker inspect` or hard-coded defaults here.
    """
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    state = _deployment_state(ctx, payload["deployment_id"])
    name = state.get("container_name")
    if not name:
        raise HandlerError("deployment has no container to update")
    new_env = deploy_pipeline.sanitize_env(payload.get("env"))
    if not isinstance(new_env, dict) or not new_env:
        raise HandlerError("payload env must be a non-empty object")
    secret_env = deploy_pipeline.sanitize_env(payload.get("secrets"))
    merged = {**state.get("env", {}), **new_env, **secret_env}
    spec = deploy_pipeline.run_spec_from_state(state)
    image = spec["image"]
    if not image:
        # Pre-contract state with no image recorded: fall back to the
        # running container's image rather than failing the update.
        info = docker.inspect(name)[0]
        image = (info.get("Config") or {}).get("Image")
    if not image:
        raise HandlerError(
            f"deployment {payload['deployment_id']} has no image recorded "
            f"and none could be read from its container"
        )
    ctx.log(_task_id(task),
            f"recreating {name} with {len(merged)} env vars "
            f"(restart={spec['restart']}, memory={spec['memory']}, "
            f"cpus={spec['cpus']}, ports={spec['ports']})")
    docker.stop(name)
    docker.rm(name, force=True)
    docker.run(name, image, ports=spec["ports"], env=merged,
               memory=spec["memory"], cpus=spec["cpus"],
               restart=spec["restart"])
    state["env"] = {k: v for k, v in merged.items() if k not in secret_env}
    ctx.deployment_store.save(state)
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": "running", "updated_env_keys": sorted(new_env)}


# ---------------------------------------------------------------------------
# artifact-download | artifact-upload
# ---------------------------------------------------------------------------
def handle_artifact_download(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "artifact_id")
    artifact_id = _validated_artifact_id(payload)
    base = _confined_path(
        Path(ctx.config.apps_dir).resolve(), payload.get("destination"),
        "destination")
    dest = str(base / f"{artifact_id}.bin") \
        if base.is_dir() or not payload.get("destination") else str(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    ctx.log(_task_id(task), f"downloading artifact {artifact_id}")
    ctx.api.download_artifact(
        artifact_id, dest, expected_size=payload.get("artifact_size"))
    expected = payload.get("artifact_checksum")
    if expected and not deploy_pipeline.verify_checksum(dest, expected):
        os.remove(dest)
        raise HandlerError("downloaded artifact failed checksum verification")
    return {"artifact_id": artifact_id, "path": dest,
            "sha256": _sha256_file(dest)}


def _refuse_sensitive_path(ctx, path: Path, what: str) -> None:
    """Refuse to read files the worker must never exfiltrate.

    The artifact-upload handler's destination is already confined to
    work_dir, but worker.env (host token, control-plane URL) lives under
    <work_dir>/config/ — an agent task must not be able to upload it as
    an "artifact". Deny anything at or under the config directory, plus
    the configured worker.env path itself wherever it lives.
    """
    resolved = path.resolve()
    base = Path(ctx.config.work_dir).resolve()
    blocked = [base / "config"]
    config_path = getattr(ctx.config, "config_path", "") or ""
    if config_path:
        blocked.append(Path(config_path).resolve())
    for deny in blocked:
        if resolved == deny or deny in resolved.parents:
            raise HandlerError(
                f"refusing to read {what}: {resolved} is a sensitive "
                f"worker path")


def handle_artifact_upload(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "artifact_id")
    base = Path(ctx.config.work_dir).resolve()
    src = _confined_path(base, payload.get("destination"), "destination")
    _refuse_sensitive_path(ctx, src, "destination")
    if not src.is_file():
        raise HandlerError(f"upload source not found: {payload.get('destination')!r}")
    ctx.log(_task_id(task),
            f"uploading {src} as artifact {payload['artifact_id']}")
    digest = _sha256_file(str(src))
    resp = ctx.api.upload_artifact_content(payload["artifact_id"], str(src))
    return {"artifact_id": payload["artifact_id"], "sha256": digest,
            "server": resp}


# ---------------------------------------------------------------------------
# ingress-sync
# ---------------------------------------------------------------------------
def handle_ingress_sync(ctx, task: dict) -> dict:
    """Refresh the ingress provider's local route mirror from the current
    deployments' domains (see ingress/sync.py). Payload is empty — the
    sync pulls current state from the control plane and the local
    deployment store. Safe to re-run: it reconciles to the desired state.
    Note: this updates the non-authoritative local mirror only — routing
    for token-based tunnels is controlled remotely by the control plane.
    """
    task_id = _task_id(task)
    result = ingress_sync.sync_ingress(ctx)
    ctx.log(task_id,
            f"ingress sync: {result.get('status')} "
            f"({len(result.get('routes', []))} routes, "
            f"{len(result.get('skipped', []))} skipped)")
    return result


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
