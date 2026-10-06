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
from deployments import rollback as rollback_mod
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
    a compose stack recorded (see deployments.rollback.is_restorable)."""
    return rollback_mod.is_restorable(state)


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


def _compose_deployment(ctx, deployment_id: str):
    """(state, compose_project) for compose-runtime deployments.

    Returns (state, None) for single-container deployments. Lifecycle and
    introspection handlers branch on this so docker-compose deployments
    get the same restart/stop/start/logs/status coverage as docker ones
    (compose vs docker-run parity, spec §9).
    """
    state = _deployment_state(ctx, deployment_id)
    return state, state.get("compose_project")


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
    state, project = _compose_deployment(ctx, payload["deployment_id"])
    if project:
        ctx.log(_task_id(task), f"restarting compose project {project}")
        docker.compose_restart(project)
        return {"deployment_id": payload["deployment_id"],
                "compose_project": project, "status": "restarted"}
    name = state.get("container_name")
    if not name:
        raise HandlerError(
            f"deployment {payload['deployment_id']} has no container "
            f"(status={state.get('status')})"
        )
    ctx.log(_task_id(task), f"restarting container {name}")
    docker.restart_container(name)
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": "restarted"}


def handle_stop(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    state, project = _compose_deployment(ctx, payload["deployment_id"])
    if project:
        ctx.log(_task_id(task), f"stopping compose project {project}")
        docker.compose_stop(project)
        state["status"] = "stopped"
        ctx.deployment_store.save(state)
        return {"deployment_id": payload["deployment_id"],
                "compose_project": project, "status": "stopped"}
    name = state.get("container_name")
    if not name:
        raise HandlerError(
            f"deployment {payload['deployment_id']} has no container "
            f"(status={state.get('status')})"
        )
    ctx.log(_task_id(task), f"stopping container {name}")
    docker.stop(name)
    state["status"] = "stopped"
    ctx.deployment_store.save(state)
    return {"deployment_id": payload["deployment_id"], "container": name,
            "status": "stopped"}


def handle_start(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    state, project = _compose_deployment(ctx, payload["deployment_id"])
    if project:
        ctx.log(_task_id(task), f"starting compose project {project}")
        docker.compose_start(project)
        state["status"] = "running"
        ctx.deployment_store.save(state)
        return {"deployment_id": payload["deployment_id"],
                "compose_project": project, "status": "running"}
    name = state.get("container_name")
    if not name:
        raise HandlerError(
            f"deployment {payload['deployment_id']} has no container "
            f"(status={state.get('status')})"
        )
    ctx.log(_task_id(task), f"starting container {name}")
    docker.start(name)
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
    task_id = _task_id(task)
    log = lambda line: ctx.log(task_id, line)  # noqa: E731
    # The unified rollback (deployments.rollback): verify BEFORE teardown
    # (a bad target fails loudly leaving the healthy current deployment
    # untouched), then teardown of the current deployment, restore,
    # REAL health check of the target, and state commit — with the
    # target's health_status coming from the health check, never assumed.
    #
    # Spec §6: the operation is recorded durably on the current
    # deployment's row — requested -> succeeded / failed /
    # partially_reconciled — and a rollback is only reported "rolled_back"
    # when the target was restored AND proven healthy (health == healthy,
    # never merely "not unhealthy") AND the port registry settled. An
    # unhealthy OR unknown target, or a failed port settle, persists
    # rollback_failed — never a false success.
    current["rollback_status"] = "requested"
    current["rollback_target_deployment_id"] = target["deployment_id"]
    current.pop("rollback_error", None)
    ctx.deployment_store.save(current)
    try:
        outcome = rollback_mod.perform_rollback(
            ctx, docker, log=log, target=target,
            healthcheck_timeout=int(payload.get("healthcheck_timeout", 60)),
            before_restore=lambda: _teardown_current(ctx, docker, current),
            current=current)
    except rollback_mod.RollbackError as exc:
        current["rollback_status"] = "failed"
        current["rollback_error"] = str(exc)[:500]
        ctx.deployment_store.save(current)
        raise HandlerError(str(exc)) from exc
    target_healthy = outcome["target_health_status"] == "healthy"
    # Spec Part 2 (state machine): healthy -> settle ports -> reconcile
    # -> rolled_back; (unhealthy|unknown) -> rollback_failed. Port
    # settlement runs ONLY after the target is proven healthy — settling
    # the registry for a target whose health is unverified would bless a
    # broken promotion.
    if target_healthy:
        # Spec §3: reconcile the control-plane port registry (release the
        # rolled-back deployment's reservations, re-reserve the target's).
        # A settle failure never reports success: it is recorded durably
        # (rollback_failed / partially_reconciled) with the settle outcome
        # kept on the row for reconciliation/retry. A skipped settle (no
        # control-plane session — tests/doubles only) is not a failure.
        settle = rollback_mod.settle_rollback_ports(
            ctx, log, current["deployment_id"], outcome["rolled_back_to"])
        settle_failed = (not settle["settled"]
                         and settle.get("reason") != "no control-plane session")
    else:
        settle = {"settled": False, "released": None, "restored": None,
                  "skipped": None,
                  "reason": "settle skipped: target health not verified"}
        settle_failed = False
    if target_healthy and not settle_failed:
        final_status = "rolled_back"
        rb_status = "succeeded"
        rb_error = None
    elif target_healthy:
        final_status = "rollback_failed"
        rb_status = "partially_reconciled"
        rb_error = (f"target restored and healthy but port-registry settle "
                    f"failed: {settle.get('reason')}")
    elif outcome["target_health_status"] == "unknown":
        final_status = "rollback_failed"
        rb_status = "failed"
        rb_error = (f"target {outcome['rolled_back_to']} restored but "
                    f"target health could not be verified "
                    f"(target_health_status=unknown)")
    else:
        final_status = "rollback_failed"
        rb_status = "failed"
        rb_error = (f"target {outcome['rolled_back_to']} restored but its "
                    f"health check failed (health_status="
                    f"{outcome['target_health_status']})")
    current["status"] = final_status
    current["rollback_status"] = rb_status
    if rb_error:
        current["rollback_error"] = rb_error[:500]
    else:
        current.pop("rollback_error", None)
    # Recorded for reconciliation/retry (spec §6): the settle outcome
    # carries everything needed to re-run the registry reconciliation.
    current["port_settle"] = {"settled": settle["settled"],
                              "reason": settle.get("reason")}
    ctx.deployment_store.save(current)
    if final_status == "rolled_back":
        # Spec §4.14: the live deployment changed — refresh the local
        # ingress route mirror so it stops pointing at the torn-down
        # deployment (best-effort; the control plane re-points domains
        # from this task's result).
        _ingress_after_change(ctx, task_id, outcome["rolled_back_to"])
    return {"deployment_id": current["deployment_id"], "status": final_status,
            "rollback_status": rb_status,
            "rolled_back_to": outcome["rolled_back_to"],
            "target_health_status": outcome["target_health_status"],
            "ports_settled": settle["settled"]}


# ---------------------------------------------------------------------------
# introspection: logs | status | healthcheck | system-info
# ---------------------------------------------------------------------------
def _compose_logs(docker, project: str, tail: int) -> dict:
    """Logs per container of a compose project, keyed by container name."""
    per_container = {}
    for rec in docker.compose_ps(project) or []:
        cname = rec.get("Name") or rec.get("ID")
        if not cname:
            continue
        per_container[cname] = docker.logs(cname, tail=tail)
    return per_container


def _compose_status(docker, project: str) -> dict:
    """Aggregate + per-container status for a compose project.

    Mirrors what handle_status returns for single containers: a top-level
    status plus per-container detail with published ports.
    """
    containers = {}
    for rec in docker.compose_ps(project) or []:
        cname = rec.get("Name") or rec.get("ID") or "?"
        ports = {}
        for pub in rec.get("Publishers") or []:
            try:
                ports[str(pub.get("TargetPort"))] = str(
                    pub.get("PublishedPort"))
            except (TypeError, ValueError):
                continue
        containers[cname] = {
            "status": (rec.get("State") or "unknown").lower(),
            "ports": ports,
        }
    states = {c["status"] for c in containers.values()}
    if not containers:
        aggregate = "unknown"
    elif states == {"running"}:
        aggregate = "running"
    elif "running" in states:
        aggregate = "partial"
    else:
        aggregate = "stopped"
    return {"containers": containers, "status": aggregate}


def handle_logs(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    tail = int(payload.get("tail", 500))
    tail = max(1, min(tail, 5000))
    state, project = _compose_deployment(ctx, payload["deployment_id"])
    if project:
        logs = _compose_logs(docker, project, tail)
        return {"deployment_id": payload["deployment_id"],
                "compose_project": project, "logs": logs}
    name = _container_name(ctx, payload["deployment_id"])
    logs = docker.logs(name, tail=tail)
    return {"deployment_id": payload["deployment_id"], "logs": logs}


def handle_status(ctx, task: dict) -> dict:
    payload = _payload(task)
    _require(payload, "deployment_id")
    docker = ctx.require_docker()
    state, project = _compose_deployment(ctx, payload["deployment_id"])
    if project:
        detail = _compose_status(docker, project)
        return {"deployment_id": payload["deployment_id"],
                "compose_project": project,
                "status": detail["status"],
                "containers": detail["containers"]}
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
