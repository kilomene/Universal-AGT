"""Deploy pipeline: artifact -> verify -> extract -> validate -> build ->
run -> healthcheck -> (rollback on failure).

The deploy handler is the most privileged worker path, so it is also the
most defensive:

  * artifact bytes are SHA-256 verified BEFORE extraction; a mismatch moves
    the file to <work_dir>/quarantine/ and the task fails with no docker
    interaction at all;
  * agent.deploy.json is validated BEFORE any build (PROTOCOL §4);
  * the previous healthy deployment's container is kept (stopped) so a
    failed healthcheck can roll back to it;
  * every subprocess call is an argv list — never shell=True.
  * container names are unique per deployment attempt
    (uaht-<project>-<version>-<deployment[:8]>): redeploying the same
    version never stop+rms the live container — it survives until the new
    one passes health, then it is stopped (kept, not removed) as before;
  * before every `docker run` the host port is verified free both at OS
    level (bind test) and Docker level (`docker ps` published-port scan);
    a collision fails the task with a clear error instead of a cryptic
    `docker run` failure. Compose deployments get the same check: the
    ports declared in the compose file (via `docker compose config`) are
    verified free before `compose up`;
  * a failed healthcheck on a compose deploy tears the new stack down
    (`compose down`) and restores the previous stack (`compose up` with
    its stored compose file);
  * after every successful deploy, garbage collection removes containers
    and worker-built images of generations older than
    DEPLOY_KEEP_GENERATIONS (default 2) per project.

Deployment state is recorded under <work_dir>/deployments/<deployment_id>/
via deployments.state.DeploymentStore. Secret env values are injected into
the container but NEVER persisted to the state file.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import tarfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from deployments.manifest import parse_memory_mb, validate_manifest
from deployments import compose_validate
from deployments import gc
from health import checker as health_checker
from health import collector as health_collector

MANIFEST_FILENAME = "agent.deploy.json"
QUARANTINE_DIRNAME = "quarantine"
STATIC_DOCKERFILE = """\
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"""


class DeployError(Exception):
    """Fatal deploy failure; the dispatcher reports the task as failed."""


# ---------------------------------------------------------------------------
# Pure / easily-tested helpers
# ---------------------------------------------------------------------------

def decide_rollback(healthcheck_passed: bool,
                    previous: Optional[dict]) -> str:
    """Decide the post-healthcheck action.

    Returns "keep_new" | "rollback_to_previous" | "fail_clean".
    A previous deployment is a rollback target when it has a restorable
    handle: a container_name (docker/static runtime) or a compose_project
    (docker-compose runtime, restored via `compose up` with its stored
    compose file). Pure function — heavily unit tested.
    """
    if healthcheck_passed:
        return "keep_new"
    if previous and (previous.get("container_name")
                     or previous.get("compose_project")):
        return "rollback_to_previous"
    return "fail_clean"


def verify_checksum(path: str, expected: str) -> bool:
    """expected like 'sha256:<hex>'. Constant-time compare."""
    algo, _, hex_digest = expected.partition(":")
    if algo != "sha256" or not hex_digest:
        raise DeployError(f"unsupported checksum format: {expected!r}")
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    import hmac
    return hmac.compare_digest(digest.hexdigest(), hex_digest.lower())


def extract_archive(archive_path: str, dest_dir: str) -> str:
    """Extract .tar.gz/.tgz/.tar/.zip with path-traversal protection.

    The format is detected from the file's magic bytes, not its name —
    the pipeline stores downloads as <artifact_id>.bin, so an extension
    check would reject every real artifact.

    Tar member names are pre-scanned with an explicit confinement check:
    absolute paths and ``..`` escapes fail fast with a clear DeployError
    (data_filter would merely strip a leading "/" and keep going — a
    malicious archive must FAIL, not silently land renamed files).
    Extraction itself uses ``tarfile.data_filter`` (Python 3.12+): it
    blocks — critically — symlink/hardlink members that point outside the
    destination (the naive pre-extract loop it replaces could be bypassed
    by extracting a symlink first and then a file *through* it).

    Zip members are pre-scanned the same way; ``zipfile.extractall``
    always materializes entries as regular files, so symlink-flagged
    zip entries can never become real symlinks.

    All extraction failures surface as DeployError with a clear message —
    tar/zip library errors are wrapped, never leaked raw.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    def _safe_join(base: Path, member: str) -> Path:
        target = (base / member).resolve()
        if not str(target).startswith(str(base.resolve()) + os.sep) and target != base.resolve():
            raise DeployError(f"archive member escapes destination: {member!r}")
        return target

    try:
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path, "r") as tf:
                # Pre-scan every member name: data_filter neutralizes
                # absolute paths by stripping the leading "/" (extracting
                # them inside dest), but a malicious archive must FAIL with
                # a clear error, not silently land renamed files.
                for member in tf.getnames():
                    _safe_join(dest, member)
                tf.extractall(dest, filter="data")
        elif zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.namelist():
                    _safe_join(dest, member)
                zf.extractall(dest)
        else:
            raise DeployError(
                f"unsupported artifact format: {archive_path!r} "
                "(expected a tar archive or a zip file)"
            )
    except DeployError:
        raise
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError) as exc:
        # Corrupt archive, truncated stream, or a data_filter rejection
        # (.. escape, symlink/hardlink pointing outside). Absolute paths
        # are rejected earlier by the pre-scan. Nothing partial is
        # trusted: extraction already aborted.
        raise DeployError(f"archive extraction failed: {exc}") from exc
    return str(dest)


def confined_under(base: Path, requested: str | None, what: str) -> Path:
    """Resolve ``requested`` strictly inside ``base``; reject escapes.

    Used for manifest-controlled paths (dockerfile, build context, compose
    file): an artifact's agent.deploy.json must not smuggle ``..`` or
    absolute paths out of the extracted artifact tree.
    """
    resolved_base = base.resolve()
    target = (resolved_base / (requested or ".")).resolve()
    if target != resolved_base and resolved_base not in target.parents:
        raise DeployError(f"{what} escapes the allowed directory: {requested!r}")
    return target


def pick_free_port(used: set, log=None) -> int:
    """Pick an unused loopback TCP port, avoiding ports already assigned."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in used:
            return port
    raise DeployError("could not find a free host port after 20 attempts")


# Fields of the persisted deployment contract every docker-run rebuild must
# come from. The pipeline writes them; environment-update and reconcile read
# them back through run_spec_from_state(). States written before the full
# contract existed simply lack the keys (handled as documented there).
DEFAULT_RESTART_POLICY = "unless-stopped"


def run_spec_from_state(state: dict) -> dict:
    """Rebuild the ``docker run`` spec from a persisted deployment contract.

    Returns ``{"image", "ports", "memory", "cpus", "restart"}``.

    This is the SINGLE place environment-update and reconcile derive run
    behavior from. They must never fall back to hard-coded defaults or
    re-derive the spec from ``docker inspect`` while the contract is
    present — a deployment's restart policy, resource limits, image,
    container name and port mapping are deployment-time facts, and the
    state file is where they live.

    Back-compat: states written before the full contract was persisted
    lack the newer keys. Missing ``restart`` falls back to
    ``unless-stopped`` (the historical default); missing ``resources``
    means no limits; a missing ``ports`` mapping falls back to the legacy
    single ``host_port``/``container_port`` pair. Every fallback is a
    documented degradation for old states, not a reconstruction path for
    new ones.
    """
    resources = state.get("resources") or {}
    ports: dict = {}
    stored_ports = state.get("ports") or {}
    for host_port, container_port in stored_ports.items():
        try:
            ports[int(host_port)] = int(container_port)
        except (TypeError, ValueError):
            continue
    if not ports:
        # Pre-contract states: single host_port/container_port pair.
        host_port, container_port = state.get("host_port"), state.get("container_port")
        try:
            if host_port and container_port:
                ports[int(host_port)] = int(container_port)
        except (TypeError, ValueError):
            pass
    cpu = resources.get("cpu")
    return {
        "image": state.get("image"),
        "ports": ports or None,
        "memory": resources.get("memory"),
        "cpus": str(cpu) if cpu else None,
        "restart": state.get("restart") or DEFAULT_RESTART_POLICY,
    }


def published_host_ports(ports_text) -> list:
    """All host ports published in a `docker ps` Ports cell, in order.

    Cells look like "0.0.0.0:8080->3000/tcp, :::8080->3000/tcp" (or are
    empty for unexposed containers). Pure function — unit tested.
    """
    return [int(m.group(1)) for m in re.finditer(r":(\d+)->", ports_text or "")]


def verify_host_port_free(docker, host_port: int, log=None) -> None:
    """Fail with DeployError unless host_port is free, twice over.

    OS level: bind the port ourselves — catches anything listening on the
    host (including docker's own published-port proxy). Docker level: scan
    `docker ps -a` published ports — catches ports docker holds on a
    specific interface that a wildcard bind might miss.

    Called before every `docker run`; a collision fails the task with a
    clear error instead of a cryptic `docker run` failure.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", host_port))
    except OSError:
        raise DeployError(
            f"host port {host_port} is already in use on this host "
            f"(OS bind test failed); refusing docker run"
        )
    try:
        rows = docker.ps(all=True)
    except Exception as exc:
        raise DeployError(
            f"could not list docker containers to verify port "
            f"{host_port} is free: {exc}"
        )
    used = set()
    for row in rows or []:
        used.update(published_host_ports(row.get("Ports")))
    if host_port in used:
        raise DeployError(
            f"host port {host_port} is already published by another docker "
            f"container; refusing docker run"
        )
    if log:
        log(f"host port {host_port} verified free (OS bind + docker ps)")


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_env(env: dict, log=None) -> dict:
    """Coerce env to string values, drop keys docker would reject."""
    clean = {}
    for key, value in (env or {}).items():
        if not isinstance(key, str) or not _ENV_KEY_RE.match(key):
            if log:
                log(f"dropping invalid env key: {key!r}")
            continue
        clean[key] = str(value)
    return clean


# ---------------------------------------------------------------------------
# Deploy handler
# ---------------------------------------------------------------------------

def deploy(ctx, task: dict) -> dict:
    payload = task.get("payload") or {}
    task_id = task.get("id", "unknown")
    secrets = payload.get("secrets") or {}

    project_id = payload.get("project_id")
    project_name = payload.get("project_name") or payload.get("name")
    version = payload.get("version")
    artifact_id = payload.get("artifact_id")
    deployment_id = payload.get("deployment_id") or str(uuid.uuid4())
    if not project_id or not project_name or not version:
        raise DeployError("deploy payload needs project_id, project_name and version")

    def log(line: str) -> None:
        ctx.log(task_id, line)  # ctx.log scrubs secrets before writing
        try:
            ctx.log_store.append_deployment(deployment_id, ctx.scrub(line))
        except OSError:
            pass

    log(f"deploy start: project={project_name} version={version} "
        f"deployment={deployment_id}")
    store = ctx.deployment_store
    work_dir = Path(ctx.config.work_dir)
    state_dir = store.deployment_dir(deployment_id)

    # -- 1. artifact download + checksum (before ANY docker interaction) ----
    extract_dir = None
    manifest_data = payload.get("manifest")
    image_tag = payload.get("image")  # alternative: run a prebuilt image
    safe_project = re.sub(r"[^a-z0-9_.-]+", "-", str(project_name).lower()).strip("-") or "app"
    safe_version = re.sub(r"[^a-z0-9_.-]+", "-", str(version).lower()).strip("-") or "v0"
    # Unique per deployment attempt: redeploying the same version must NOT
    # stop+rm the live container before the new one passes health. The old
    # container keeps its own name and survives until the new one is
    # healthy; then it is stopped (kept, not removed) as before. The actual
    # name is stored in state.json, which is what reconcile/handlers use —
    # the uaht-<project>-<version>- prefix stays docker-ps-greppable.
    attempt_suffix = re.sub(r"[^a-z0-9]+", "", str(deployment_id).lower())[:8] or "retry"
    container_name = f"uaht-{safe_project}-{safe_version}-{attempt_suffix}"
    built_image = image_tag or f"uaht-{safe_project}:{safe_version}"

    if artifact_id:
        expected_checksum = payload.get("artifact_checksum")
        if not expected_checksum:
            raise DeployError(
                "refusing to deploy: payload has artifact_id but no "
                "artifact_checksum; unverified artifacts are never executed"
            )
        artifacts_dir = work_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = str(artifacts_dir / f"{artifact_id}.bin")
        log(f"downloading artifact {artifact_id}")
        ctx.api.download_artifact(
            artifact_id, dest, expected_size=payload.get("artifact_size")
        )
        log("verifying SHA-256")
        if not verify_checksum(dest, expected_checksum):
            quarantine = work_dir / QUARANTINE_DIRNAME
            quarantine.mkdir(parents=True, exist_ok=True)
            qpath = quarantine / f"{artifact_id}-{int(time.time())}.bin"
            shutil.move(dest, qpath)
            raise DeployError(
                f"artifact checksum mismatch for {artifact_id}; "
                f"quarantined at {qpath}, no docker interaction performed"
            )
        log("checksum OK")
        extract_dir = str(state_dir / "source")
        log(f"extracting artifact to {extract_dir}")
        extract_archive(dest, extract_dir)
        file_manifest = Path(extract_dir) / MANIFEST_FILENAME
        if file_manifest.exists():
            import json
            with open(file_manifest, "r", encoding="utf-8") as fh:
                file_data = json.load(fh)
            merged = dict(file_data)
            if isinstance(manifest_data, dict):
                merged.update(manifest_data)  # payload wins: control plane authoritative
            manifest_data = merged
            log(f"loaded {MANIFEST_FILENAME} from artifact"
                + (" (+ payload overlay)" if payload.get("manifest") else ""))

    if not isinstance(manifest_data, dict):
        raise DeployError(
            f"no {MANIFEST_FILENAME} found in artifact and no manifest in "
            "payload; cannot deploy without a manifest"
        )

    # -- 2. manifest validation (before any build) --------------------------
    ok, errors = validate_manifest(manifest_data, project_name=project_name)
    if not ok:
        raise DeployError("manifest validation failed: " + "; ".join(errors))
    log(f"manifest valid: runtime={manifest_data['runtime']}")

    runtime = manifest_data["runtime"]
    service = manifest_data.get("service") or {}
    container_port = int(service.get("port") or (80 if runtime == "static" else 3000))
    healthcheck_path = service.get("healthcheck") or "/"
    resources = manifest_data.get("resources") or {}
    restart_policy = manifest_data.get("restart") or "unless-stopped"
    manifest_env = sanitize_env(manifest_data.get("env"), log=log)
    # Secrets: payload-carried values first (control-plane injected), then
    # the worker pulls the project's stored secrets over its authenticated
    # channel (GET /v1/worker/projects/:id/secrets) when the payload carries
    # none. Payload values win on key collision. Either way they become
    # container env only — never persisted to state.json (see the save
    # below) and scrubbed from every log line.
    payload_secrets = secrets if isinstance(secrets, dict) else {}
    fetched_secrets: dict = {}
    if project_id and hasattr(ctx.api, "get_project_secrets"):
        try:
            fetched_secrets = ctx.api.get_project_secrets(project_id) or {}
            if fetched_secrets:
                log(f"fetched {len(fetched_secrets)} project secret(s) "
                    f"for container env")
        except Exception as exc:
            log(f"project secrets unavailable ({exc}); continuing with "
                f"payload secrets only")
    secret_env = sanitize_env({**fetched_secrets, **payload_secrets}, log=log)
    run_env = {**manifest_env, **secret_env}  # secrets override manifest env

    # -- 3. host resource check (admission control on RESERVED resources) ----
    # allocated = sum of reservations from running deployments' manifests
    # (see health.collector.allocated_resources); utilized = instantaneous
    # measured usage, which is IRRELEVANT here. A deployment is admitted
    # only when total - allocated covers its request: a host at 2% CPU can
    # still refuse a deployment whose reservations are already full.
    # Note: the previous deployment of this project (if any) is still
    # 'running' at this point — both containers briefly exist during the
    # healthcheck window, so its reservation correctly counts as allocated.
    if resources.get("memory") or resources.get("cpu"):
        allocated = health_collector.allocated_resources(store)
        if resources.get("memory"):
            need_mb = parse_memory_mb(resources["memory"])
            total_mb = health_collector.total_ram_mb()
            avail_mb = total_mb - allocated["ram_mb"]
            log(f"resource check: need {need_mb}MB ram, host total "
                f"{total_mb}MB, reserved {allocated['ram_mb']}MB, "
                f"available {avail_mb:.0f}MB")
            if total_mb > 0 and need_mb > avail_mb:
                raise DeployError(
                    f"insufficient reservable memory: deployment requests "
                    f"{need_mb}MB but only {max(avail_mb, 0):.0f}MB is "
                    f"available (host total {total_mb}MB, "
                    f"{allocated['ram_mb']}MB already reserved by running "
                    f"deployments)"
                )
        if resources.get("cpu"):
            need_cpu = float(resources["cpu"])
            total_cpus = health_collector.total_cpu()
            avail_cpu = total_cpus - allocated["cpu"]
            log(f"resource check: need {need_cpu} cpu, host total "
                f"{total_cpus}, reserved {allocated['cpu']}, "
                f"available {avail_cpu:.2f}")
            if total_cpus > 0 and need_cpu > avail_cpu:
                raise DeployError(
                    f"insufficient reservable CPU: deployment requests "
                    f"{need_cpu} but only {max(avail_cpu, 0):.2f} is "
                    f"available (host total {total_cpus}, "
                    f"{allocated['cpu']} already reserved by running "
                    f"deployments)"
                )

    docker = ctx.require_docker()

    # -- 4. previous healthy deployment (rollback target) --------------------
    previous = store.latest_for_project(project_id, statuses=("running",))
    if previous:
        log(f"rollback target: previous deployment {previous['deployment_id']} "
            f"(container {previous.get('container_name')})")
    else:
        log("no previous healthy deployment; rollback target: none")

    # -- 5. build ------------------------------------------------------------
    effective_health_port = None  # for compose-discovered ports
    compose_file = None
    compose_project_name = None
    compose_ports = None  # published host ports declared by the compose file
    image_built = False  # True only when THIS deploy ran docker build
    if runtime == "docker":
        if payload.get("image"):
            log(f"using prebuilt image {built_image}; skipping build")
        else:
            if extract_dir is None:
                raise DeployError("runtime=docker needs artifact_id with a Dockerfile")
            build_cfg = manifest_data.get("build") or {}
            extract_base = Path(extract_dir)
            dockerfile = str(confined_under(
                extract_base, build_cfg.get("dockerfile", "Dockerfile"),
                "build.dockerfile"))
            context_dir = str(confined_under(
                extract_base, build_cfg.get("context", "."), "build.context"))
            if not os.path.isfile(dockerfile):
                raise DeployError(f"Dockerfile not found: {dockerfile}")
            log(f"docker build: tag={built_image} dockerfile={dockerfile}")
            docker.build(context_dir, dockerfile, built_image,
                         timeout=int(payload.get("build_timeout", 1200)))
            image_built = True
            log("docker build OK")
    elif runtime == "docker-compose":
        if not docker.compose_available():
            raise DeployError("runtime=docker-compose but 'docker compose' is unavailable")
        if extract_dir is None:
            raise DeployError("runtime=docker-compose needs artifact_id")
        extract_base = Path(extract_dir)
        compose_file = (
            str(confined_under(extract_base, payload.get("compose_file"),
                               "compose_file"))
            if payload.get("compose_file")
            else str(extract_base / "docker-compose.yml")
        )
        if not os.path.isfile(compose_file):
            alt = str(Path(extract_dir) / "compose.yaml")
            compose_file = alt if os.path.isfile(alt) else compose_file
        if not os.path.isfile(compose_file):
            raise DeployError(f"compose file not found: {compose_file}")
        compose_project_name = f"uaht-{safe_project}"
        # Port check for compose too: compose publishes host ports, so the
        # ports the file declares must be verified free BEFORE `compose up`,
        # exactly like the pre-`docker run` check. Ports already held by
        # this project's own stack are skipped (redeploy replaces it).
        try:
            compose_model = docker.compose_config_json(compose_file)
        except Exception as exc:
            raise DeployError(
                f"could not read normalized compose model for {compose_file}: "
                f"{exc}"
            )
        # W5/W7: explicit DENY-list validation of the compose model BEFORE
        # `compose up`. A task-controlled compose file must not smuggle
        # privileged mode, host namespaces, host devices, added
        # capabilities, custom security options, or bind mounts escaping
        # the deployment workspace.
        compose_errors = compose_validate.validate_compose_model(
            compose_model, workspace=extract_base, compose_file=compose_file)
        if compose_errors:
            raise DeployError(
                "compose file rejected by security policy: "
                + "; ".join(compose_errors))
        log("compose file passed security validation")
        wanted_ports = parse_compose_published_ports(compose_model)
        log(f"compose declares published host ports: {wanted_ports or 'none'}")
        # Ports held by the previous generation of THIS project are not a
        # collision (redeploy replaces its own stack in place).
        previous_ports: set = set()
        if previous:
            if previous.get("host_port"):
                previous_ports.add(int(previous["host_port"]))
            for p in previous.get("compose_ports") or []:
                try:
                    previous_ports.add(int(p))
                except (TypeError, ValueError):
                    continue
        verify_compose_ports_free(docker, compose_project_name, wanted_ports,
                                  log=log,
                                  registry_used=store.used_host_ports()
                                  - previous_ports)
        compose_ports = sorted(wanted_ports) or None
        log(f"docker compose up: {compose_file} (project {compose_project_name})")
        docker.compose_up(compose_file, project_name=compose_project_name,
                          build=True)
        effective_health_port = _discover_compose_port(
            docker, compose_project_name, log)
        if not effective_health_port:
            raise DeployError(
                "compose started but no published port found on its "
                "containers; cannot healthcheck"
            )
        log(f"compose service port discovered: {effective_health_port}")
    elif runtime == "static":
        if extract_dir is None:
            raise DeployError("runtime=static needs artifact_id with static files")
        df_path = str(Path(extract_dir) / "Dockerfile.uaht-static")
        with open(df_path, "w", encoding="utf-8") as fh:
            fh.write(STATIC_DOCKERFILE)
        container_port = 80
        log(f"static build: tag={built_image} (nginx)")
        docker.build(extract_dir, df_path, built_image,
                     timeout=int(payload.get("build_timeout", 1200)))
        image_built = True
        log("static build OK")
    else:  # pragma: no cover — validator guarantees this is unreachable
        raise DeployError(f"unsupported runtime: {runtime!r}")

    # -- 6. run new container (compose runtime already running) -------------
    new_container_started = False
    host_port = None
    if runtime == "docker-compose":
        host_port = effective_health_port
    else:
        requested = payload.get("requested_host_port")
        if requested is not None:
            # Fixed port reserved in the control plane's port registry for
            # this deployment: use it, after verifying it is really free.
            try:
                requested_port = int(requested)
            except (TypeError, ValueError):
                raise DeployError(
                    f"invalid requested_host_port {requested!r}; "
                    f"expected an integer 1-65535"
                )
            if not 1 <= requested_port <= 65535:
                raise DeployError(
                    f"invalid requested_host_port {requested!r}; "
                    f"expected an integer 1-65535"
                )
            host_port = requested_port
            log(f"using registry-reserved host port {host_port}")
        else:
            host_port = pick_free_port(store.used_host_ports(), log=log)
        verify_host_port_free(docker, host_port, log=log)
        if docker.container_exists(container_name):
            log(f"removing stale container {container_name}")
            docker.stop(container_name)
            docker.rm(container_name)
        log(f"docker run: name={container_name} image={built_image} "
            f"port {host_port}->{container_port}")
        docker.run(
            container_name, built_image,
            ports={host_port: container_port},
            env=run_env,
            memory=resources.get("memory"),
            cpus=str(resources["cpu"]) if resources.get("cpu") else None,
            restart=restart_policy,
        )
        new_container_started = True

    # -- 7. healthcheck -------------------------------------------------------
    health_timeout = int(payload.get("healthcheck_timeout", 120))
    log(f"healthcheck: GET :{host_port}{healthcheck_path} "
        f"(timeout {health_timeout}s)")
    healthy = health_checker.wait_for_healthcheck(
        host_port, healthcheck_path, timeout_secs=health_timeout, log=log)

    action = decide_rollback(healthy, previous)
    if action == "keep_new":
        if previous and previous.get("container_name"):
            log(f"new version healthy; stopping previous "
                f"{previous['container_name']} (kept for rollback)")
            docker.stop(previous["container_name"])
            prev_state = store.load(previous["deployment_id"]) or previous
            prev_state["status"] = "superseded"
            store.save(prev_state)
        state = {
            "deployment_id": deployment_id,
            "task_id": task_id,
            "project_id": project_id,
            "project_name": project_name,
            "version": version,
            "container_name": container_name if new_container_started else None,
            "compose_project": compose_project_name,
            "compose_file": compose_file,
            "compose_ports": compose_ports,
            "image": built_image,
            "image_built": image_built,  # GC may only remove images we built
            "runtime": runtime,
            "host_port": host_port,
            "container_port": container_port,
            # Full deployment contract (W4): every later rebuild of this
            # deployment — env updates, restarts, post-reboot reconcile —
            # derives its run behavior from THESE fields via
            # run_spec_from_state(), never from hard-coded defaults or
            # re-derived `docker inspect` output.
            "ports": {str(host_port): container_port} if host_port else None,
            "restart": restart_policy,
            "resources": {
                "cpu": resources.get("cpu"),
                "memory": resources.get("memory"),
            },
            "volumes": manifest_data.get("volumes"),
            "healthcheck": manifest_data.get("healthcheck"),
            "domains": manifest_data.get("domains"),
            "manifest": manifest_data,
            "healthcheck_path": healthcheck_path,
            "env": manifest_env,  # secrets deliberately NOT persisted
            "status": "running",
            "health_status": "healthy",
            "previous_deployment_id": (previous or {}).get("deployment_id"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        store.save(state)
        log(f"deploy OK: {project_name} {version} healthy on :{host_port}")
        # Garbage-collect superseded generations AFTER the successful
        # deploy — never during. Keeps DEPLOY_KEEP_GENERATIONS (default 2)
        # newest generations per project.
        try:
            gc_summary = gc.collect_garbage(ctx, log=log)
            removed = (len(gc_summary["removed_containers"])
                       + len(gc_summary["removed_images"]))
            if removed:
                log(f"gc: removed {len(gc_summary['removed_containers'])} "
                    f"container(s), {len(gc_summary['removed_images'])} "
                    f"image(s)")
        except Exception as exc:  # GC must never fail a good deploy
            log(f"gc error (non-fatal): {exc}")
        return {
            "deployment_id": deployment_id,
            "status": "running",
            "health_status": "healthy",
            "ports": {str(host_port): container_port},
        }

    # -- 8. failure: remove new, roll back ------------------------------------
    log("healthcheck failed; rolling back")
    if runtime == "docker-compose" and compose_project_name:
        # Tear down the NEW unhealthy compose stack first — it must not
        # keep running (and holding ports) after a failed deploy.
        log(f"tearing down failed compose stack {compose_project_name}")
        try:
            docker.compose_down(compose_file, project_name=compose_project_name)
        except Exception as exc:
            log(f"compose down of failed stack failed (continuing): {exc}")
    if new_container_started:
        container_logs = ""
        try:
            container_logs = docker.logs(container_name, tail=200)
        except Exception:
            pass
        if container_logs:
            log(f"failed container logs (tail):\n{container_logs[-4000:]}")
        docker.stop(container_name)
        docker.rm(container_name, force=True)
    rolled_back_to = None
    if action == "rollback_to_previous" and previous:
        prev_name = previous.get("container_name")
        prev_compose = previous.get("compose_project")
        if prev_name:
            log(f"restarting previous container {prev_name}")
            docker.start(prev_name)
        elif prev_compose:
            prev_file = previous.get("compose_file")
            if not prev_file or not os.path.isfile(prev_file):
                raise DeployError(
                    f"healthcheck failed for {project_name} {version} and "
                    f"the previous compose stack {prev_compose} cannot be "
                    f"restored: its compose file "
                    f"{prev_file!r} is missing (redeploy required)"
                )
            log(f"restoring previous compose stack {prev_compose} "
                f"from {prev_file}")
            docker.compose_up(prev_file, project_name=prev_compose, build=True)
        prev_state = store.load(previous["deployment_id"]) or dict(previous)
        prev_state["status"] = "running"
        prev_state["health_status"] = "healthy"
        store.save(prev_state)
        rolled_back_to = previous["deployment_id"]
    store.save({
        "deployment_id": deployment_id,
        "task_id": task_id,
        "project_id": project_id,
        "project_name": project_name,
        "version": version,
        "container_name": None,
        "compose_project": None,
        "compose_file": compose_file,
        "compose_ports": compose_ports,
        "image": built_image,
        "image_built": image_built,
        "runtime": runtime,
        "host_port": None,
        "container_port": container_port,
        "ports": None,
        "restart": restart_policy,
        "resources": {
            "cpu": resources.get("cpu"),
            "memory": resources.get("memory"),
        },
        "volumes": manifest_data.get("volumes"),
        "healthcheck": manifest_data.get("healthcheck"),
        "domains": manifest_data.get("domains"),
        "manifest": manifest_data,
        "healthcheck_path": healthcheck_path,
        "env": manifest_env,
        "status": "rolled_back" if rolled_back_to else "failed",
        "health_status": "unhealthy",
        "rollback_of": rolled_back_to,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    raise DeployError(
        f"healthcheck failed for {project_name} {version} "
        f"(GET :{host_port}{healthcheck_path} never returned 200); "
        + (f"rolled back to deployment {rolled_back_to}"
           if rolled_back_to else "no previous healthy deployment to roll back to")
    )


def _discover_compose_port(docker, compose_project: str, log=None) -> Optional[int]:
    """Find the first published host port of a compose project's containers.

    Reads `docker compose -p <name> ps --format json` (the Publishers list)
    and falls back to `docker inspect` NetworkSettings.Ports per container.
    No regexing of `docker ps` text. Pure-ish: the parsing helpers are unit
    tested; docker calls are injected.
    """
    try:
        records = docker.compose_ps(compose_project)
    except Exception as exc:
        if log:
            log(f"compose port discovery failed for {compose_project}: {exc}")
        return None
    for rec in records or []:
        found = _published_ports_from_compose_ps(rec)
        if found:
            return found[0]
    # Fallback: inspect each compose container's published ports.
    for rec in records or []:
        name = rec.get("Name") or rec.get("ID")
        if not name:
            continue
        try:
            info = docker.inspect(name)
        except Exception:
            continue
        found = _published_ports_from_inspect(info)
        if found:
            return found[0]
    return None


def _published_ports_from_compose_ps(record: dict) -> list:
    """Host ports from a `docker compose ps --format json` record's
    Publishers list: [{PublishedPort, TargetPort, Protocol, ...}]."""
    ports = []
    for pub in (record or {}).get("Publishers") or []:
        try:
            ports.append(int(pub.get("PublishedPort")))
        except (TypeError, ValueError):
            continue
    return ports


def _published_ports_from_inspect(info: list) -> list:
    """Host ports from `docker inspect` NetworkSettings.Ports."""
    ports = []
    try:
        bindings = (info[0].get("NetworkSettings") or {}).get("Ports") or {}
    except (IndexError, AttributeError):
        return ports
    for binds in bindings.values():
        for bind in binds or []:
            try:
                ports.append(int(bind.get("HostPort")))
            except (TypeError, ValueError):
                continue
    return ports


def parse_compose_published_ports(config: dict) -> list:
    """All published host ports declared in a normalized compose model
    (`docker compose config --format json`). Handles short syntax
    ("8080:80", "127.0.0.1:8080:80", "80") and long syntax
    ({published: 8080, target: 80}). Pure function — unit tested."""
    ports: set = set()
    services = (config or {}).get("services") or {}
    for svc in services.values():
        for entry in (svc or {}).get("ports") or []:
            if isinstance(entry, dict):
                pub = entry.get("published")
                if pub is not None:
                    try:
                        ports.add(int(str(pub)))
                    except (TypeError, ValueError):
                        pass
            elif isinstance(entry, str):
                ports.update(_parse_compose_short_port(entry))
    return sorted(ports)


def _parse_compose_short_port(spec: str) -> list:
    """Published host port from one short-syntax port spec, or []."""
    s = str(spec).split("/")[0].strip()  # drop "/tcp" suffix
    parts = s.split(":")
    if len(parts) == 2:
        host = parts[0]
    elif len(parts) == 3:
        host = parts[1]  # [ip:]host:container
    else:
        return []  # "container" only (ephemeral) or exotic forms: no fixed host port
    host = host.strip().strip("[]")
    return [int(host)] if host.isdigit() else []


def verify_compose_ports_free(docker, compose_project: str,
                              wanted_ports: list, log=None,
                              registry_used: Optional[set] = None) -> None:
    """Verify every host port a compose file wants to publish is free —
    the same OS-bind + docker-ps check `docker run` gets.

    Ports already held by THIS compose project's current stack are skipped:
    a redeploy replaces its own stack in place (`compose up` on the same
    project name), so its own ports are not a collision.

    ``registry_used`` (optional): host ports the control-plane port
    registry / deployment store has reserved for other deployments.
    Catches the window where a port is allocated but not yet bound.
    """
    own: set = set()
    try:
        for rec in docker.compose_ps(compose_project) or []:
            own.update(_published_ports_from_compose_ps(rec))
    except Exception:
        own = set()
    for port in sorted(set(wanted_ports) - own):
        if registry_used and port in registry_used:
            raise DeployError(
                f"host port {port} is reserved by another deployment "
                f"(port registry); refusing compose up"
            )
        verify_host_port_free(docker, port, log=log)
    if log and wanted_ports:
        log(f"compose ports verified: wanted={sorted(set(wanted_ports))} "
            f"already-held-by-own-stack={sorted(own)}")
