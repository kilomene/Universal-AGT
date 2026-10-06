"""Thin wrapper over the docker CLI.

Every docker invocation is built as an argv list and run with
subprocess.run WITHOUT shell=True. Network-derived values (container
names, image tags, env values, ports) are passed as individual argv
elements and are additionally sanitized where they become identifiers.

Raises DockerMissing if the docker binary is absent, DockerError on
non-zero exit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Optional

DOCKER_BINARY = "docker"

# Safe for identifiers the worker mints (container names, image tags).
_SAFE_IDENT = re.compile(r"[^a-z0-9_.-]+")

# Restart policies the worker will pass to `docker run`. Anything else is
# rejected before the argv is built (fail closed, not docker's error).
RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")

# `--memory` / `--cpus` shapes accepted by run(). Values that do not match
# are rejected client-side so a malformed payload fails fast with a clear
# error instead of a cryptic docker CLI failure. The optional `i` suffix
# is accepted so every manifest-valid memory value (e.g. "512Mi" from the
# canonical resource grammar — deployments/manifest.py MEMORY_RE) passes
# through to Docker, whose own parser understands it.
_MEMORY_RE = re.compile(r"^\d+(\.\d+)?[bkmgBKMG][iI]?$")
_CPU_RE = re.compile(r"^\d+(\.\d+)?$")
_BUILD_ARG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

# Environment allowlist for `docker compose` subprocesses (W5).
#
# Compose interpolates ${VAR} in the compose file from the worker's OS
# environment — and the worker environment carries WORKER_HOST_TOKEN.
# An agent-controlled compose file with
#   environment: ["X=${WORKER_HOST_TOKEN}"]
# would otherwise exfiltrate the host token into a container the agent
# controls. Compose subprocesses therefore run with a scrubbed
# environment containing only non-secret operational variables.
# Defaults restrictive: anything not on this list is invisible to the
# compose file.
SAFE_COMPOSE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES",
})


def compose_subprocess_env() -> dict:
    """Scrubbed environment for `docker compose` subprocesses."""
    return {k: v for k, v in os.environ.items() if k in SAFE_COMPOSE_ENV_KEYS}


def validate_run_args(ports=None, memory=None, cpus=None,
                      restart: str = "unless-stopped") -> None:
    """Validate docker-run arguments client-side. Raises ValueError.

    Pure function — unit tested. The pipeline/handlers call run(), which
    invokes this before building the argv.
    """
    if restart not in RESTART_POLICIES:
        raise ValueError(
            f"invalid restart policy {restart!r}; must be one of "
            f"{list(RESTART_POLICIES)}"
        )
    if memory is not None and not _MEMORY_RE.match(str(memory)):
        raise ValueError(
            f"invalid memory limit {memory!r}; must look like '256m' or '1g'"
        )
    if cpus is not None:
        if not _CPU_RE.match(str(cpus)) or float(cpus) <= 0:
            raise ValueError(
                f"invalid cpu limit {cpus!r}; must be a positive number"
            )
    for host_port, container_port in (ports or {}).items():
        for value, what in ((host_port, "host"), (container_port, "container")):
            try:
                port = int(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"invalid {what} port {value!r}; must be an integer"
                )
            if not 1 <= port <= 65535:
                raise ValueError(
                    f"invalid {what} port {value!r}; must be 1..65535"
                )


def validate_build_args(build_args: Optional[dict]) -> None:
    """Build-arg keys become `docker --build-arg KEY=...` argv elements;
    keep them identifier-shaped so a key can never smuggle a CLI flag."""
    for key in (build_args or {}):
        if not _BUILD_ARG_KEY_RE.match(str(key)):
            raise ValueError(f"invalid build-arg key: {key!r}")


class DockerMissing(Exception):
    """The docker binary is not installed on this host."""


class DockerError(Exception):
    def __init__(self, argv: list, returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"docker {' '.join(argv[1:])} exited {returncode}: {stderr[:500]}"
        )


def sanitize_ident(value: str, max_len: int = 120) -> str:
    """Make a network-derived string safe for use as a container/image name."""
    cleaned = _SAFE_IDENT.sub("-", str(value).lower()).strip("-.")
    return cleaned[:max_len] or "unnamed"


def _reject_flag_like(value: str, what: str) -> str:
    """Fail closed on a value that would be parsed as a CLI flag.

    A positional argv element starting with "-" is parsed by the docker CLI
    (pflag) as a FLAG, not as the intended positional — an untrusted payload
    must never reach that parsing ambiguity (§17, first applied to run()'s
    image). Values consumed as a flag's own argument (after -t/-f/-e/-p)
    cannot hit this; only bare positionals need the guard.
    """
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise ValueError(f"invalid {what} {value!r}; must not start with '-'")
    return value


class DockerClient:
    def __init__(self, binary: str = DOCKER_BINARY):
        if shutil.which(binary) is None:
            raise DockerMissing(
                f"docker binary {binary!r} not found on PATH; "
                "install docker.io to run container workloads"
            )
        self.binary = binary

    # -- low level -------------------------------------------------------
    def _run(self, *argv: str, timeout: Optional[int] = 300,
             check: bool = True, capture: bool = True,
             env: Optional[dict] = None) -> subprocess.CompletedProcess:
        full = [self.binary, *argv]
        try:
            proc = subprocess.run(
                full, timeout=timeout, check=False,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise DockerMissing(f"docker binary disappeared: {exc}")
        if check and proc.returncode != 0:
            raise DockerError(full, proc.returncode, (proc.stderr or "").strip())
        return proc

    # -- info ------------------------------------------------------------
    def version(self, timeout: Optional[int] = 300) -> str:
        """Server version. Callers on a hot path (heartbeats) should pass a
        short timeout — a wedged docker daemon must never stall them."""
        return self._run("version", "--format", "{{.Server.Version}}",
                         timeout=timeout).stdout.strip()

    def compose_available(self) -> bool:
        proc = self._run("compose", "version", check=False)
        return proc.returncode == 0

    # -- images ----------------------------------------------------------
    def build(self, context_dir: str, dockerfile: str, tag: str,
              build_args: Optional[dict] = None, timeout: int = 1200) -> str:
        validate_build_args(build_args)
        # §17: context_dir is the last positional in the argv — a value
        # starting with "-" would be parsed as a flag, not the build
        # context. (dockerfile/tag are consumed as -f/-t values.)
        _reject_flag_like(context_dir, "build context")
        argv = ["build", "-t", tag, "-f", dockerfile]
        for key, val in (build_args or {}).items():
            argv += ["--build-arg", f"{key}={val}"]
        argv.append(context_dir)
        proc = self._run(*argv, timeout=timeout)
        return (proc.stdout or "")[-4000:]

    def image_exists(self, tag: str) -> bool:
        # §17: tag is a bare positional for `image inspect` — fail closed
        # on flag-shaped values (state-file tags reach this path via gc).
        _reject_flag_like(tag, "image tag")
        proc = self._run("image", "inspect", tag, check=False)
        return proc.returncode == 0

    def remove_image(self, tag: str) -> None:
        # §17: tag is a bare positional for `rmi` — see image_exists.
        _reject_flag_like(tag, "image tag")
        self._run("rmi", tag, check=False)

    # -- containers ------------------------------------------------------
    def run(self, name: str, image: str,
            ports: Optional[dict] = None,
            env: Optional[dict] = None,
            memory: Optional[str] = None,
            cpus: Optional[str] = None,
            restart: str = "unless-stopped",
            timeout: int = 120) -> str:
        """docker run -d; returns the container id.

        NOTE (security, 2026-10-05): there is deliberately NO ``extra_args``
        parameter. Caller-supplied docker flags were an argv-injection path
        to host root (``--privileged``, ``-v /:/host``); they were removed
        from the protocol and are rejected by policy before execution.

        EXPLICIT DENY LIST (W5): ``docker run`` NEVER receives any of the
        following — they are not parameters of this method at all, so no
        caller, however compromised, can smuggle them into the argv built
        here:

          * ``--privileged``
          * ``--network host`` (host networking)
          * ``--pid host`` / ``--ipc host`` / ``--uts host``
          * ``--device`` (host device access)
          * ``-v /var/run/docker.sock:...`` (docker socket mount)
          * ``-v`` / ``--volume`` / ``--mount`` (any host bind mount —
            containers run with no host filesystem access at all)
          * ``--cap-add`` (added capabilities)
          * ``--security-opt`` (custom security options)

        What run() DOES accept is allowlisted and validated client-side by
        validate_run_args(): restart policy, --memory/--cpus shapes, and
        1..65535 port mappings. Defaults restrictive: a bare
        run(name, image) publishes no ports and mounts nothing.
        """
        validate_run_args(ports=ports, memory=memory, cpus=cpus,
                          restart=restart)
        name = sanitize_ident(name)
        # §17: the image is the last positional in the argv. A value
        # starting with "-" would be parsed by the docker CLI as a FLAG
        # (pflag), not as the image — an untrusted payload must never be
        # able to reach that parsing ambiguity. Reject it outright.
        if not image or not isinstance(image, str) or image.startswith("-"):
            raise ValueError(f"invalid image {image!r}; must not start with '-'")
        argv = ["run", "-d", "--name", name]
        for host_port, container_port in (ports or {}).items():
            argv += ["-p", f"{int(host_port)}:{int(container_port)}"]
        for key, val in (env or {}).items():
            argv += ["-e", f"{key}={val}"]
        if memory:
            argv += ["--memory", str(memory)]
        if cpus:
            argv += ["--cpus", str(cpus)]
        if restart:
            argv += ["--restart", str(restart)]
        argv += [image]
        return self._run(*argv, timeout=timeout).stdout.strip()

    def start(self, name: str, timeout: int = 120) -> None:
        self._run("start", sanitize_ident(name), timeout=timeout)

    def stop(self, name: str, timeout_secs: int = 10, timeout: int = 120) -> None:
        self._run("stop", "-t", str(timeout_secs), sanitize_ident(name),
                  timeout=timeout, check=False)

    def restart_container(self, name: str, timeout: int = 120) -> None:
        self._run("restart", sanitize_ident(name), timeout=timeout)

    def rm(self, name: str, force: bool = False, timeout: int = 120) -> None:
        argv = ["rm"]
        if force:
            argv.append("-f")
        argv.append(sanitize_ident(name))
        self._run(*argv, timeout=timeout, check=False)

    def container_exists(self, name: str) -> bool:
        proc = self._run("inspect", sanitize_ident(name), check=False)
        return proc.returncode == 0

    def ps(self, all: bool = False) -> list:
        argv = ["ps"]
        if all:
            argv.append("-a")
        argv += ["--format", "{{json .}}"]
        out = self._run(*argv).stdout
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    def logs(self, name: str, tail: int = 500) -> str:
        proc = self._run("logs", "--tail", str(tail), sanitize_ident(name),
                         check=False)
        return ((proc.stdout or "") + (proc.stderr or ""))[-200000:]

    def inspect(self, name: str) -> list:
        out = self._run("inspect", sanitize_ident(name)).stdout
        return json.loads(out)

    def container_status(self, name: str) -> Optional[str]:
        """'running' | 'exited' | ... | None if the container does not exist."""
        try:
            data = self.inspect(name)
        except DockerError:
            return None
        if not data:
            return None
        return (data[0].get("State") or {}).get("Status")

    def restart_count(self, name: str) -> Optional[int]:
        """Container RestartCount from docker inspect; None if it does not exist."""
        try:
            data = self.inspect(name)
        except DockerError:
            return None
        if not data:
            return None
        try:
            return int(data[0].get("RestartCount") or 0)
        except (TypeError, ValueError):
            return 0

    # -- compose ---------------------------------------------------------
    # Every compose invocation runs with compose_subprocess_env(): the
    # compose file interpolates ${VAR} from the subprocess environment,
    # and the worker's real environment carries WORKER_HOST_TOKEN. The
    # scrubbed env keeps the token (and every other secret) invisible
    # to agent-controlled compose files.
    def compose_up(self, compose_file: str, project_name: Optional[str] = None,
                   build: bool = False, timeout: int = 1200) -> str:
        argv = ["compose", "-f", compose_file]
        if project_name:
            argv += ["-p", sanitize_ident(project_name)]
        argv += ["up", "-d"]
        if build:
            argv.append("--build")
        return self._run(*argv, timeout=timeout,
                         env=compose_subprocess_env()).stdout[-4000:]

    def compose_down(self, compose_file: str, project_name: Optional[str] = None,
                     timeout: int = 300) -> str:
        argv = ["compose", "-f", compose_file]
        if project_name:
            argv += ["-p", sanitize_ident(project_name)]
        argv += ["down"]
        return self._run(*argv, timeout=timeout, check=False,
                         env=compose_subprocess_env()).stdout[-4000:]

    def _compose_lifecycle(self, subcommand: str, project_name: str,
                           timeout: int = 300) -> str:
        """`docker compose -p <name> <restart|stop|start>`.

        Operates on the project's labeled containers — no compose file
        needed, mirroring compose_ps. Runs with the scrubbed compose env
        like every other compose invocation.
        """
        return self._run("compose", "-p", sanitize_ident(project_name),
                         subcommand, timeout=timeout,
                         env=compose_subprocess_env()).stdout[-4000:]

    def compose_restart(self, project_name: str, timeout: int = 300) -> str:
        return self._compose_lifecycle("restart", project_name,
                                       timeout=timeout)

    def compose_stop(self, project_name: str, timeout: int = 300) -> str:
        return self._compose_lifecycle("stop", project_name, timeout=timeout)

    def compose_start(self, project_name: str, timeout: int = 300) -> str:
        return self._compose_lifecycle("start", project_name, timeout=timeout)

    def compose_ps(self, project_name: str, timeout: int = 60) -> list:
        """`docker compose -p <name> ps --format json`, parsed to a list of
        dicts. Each record carries Name, State and a Publishers list
        ([{PublishedPort, TargetPort, Protocol, ...}]). Tolerates both the
        one-JSON-object-per-line format and a single JSON array. Never
        raises on unparseable output — returns what parsed."""
        argv = ["compose", "-p", sanitize_ident(project_name),
                "ps", "--format", "json"]
        out = (self._run(*argv, timeout=timeout, check=False,
                         env=compose_subprocess_env()).stdout or "").strip()
        records: list = []
        if not out:
            return records
        if out.startswith("["):
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError:
                parsed = []
            records = [r for r in parsed if isinstance(r, dict)]
        else:
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
        return records

    def compose_config_json(self, compose_file: str, timeout: int = 60) -> dict:
        """Normalized compose model via `docker compose config --format
        json`. Used to read published ports before `compose up` so the
        worker can verify them free. Raises DockerError on failure."""
        out = self._run("compose", "-f", compose_file, "config",
                        "--format", "json",
                        timeout=timeout,
                        env=compose_subprocess_env()).stdout
        data = json.loads(out)
        return data if isinstance(data, dict) else {}
