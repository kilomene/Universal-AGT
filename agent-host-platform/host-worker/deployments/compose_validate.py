"""Compose-file security validation (spec W5 docker lockdown, W7 compose).

``docker compose up`` executes an agent-controlled YAML file. Before the
worker ever runs it, the NORMALIZED model (``docker compose config
--format json``) is validated here against an explicit DENY list.

DENY (per service):
  * ``privileged: true``
  * ``network_mode: host``
  * ``pid: host`` | ``ipc: host`` | ``uts: host`` | ``userns_mode: host``
  * ``devices: [...]``            (any host device node)
  * ``cap_add: [...]``            (any added Linux capability)
  * ``security_opt: [...]``      (any custom security option)
  * ``runtime: <anything>``       (custom OCI runtime selection)
  * ``volumes`` whose host source escapes the deployment workspace
    (bind mounts; this includes /var/run/docker.sock anywhere, even
    smuggled in under a workspace-relative name)
  * ``env_file`` / ``build.context`` / ``build.dockerfile`` /
    ``extends.file`` / top-level ``configs``/``secrets`` ``file:``
    entries that escape the deployment workspace
  * malformed ``deploy.resources`` limits (fail closed)

ALLOW (explicitly): named volumes, tmpfs mounts, workspace-relative bind
mounts, bridge networks, published ports (the caller verifies them free
and registers them in the port registry), well-formed resource limits.

Pure functions: validate_compose_model(model, workspace, compose_file)
-> list of human-readable error strings. Callers raise their own
DeployError/HandlerError on any error. Defaults deny: anything not
understood as safe is rejected.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Explicit DENY list (W5)
# ---------------------------------------------------------------------------

#: Service flags that are unconditionally denied when truthy.
DENIED_SERVICE_FLAGS = ("privileged",)

#: service key -> set of denied values (host-namespace escapes).
DENIED_HOST_NAMESPACES = {
    "network_mode": {"host"},
    "pid": {"host"},
    "ipc": {"host"},
    "uts": {"host"},
    # userns_mode: host disables user-namespace remapping — on hosts with
    # userns-remap configured this maps container root to host root.
    "userns_mode": {"host"},
}

#: Service keys denied whenever they are set and non-empty.
#:
#: * devices / cap_add / security_opt — direct host-escape primitives;
#: * runtime — selects the OCI runtime binary; a non-default runtime is an
#:   intentional host-level capability, never an agent default.
DENIED_WHEN_NONEMPTY = ("devices", "cap_add", "security_opt", "runtime")

#: Namespace-sharing keys whose values must never reference an arbitrary
#: container. `network_mode: "container:<name>"`, `pid: "container:<name>"`
#: and `ipc: "container:<name>"` join the network/PID/IPC namespace of ANY
#: container on the host — including other deployments' containers or host
#: infrastructure. (W16 audit: previously only the literal "host" was
#: denied.) `service:<name>` stays allowed: it references a sibling service
#: in the same compose file, which the agent already fully controls.
DENIED_NAMESPACE_JOIN_PREFIXES = ("container:",)

#: Basenames that are never mountable, wherever they resolve.
DENIED_MOUNT_BASENAMES = ("docker.sock",)

#: Restart policies / memory / cpu formats accepted for resource limits.
_CPU_RE = re.compile(r"^\d+(\.\d+)?$")
_MEMORY_RE = re.compile(r"^\d+(\.\d+)?[bkmgBKMG][iI]?$|^\d+$")  # optional `i` suffix: "512Mi" is manifest-valid


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolve_lexical(p: Path) -> Path:
    """Resolve symlinks for existing components, lexical otherwise."""
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.normpath(str(p)))


def _confined(path_str: str, file_dir: Path, workspace: Path,
               what: str) -> str | None:
    """Return an error string if ``path_str`` escapes ``workspace``.

    Relative paths resolve against ``file_dir`` (the compose file's
    directory, matching compose semantics). ``~`` is expanded. Symlinks
    in existing path components are followed before the check, so a
    workspace-relative symlink pointing at /etc is still rejected.
    """
    raw = os.path.expanduser(str(path_str))
    p = Path(raw)
    if not p.is_absolute():
        p = file_dir / p
    resolved = _resolve_lexical(p)
    if resolved == workspace or workspace in resolved.parents:
        if resolved.name in DENIED_MOUNT_BASENAMES:
            return (f"{what} {path_str!r} resolves to a denied mount target "
                    f"({resolved.name})")
        return None
    return f"{what} {path_str!r} escapes the deployment workspace"


def _classify_volume_source(source: str) -> str:
    """'bind' for host paths, 'named' for named volumes (compose rules)."""
    if source.startswith((".", "/", "~")) or "/" in source:
        return "bind"
    return "named"


# ---------------------------------------------------------------------------
# Volume / mount validation
# ---------------------------------------------------------------------------

def _short_volume_source(spec: str) -> str | None:
    """Host source of a short-syntax volume spec, or None if sourceless."""
    parts = str(spec).split(":")
    if len(parts) == 1:
        return None  # target-only (anonymous volume)
    # [SOURCE:]TARGET[:MODE] — MODE never contains a path, SOURCE may on
    # Windows only (not our platform).
    return parts[0] or None


def _check_volume_source(source: str | None, vtype: str | None,
                         file_dir: Path, workspace: Path, svc: str,
                         what: str, errors: list) -> None:
    if not source:
        return  # anonymous volume / tmpfs without source
    kind = (vtype or "").lower() or (
        "bind" if _classify_volume_source(source) == "bind" else "volume")
    if kind == "bind":
        err = _confined(source, file_dir, workspace,
                        f"service {svc!r}: bind mount source ({what})")
        if err:
            errors.append(err)
    elif kind == "npipe":
        errors.append(
            f"service {svc!r}: npipe mounts are not allowed ({what})")
    # kind == "volume" (named), "tmpfs", "cluster": no host source.


def _check_service_volumes(svc_name: str, svc: dict, file_dir: Path,
                           workspace: Path, errors: list) -> None:
    for idx, entry in enumerate(svc.get("volumes") or []):
        what = f"volumes[{idx}]"
        if isinstance(entry, str):
            _check_volume_source(_short_volume_source(entry), None,
                                 file_dir, workspace, svc_name, what, errors)
        elif isinstance(entry, dict):
            _check_volume_source(entry.get("source"), entry.get("type"),
                                 file_dir, workspace, svc_name, what, errors)
        elif entry is not None:
            errors.append(
                f"service {svc_name!r}: {what} must be a string or mapping")


def _check_file_refs(svc_name: str, svc: dict, file_dir: Path,
                     workspace: Path, errors: list) -> None:
    """env_file / configs[].file / secrets[].file confinement."""
    env_file = svc.get("env_file")
    entries: list = []
    if isinstance(env_file, str):
        entries = [env_file]
    elif isinstance(env_file, list):
        entries = env_file
    elif env_file is not None:
        errors.append(f"service {svc_name!r}: env_file must be a string or list")
    for idx, item in enumerate(entries):
        path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(path, str) or not path:
            errors.append(
                f"service {svc_name!r}: env_file[{idx}] must name a file")
            continue
        err = _confined(path, file_dir, workspace,
                        f"service {svc_name!r}: env_file[{idx}]")
        if err:
            errors.append(err)


def _check_build(svc_name: str, svc: dict, file_dir: Path,
                 workspace: Path, errors: list) -> None:
    build = svc.get("build")
    if build is None:
        return
    cfg = {"context": build} if isinstance(build, str) else build
    if not isinstance(cfg, dict):
        errors.append(f"service {svc_name!r}: build must be a string or mapping")
        return
    context = cfg.get("context", ".")
    if not isinstance(context, str) or not context:
        errors.append(f"service {svc_name!r}: build.context must be a string")
    else:
        err = _confined(context, file_dir, workspace,
                        f"service {svc_name!r}: build.context")
        if err:
            errors.append(err)
    dockerfile = cfg.get("dockerfile")
    if dockerfile is not None:
        if not isinstance(dockerfile, str) or not dockerfile:
            errors.append(
                f"service {svc_name!r}: build.dockerfile must be a string")
        else:
            # Resolved against the context dir, like the daemon does.
            ctx_dir = file_dir / context if isinstance(context, str) else file_dir
            err = _confined(dockerfile, ctx_dir, workspace,
                            f"service {svc_name!r}: build.dockerfile")
            if err:
                errors.append(err)
    # build.args keys become `docker --build-arg KEY=...`; keep them
    # identifier-shaped so a key can never smuggle a CLI flag.
    args = cfg.get("args")
    if isinstance(args, dict):
        for key in args:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", str(key)):
                errors.append(
                    f"service {svc_name!r}: build.args key {key!r} is not "
                    f"a valid identifier")


def _check_extends(svc_name: str, svc: dict, file_dir: Path,
                   workspace: Path, errors: list) -> None:
    extends = svc.get("extends")
    if not isinstance(extends, dict):
        return
    ref = extends.get("file")
    if ref is not None:
        err = _confined(str(ref), file_dir, workspace,
                        f"service {svc_name!r}: extends.file")
        if err:
            errors.append(err)


def _check_resource_value(where: str, key: str, value: Any,
                           errors: list) -> None:
    if key in ("cpus",):
        if not _CPU_RE.match(str(value)):
            errors.append(f"{where}: {key} must be a positive number, "
                          f"got {value!r}")
    elif key in ("memory", "mem_limit", "memswap_limit", "mem_reservation"):
        if not _MEMORY_RE.match(str(value)):
            errors.append(f"{where}: {key} must look like '512m' or '1g', "
                          f"got {value!r}")


def _check_resources(svc_name: str, svc: dict, errors: list) -> None:
    where = f"service {svc_name!r}"
    deploy = svc.get("deploy") or {}
    if isinstance(deploy, dict):
        resources = deploy.get("resources") or {}
        if isinstance(resources, dict):
            for section in ("limits", "reservations"):
                part = resources.get(section) or {}
                if isinstance(part, dict):
                    for key, value in part.items():
                        _check_resource_value(
                            f"{where}: deploy.resources.{section}", key,
                            value, errors)
    # Legacy top-level resource keys.
    for key in ("mem_limit", "memswap_limit", "mem_reservation", "cpus"):
        if svc.get(key) is not None:
            _check_resource_value(where, key, svc[key], errors)


# ---------------------------------------------------------------------------
# Top-level volumes with bind driver_opts
# ---------------------------------------------------------------------------

def _check_top_level_volumes(model: dict, file_dir: Path,
                             workspace: Path, errors: list) -> None:
    volumes = model.get("volumes") or {}
    if not isinstance(volumes, dict):
        return
    for name, definition in volumes.items():
        if not isinstance(definition, dict):
            continue
        driver_opts = definition.get("driver_opts") or {}
        if not isinstance(driver_opts, dict):
            continue
        device = driver_opts.get("device")
        dtype = str(driver_opts.get("type", "")).lower()
        opts = str(driver_opts.get("o", "")).lower()
        if device and (dtype in ("none", "bind") or "bind" in opts):
            err = _confined(str(device), file_dir, workspace,
                            f"top-level volume {name!r} bind device")
            if err:
                errors.append(err)


def _check_top_level_file_defs(model: dict, file_dir: Path,
                               workspace: Path, errors: list) -> None:
    for section in ("configs", "secrets"):
        defs = model.get(section) or {}
        if not isinstance(defs, dict):
            continue
        for name, definition in defs.items():
            if not isinstance(definition, dict):
                continue
            ref = definition.get("file")
            if ref is not None:
                err = _confined(str(ref), file_dir, workspace,
                                f"top-level {section} {name!r} file")
                if err:
                    errors.append(err)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_compose_model(model: Any, workspace: str | Path,
                           compose_file: str | Path | None = None) -> list[str]:
    """Validate a normalized compose model. Returns a list of errors
    (empty = safe to `compose up`)."""
    errors: list[str] = []
    if not isinstance(model, dict):
        return ["compose model must be a JSON object"]
    ws = _resolve_lexical(Path(workspace))
    file_dir = (_resolve_lexical(Path(compose_file).parent)
                if compose_file else ws)

    services = model.get("services")
    if not isinstance(services, dict):
        return ["compose 'services' must be a mapping"]
    if not services:
        errors.append("compose file defines no services")

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"service {svc_name!r}: must be a mapping")
            continue
        _validate_service(str(svc_name), svc, file_dir, ws, errors)

    _check_top_level_volumes(model, file_dir, ws, errors)
    _check_top_level_file_defs(model, file_dir, ws, errors)
    return errors


def _validate_service(svc_name: str, svc: dict, file_dir: Path,
                      workspace: Path, errors: list) -> None:
    # -- explicit DENY list ------------------------------------------------
    for flag in DENIED_SERVICE_FLAGS:
        if svc.get(flag):
            errors.append(
                f"service {svc_name!r}: {flag} is denied by security policy")
    for key, denied_values in DENIED_HOST_NAMESPACES.items():
        value = svc.get(key)
        if isinstance(value, str):
            if value.lower() in denied_values:
                errors.append(
                    f"service {svc_name!r}: {key}={value!r} is denied "
                    f"(host namespace escape)")
            elif key in ("network_mode", "pid", "ipc") and value.lower().startswith(
                DENIED_NAMESPACE_JOIN_PREFIXES
            ):
                errors.append(
                    f"service {svc_name!r}: {key}={value!r} is denied "
                    f"(cross-container namespace sharing)")
    for key in DENIED_WHEN_NONEMPTY:
        if svc.get(key):
            errors.append(
                f"service {svc_name!r}: {key} is denied by security policy")

    # -- mounts / file references (workspace confinement) ------------------
    _check_service_volumes(svc_name, svc, file_dir, workspace, errors)
    _check_file_refs(svc_name, svc, file_dir, workspace, errors)
    _check_build(svc_name, svc, file_dir, workspace, errors)
    _check_extends(svc_name, svc, file_dir, workspace, errors)

    # -- resource limits (fail closed on malformed values) ------------------
    _check_resources(svc_name, svc, errors)
