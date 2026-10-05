"""Validation of agent.deploy.json per PROTOCOL §4.

Pure function: validate_manifest(data, project_name=None) -> (ok, errors).

Rules enforced:
  * name: required, non-empty string; must equal project_name when given
  * runtime: required, one of docker | docker-compose | static
  * service.port: optional, integer 1..65535
  * service.healthcheck: optional, string starting with "/"
  * resources.memory: optional, like 256m | 1g (regex ^\\d+[mMgG]$)
  * resources.cpu: optional, positive number
  * restart: optional, one of no | always | unless-stopped | on-failure
  * build.dockerfile / build.context: optional non-empty strings
  * env: optional mapping of string keys to scalar values
Unknown top-level keys are ignored (forward compatibility); unknown keys
inside known sections are ignored as well.
"""
from __future__ import annotations

import re
from typing import Any, Optional

RUNTIMES = ("docker", "docker-compose", "static")
RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")
MEMORY_RE = re.compile(r"^\d+[mMgG]$")


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
                    "resources.memory: must look like '256m' or '1g', "
                    f"got {memory!r}"
                )
            cpu = resources.get("cpu")
            if cpu is not None and (not _is_num(cpu) or cpu <= 0):
                errors.append(
                    f"resources.cpu: must be a positive number, got {cpu!r}"
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

    return (len(errors) == 0), errors


def parse_memory_mb(memory: str) -> int:
    """'256m' -> 256, '1g' -> 1024, '2G' -> 2048. Raises ValueError if invalid."""
    if not isinstance(memory, str) or not MEMORY_RE.match(memory):
        raise ValueError(f"invalid memory spec: {memory!r}")
    amount = int(memory[:-1])
    return amount * 1024 if memory[-1].lower() == "g" else amount
