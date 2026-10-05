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
import re
import shutil
import subprocess
from typing import Optional

DOCKER_BINARY = "docker"

# Safe for identifiers the worker mints (container names, image tags).
_SAFE_IDENT = re.compile(r"[^a-z0-9_.-]+")


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
             check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        full = [self.binary, *argv]
        try:
            proc = subprocess.run(
                full, timeout=timeout, check=False,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                text=True,
            )
        except FileNotFoundError as exc:
            raise DockerMissing(f"docker binary disappeared: {exc}")
        if check and proc.returncode != 0:
            raise DockerError(full, proc.returncode, (proc.stderr or "").strip())
        return proc

    # -- info ------------------------------------------------------------
    def version(self) -> str:
        return self._run("version", "--format", "{{.Server.Version}}").stdout.strip()

    def compose_available(self) -> bool:
        proc = self._run("compose", "version", check=False)
        return proc.returncode == 0

    # -- images ----------------------------------------------------------
    def build(self, context_dir: str, dockerfile: str, tag: str,
              build_args: Optional[dict] = None, timeout: int = 1200) -> str:
        argv = ["build", "-t", tag, "-f", dockerfile]
        for key, val in (build_args or {}).items():
            argv += ["--build-arg", f"{key}={val}"]
        argv.append(context_dir)
        proc = self._run(*argv, timeout=timeout)
        return (proc.stdout or "")[-4000:]

    def image_exists(self, tag: str) -> bool:
        proc = self._run("image", "inspect", tag, check=False)
        return proc.returncode == 0

    def remove_image(self, tag: str) -> None:
        self._run("rmi", tag, check=False)

    # -- containers ------------------------------------------------------
    def run(self, name: str, image: str,
            ports: Optional[dict] = None,
            env: Optional[dict] = None,
            memory: Optional[str] = None,
            cpus: Optional[str] = None,
            restart: str = "unless-stopped",
            extra_args: Optional[list] = None,
            timeout: int = 120) -> str:
        """docker run -d; returns the container id."""
        name = sanitize_ident(name)
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
        argv += list(extra_args or [])
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

    # -- compose ---------------------------------------------------------
    def compose_up(self, compose_file: str, project_name: Optional[str] = None,
                   build: bool = False, timeout: int = 1200) -> str:
        argv = ["compose", "-f", compose_file]
        if project_name:
            argv += ["-p", sanitize_ident(project_name)]
        argv += ["up", "-d"]
        if build:
            argv.append("--build")
        return self._run(*argv, timeout=timeout).stdout[-4000:]

    def compose_down(self, compose_file: str, project_name: Optional[str] = None,
                     timeout: int = 300) -> str:
        argv = ["compose", "-f", compose_file]
        if project_name:
            argv += ["-p", sanitize_ident(project_name)]
        argv += ["down"]
        return self._run(*argv, timeout=timeout, check=False).stdout[-4000:]
