#!/usr/bin/env python3
"""Validate every agent.deploy.json in examples/ against PROTOCOL §4.

Rules (from agent-host-platform/agent-sdk/protocol/PROTOCOL.md §4):
  - name: required, must match the project name (the example directory name)
  - runtime: one of docker | docker-compose | static
  - build.dockerfile (if build is present): must exist relative to the example dir
  - service.port: integer in 1..65535
  - resources.memory: like "256m", "512Mi", "1g" or "1Gi"  (canonical grammar)
  - resources.cpu: a positive number
  - restart: one of no | always | unless-stopped | on-failure
  - env (if present): object mapping string -> string

Exit 0 if every manifest is valid, 1 otherwise.
"""
import json
import re
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
RUNTIMES = {"docker", "docker-compose", "static"}
RESTARTS = {"no", "always", "unless-stopped", "on-failure"}
MEMORY_RE = re.compile(r"^\d{1,6}[mMgG][iI]?$")  # canonical resource grammar (one grammar everywhere)


def fail(errors, manifest, msg):
    errors.append(f"{manifest}: {msg}")


def is_positive_number(v):
    # Canonical contract: cpu is a JSON number, never a string or bool
    # (matches the worker's deployments/manifest.py _is_num).
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float)) and v > 0


def validate_manifest(path: Path, errors: list) -> None:
    label = path.parent.name + "/agent.deploy.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        fail(errors, label, f"unreadable/invalid JSON: {e}")
        return
    if not isinstance(manifest, dict):
        fail(errors, label, "top level must be a JSON object")
        return

    # name: required, must match project name (the example directory name)
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        fail(errors, label, "name is required and must be a non-empty string")
    elif name != path.parent.name:
        fail(errors, label, f"name {name!r} must match project name {path.parent.name!r}")

    # runtime enum
    runtime = manifest.get("runtime")
    if runtime not in RUNTIMES:
        fail(errors, label, f"runtime must be one of {sorted(RUNTIMES)}, got {runtime!r}")

    # build.dockerfile must exist if build is present
    build = manifest.get("build")
    if build is not None:
        if not isinstance(build, dict):
            fail(errors, label, "build must be an object")
        else:
            dockerfile = build.get("dockerfile", "Dockerfile")
            if not isinstance(dockerfile, str) or not (path.parent / dockerfile).is_file():
                fail(errors, label, f"build.dockerfile {dockerfile!r} does not exist")
            context = build.get("context", ".")
            if not isinstance(context, str) or not (path.parent / context).is_dir():
                fail(errors, label, f"build.context {context!r} is not a directory")

    # service.port 1..65535
    service = manifest.get("service")
    if not isinstance(service, dict):
        fail(errors, label, "service is required and must be an object")
    else:
        port = service.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            fail(errors, label, f"service.port must be an integer 1..65535, got {port!r}")
        hc = service.get("healthcheck")
        if hc is not None and (not isinstance(hc, str) or not hc.startswith("/")):
            fail(errors, label, f"service.healthcheck must be a path like '/health', got {hc!r}")

    # resources.memory like 256m|1g ; resources.cpu positive number
    resources = manifest.get("resources")
    if not isinstance(resources, dict):
        fail(errors, label, "resources is required and must be an object")
    else:
        memory = resources.get("memory")
        if not isinstance(memory, str) or not MEMORY_RE.match(memory):
            fail(errors, label, f"resources.memory must look like '256m', '512Mi' or '1g', got {memory!r}")
        cpu = resources.get("cpu")
        if not is_positive_number(cpu):
            fail(errors, label, f"resources.cpu must be a positive number, got {cpu!r}")

    # restart enum
    if manifest.get("restart") not in RESTARTS:
        fail(errors, label,
             f"restart must be one of {sorted(RESTARTS)}, got {manifest.get('restart')!r}")

    # env: object of string -> string
    env = manifest.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            fail(errors, label, "env must be an object mapping string -> string")


def main() -> int:
    manifests = sorted(EXAMPLES_DIR.glob("*/agent.deploy.json"))
    if not manifests:
        print("no agent.deploy.json files found under examples/")
        return 1
    errors: list = []
    for path in manifests:
        validate_manifest(path, errors)
    for m in manifests:
        status = "FAIL" if any(e.startswith(m.parent.name + "/") for e in errors) else "OK"
        print(f"[{status}] {m.parent.name}/agent.deploy.json")
    if errors:
        print("\nerrors:")
        for e in errors:
            print("  -", e)
        return 1
    print(f"\nall {len(manifests)} manifest(s) valid per PROTOCOL §4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
