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


class HandlerError(Exception):
    """A handler-level failure; the dispatcher reports the task as failed."""


def _payload(task: dict) -> dict:
    return task.get("payload") or {}


def _task_id(task: dict) -> str:
    return task.get("id", "unknown")


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
    names — never by project-prefix matching."""
    task_id = "rollback"
    if state.get("container_name"):
        docker.start(state["container_name"])
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
    return deploy_pipeline.deploy(ctx, task)


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
    _teardown_current(ctx, docker, current)
    _restore_deployment(ctx, docker, target)
    current["status"] = "rolled_back"
    ctx.deployment_store.save(current)
    target["status"] = "running"
    ctx.deployment_store.save(target)
    return {"deployment_id": current["deployment_id"], "status": "rolled_back",
            "rolled_back_to": target["deployment_id"]}


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
    docker = ctx.require_docker()
    task_id = _task_id(task)

    expected_checksum = payload.get("artifact_checksum")
    if not expected_checksum:
        raise HandlerError("refusing to build: no artifact_checksum in payload")
    work_dir = Path(ctx.config.work_dir)
    artifacts_dir = work_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dest = str(artifacts_dir / f"{payload['artifact_id']}.bin")
    ctx.log(task_id, f"downloading artifact {payload['artifact_id']}")
    ctx.api.download_artifact(
        payload["artifact_id"], dest, expected_size=payload.get("artifact_size"))
    if not deploy_pipeline.verify_checksum(dest, expected_checksum):
        quarantine = work_dir / deploy_pipeline.QUARANTINE_DIRNAME
        quarantine.mkdir(parents=True, exist_ok=True)
        qpath = quarantine / f"{payload['artifact_id']}-{int(time.time())}.bin"
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
    ctx.log(_task_id(task), f"docker compose up {compose_file}")
    docker.compose_up(str(compose_file), project_name=project_name,
                      build=bool(payload.get("build")))
    return {"project_name": project_name, "compose_file": str(compose_file),
            "status": "running"}


# ---------------------------------------------------------------------------
# environment-update
# ---------------------------------------------------------------------------
def handle_environment_update(ctx, task: dict) -> dict:
    """Recreate a deployment's container with merged env (stop -> rm -> run)."""
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
    ctx.log(_task_id(task), f"recreating {name} with {len(merged)} env vars")
    info = docker.inspect(name)[0]
    image = info.get("Config", {}).get("Image")
    port_bindings = {}
    try:
        bindings = (info.get("NetworkSettings") or {}).get("Ports") or {}
        for cport, binds in bindings.items():
            if binds:
                port_bindings[int(binds[0]["HostPort"])] = int(cport.split("/")[0])
    except (ValueError, KeyError, IndexError):
        pass
    docker.stop(name)
    docker.rm(name, force=True)
    docker.run(name, image, ports=port_bindings, env=merged,
               restart=state.get("restart", "unless-stopped"))
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
    base = _confined_path(
        Path(ctx.config.apps_dir).resolve(), payload.get("destination"),
        "destination")
    dest = str(base / f"{payload['artifact_id']}.bin") \
        if base.is_dir() or not payload.get("destination") else str(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    ctx.log(_task_id(task), f"downloading artifact {payload['artifact_id']}")
    ctx.api.download_artifact(
        payload["artifact_id"], dest, expected_size=payload.get("artifact_size"))
    expected = payload.get("artifact_checksum")
    if expected and not deploy_pipeline.verify_checksum(dest, expected):
        os.remove(dest)
        raise HandlerError("downloaded artifact failed checksum verification")
    return {"artifact_id": payload["artifact_id"], "path": dest,
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


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
