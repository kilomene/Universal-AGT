"""Validation of agent.deploy.json per PROTOCOL §4.

Pure function: validate_manifest(data, project_name=None) -> (ok, errors).

Rules enforced:
  * name: required, non-empty string; must equal project_name when given
  * runtime: required, one of docker | docker-compose | static
  * service.port: optional, integer 1..65535
  * service.healthcheck: optional, string starting with "/"
  * resources.memory: optional, like 256m | 512Mi | 1g (regex ^\\d{1,6}[mMgG][iI]?$)
  * resources.cpu: optional, positive number
  * restart: optional, one of no | always | unless-stopped | on-failure
  * build.dockerfile / build.context: optional non-empty strings
  * env: optional mapping of string keys to scalar values
  * volumes: REJECTED (spec §6 — no partial support). The docker runtime
    never mounts host paths (DockerClient.run has no -v flag, deliberately),
    so a manifest-level `volumes` field would be persisted-but-inert and
    silently misleading. Persistent storage is available via
    runtime=docker-compose with named volumes or workspace-relative bind
    mounts (validated by compose_validate.py).
Unknown top-level keys are ignored (forward compatibility); unknown keys
inside known sections are ignored as well.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

RUNTIMES = ("docker", "docker-compose", "static")
RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")
# W16 audit: digit run capped at 6 — a 5000-digit "memory" passed the old
# ^\d+$ shape and then blew up int() (Python 3.11+ caps str->int at 4300
# digits) deep in the deploy pipeline. 999999m/999999g is already absurd.
# Part 1B: the optional "i"/"I" suffix ("512Mi", "1Gi") is accepted so the
# single authoritative resource parser below and validate_manifest agree on
# exactly one memory grammar.
MEMORY_RE = re.compile(r"^\d{1,6}[mMgG][iI]?$")


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_manifest(data: Any,
                      project_name: Optional[str] = None) -> tuple[bool, list]:
    errors: list[str] = []

    if not isinstance(data, dict):
        return False, ["manifest must be a JSON object"]

    # -- name ------------------------------------------------------------
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name: required non-empty string")
    elif project_name is not None and name != project_name:
        errors.append(
            f"name: {name!r} does not match project name {project_name!r}"
        )

    # -- runtime ---------------------------------------------------------
    runtime = data.get("runtime")
    if runtime not in RUNTIMES:
        errors.append(
            f"runtime: must be one of {list(RUNTIMES)}, got {runtime!r}"
        )

    # -- build -----------------------------------------------------------
    build = data.get("build", {})
    if build is not None:
        if not isinstance(build, dict):
            errors.append("build: must be an object")
        else:
            dockerfile = build.get("dockerfile")
            if dockerfile is not None and (
                not isinstance(dockerfile, str) or not dockerfile.strip()
            ):
                errors.append("build.dockerfile: must be a non-empty string")
            context = build.get("context")
            if context is not None and (
                not isinstance(context, str) or not context.strip()
            ):
                errors.append("build.context: must be a non-empty string")

    # -- service ---------------------------------------------------------
    service = data.get("service", {})
    if service is not None:
        if not isinstance(service, dict):
            errors.append("service: must be an object")
        else:
            port = service.get("port")
            if port is not None:
                if not isinstance(port, int) or isinstance(port, bool):
                    errors.append(
                        f"service.port: must be an integer 1..65535, got {port!r}"
                    )
                elif not 1 <= port <= 65535:
                    errors.append(
                        f"service.port: must be 1..65535, got {port}"
                    )
            healthcheck = service.get("healthcheck")
            if healthcheck is not None and (
                not isinstance(healthcheck, str) or not healthcheck.startswith("/")
            ):
                errors.append(
                    "service.healthcheck: must be a string starting with '/', "
                    f"got {healthcheck!r}"
                )

    # -- resources -------------------------------------------------------
    resources = data.get("resources", {})
    if resources is not None:
        if not isinstance(resources, dict):
            errors.append("resources: must be an object")
        else:
            memory = resources.get("memory")
            if memory is not None and (
                not isinstance(memory, str) or not MEMORY_RE.match(memory)
            ):
                errors.append(
                    "resources.memory: must look like '256m', '512Mi' or '1g', "
                    f"got {memory!r}"
                )
            cpu = resources.get("cpu")
            if cpu is not None and (not _is_num(cpu) or cpu <= 0):
                errors.append(
                    f"resources.cpu: must be a positive number, got {cpu!r}"
                )
            # W16 audit: JSON can carry NaN/Infinity (Python's json accepts
            # them); both pass _is_num and the <= 0 check but are nonsense
            # as a CPU reservation. Reject non-finite values explicitly.
            if isinstance(cpu, float) and not math.isfinite(cpu):
                errors.append(
                    f"resources.cpu: must be finite, got {cpu!r}"
                )

    # -- restart ---------------------------------------------------------
    restart = data.get("restart")
    if restart is not None and restart not in RESTART_POLICIES:
        errors.append(
            f"restart: must be one of {list(RESTART_POLICIES)}, got {restart!r}"
        )

    # -- env -------------------------------------------------------------
    env = data.get("env")
    if env is not None:
        if not isinstance(env, dict):
            errors.append("env: must be an object of string keys to scalar values")
        else:
            for key, value in env.items():
                if not isinstance(key, str) or not key:
                    errors.append(f"env: invalid key {key!r}")
                elif not isinstance(value, (str, int, float, bool)) or value is None:
                    errors.append(
                        f"env.{key}: value must be a string/number/boolean, "
                        f"got {value!r}"
                    )

    # -- volumes -------------------------------------------------------
    # Spec §6: no partial volume support. Manifest-level `volumes` would be
    # persisted to the deployment state but never applied — docker.run
    # accepts no -v flag (W5 deny list) and compose deployments carry their
    # own volumes in the compose file. Reject outright instead of silently
    # ignoring; agents needing persistent storage use
    # runtime=docker-compose with named volumes or workspace-relative bind
    # mounts.
    if data.get("volumes") is not None:
        errors.append(
            "volumes: not supported in agent.deploy.json; the docker "
            "runtime never mounts host paths. Use runtime=docker-compose "
            "with named volumes or workspace-relative bind mounts instead"
        )

    return (len(errors) == 0), errors


def parse_memory_mb(memory: str) -> int:
    """'256m' -> 256, '512Mi' -> 512, '1g' -> 1024, '2G' -> 2048.

    Raises ValueError if invalid.
    """
    if not isinstance(memory, str) or not MEMORY_RE.match(memory):
        raise ValueError(f"invalid memory spec: {memory!r}")
    spec = memory[:-1] if memory[-1] in ("i", "I") else memory
    try:
        amount = int(spec[:-1])
    except ValueError:
        # Defensive: the regex above already bounds the digit run, but a
        # direct caller could pass a hostile string on an interpreter with
        # different int-conversion limits.
        raise ValueError(f"invalid memory spec: {memory!r}")
    return amount * 1024 if spec[-1].lower() == "g" else amount


def normalize_resources(resources: Any) -> dict:
    """Single authoritative resource parser (Part 1B).

    ``{"resources": {"cpu": 1.5, "memory": "512Mi"}}`` ->>
    ``{"cpu": 1.5, "ram_mb": 512}``.

    One parsing rule used by both the worker admission path and the
    control-plane scheduler (whose TypeScript twin, normalizeResources in
    lib/scheduler.ts, implements byte-identical semantics):

      * cpu: int/float (never bool), finite, > 0 -> float(cpu); else None.
      * memory: string matching ^\\d{1,6}[mMgG][iI]?$ ("256m", "512Mi",
        "1g", "2Gi") -> int megabytes; else None.

    Never raises: malformed/absent fields normalize to None so a bad
    manifest can never crash scheduling or admission — validation
    (validate_manifest) is the separate gate that rejects bad shapes.
    """
    cpu: Optional[float] = None
    ram_mb: Optional[int] = None
    if isinstance(resources, dict):
        raw_cpu = resources.get("cpu")
        if (
            _is_num(raw_cpu)
            and math.isfinite(raw_cpu)
            and raw_cpu > 0
        ):
            cpu = float(raw_cpu)
        raw_mem = resources.get("memory")
        if isinstance(raw_mem, str) and MEMORY_RE.match(raw_mem):
            try:
                ram_mb = parse_memory_mb(raw_mem)
            except ValueError:
                ram_mb = None
    return {"cpu": cpu, "ram_mb": ram_mb}
