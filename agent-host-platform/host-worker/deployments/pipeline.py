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
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from deployments.manifest import parse_memory_mb, validate_manifest
from deployments import compose_validate
from deployments import gc
from deployments import rollback as rollback_mod
from health import checker as health_checker
from health import collector as health_collector

MANIFEST_FILENAME = "agent.deploy.json"
QUARANTINE_DIRNAME = "quarantine"

# Archive safety caps (§16). The wire is capped server-side
# (ARTIFACT_MAX_BYTES, default 500MB), but a small compressed archive can
# expand to many gigabytes — a decompression bomb that fills the worker
# disk. The pre-scan in extract_archive() therefore rejects archives whose
# DECLARED uncompressed size (tar member sizes / zip file_size headers)
# exceeds MAX_EXTRACT_BYTES, or whose member count exceeds
# MAX_ARCHIVE_MEMBERS (inode/CPU exhaustion via millions of tiny members).
#
# The cap is ABSOLUTE, not a compression-ratio cap: legitimate artifacts
# are routinely highly compressible (200KB of zeros -> 291 bytes on the
# wire is a 687x ratio), so a ratio cap would break real deploys. What
# matters is bounding actual disk writes, and declared sizes bound them:
# tar reads exactly member.size bytes per member, and zip enforces
# file_size (a lying header fails CRC and aborts extraction).
MAX_EXTRACT_BYTES = 8 * 1024**3  # 8 GiB
MAX_ARCHIVE_MEMBERS = 100_000

# artifact_id values become the path component artifacts/<id>.bin. They are
# server-minted UUIDs, but the worker builds the path from the task payload
# (agent-controlled) — validate before any path use so ".." / "/" can never
# steer the download outside <work_dir>/artifacts (§16).
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def validate_artifact_id(artifact_id) -> str:
    """Return artifact_id if it is safe as a filesystem path component.

    Raises ValueError otherwise — callers wrap it in their own fatal error
    type (DeployError / HandlerError / WorkerAPIError).
    """
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.match(artifact_id):
        raise ValueError(
            f"invalid artifact_id {artifact_id!r}; refusing to build a "
            f"filesystem path from it"
        )
    return artifact_id
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
    # Reject malformed digests before compare_digest: non-ASCII input
    # raises TypeError (an unhandled traceback), and a wrong-length digest
    # can never match. The checksum comes from the task payload
    # (agent-controlled), so validate it like any other untrusted input.
    if not re.fullmatch(r"[0-9a-fA-F]{64}", hex_digest):
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

    Decompression-bomb guard (§16): the declared uncompressed total (tar
    member sizes / zip file_size — the bytes extraction would actually
    write) is capped at MAX_EXTRACT_BYTES (8 GiB) and the member count at
    MAX_ARCHIVE_MEMBERS. The tar header scan is incremental: the running
    total is checked BEFORE tarfile seeks past each member's data, so a
    bomb's payload is never even skipped over.

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

    def _check_bomb(members: list[tuple[str, int]], what: str) -> None:
        # Decompression-bomb guard (§16), zip branch: reject archives whose
        # declared uncompressed payload exceeds the absolute extraction cap
        # (infolist() reads the central directory only, so no member data
        # is touched before this check). Declared sizes are what extraction
        # writes (zip verifies file_size via CRC), so a lying header fails
        # closed either here or at extraction.
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise DeployError(
                f"archive rejected: {len(members)} members exceeds the "
                f"{MAX_ARCHIVE_MEMBERS} member cap ({what})"
            )
        total = sum(size for _, size in members)
        if total > MAX_EXTRACT_BYTES:
            raise DeployError(
                f"archive rejected: declared uncompressed size {total} bytes "
                f"exceeds the {MAX_EXTRACT_BYTES} byte extraction cap; "
                f"refusing a probable decompression bomb ({what})"
            )

    try:
        if tarfile.is_tarfile(archive_path):
            # Header scan, incremental: member names are confinement-checked
            # and the running uncompressed total is capped BEFORE tarfile
            # seeks past each member's data — a bomb's payload is never even
            # skipped over, let alone extracted. (getmembers() would stream
            # through the whole payload first: CPU burn on a hostile archive.)
            # Extraction is a second open: the scan consumes the stream.
            with tarfile.open(archive_path, "r") as tf:
                count = 0
                total = 0
                while True:
                    member = tf.next()
                    if member is None:
                        break
                    # Pre-scan every member name: data_filter neutralizes
                    # absolute paths by stripping the leading "/" (extracting
                    # them inside dest), but a malicious archive must FAIL
                    # with a clear error, not silently land renamed files.
                    _safe_join(dest, member.name)
                    count += 1
                    total += member.size
                    if count > MAX_ARCHIVE_MEMBERS:
                        raise DeployError(
                            f"archive rejected: more than "
                            f"{MAX_ARCHIVE_MEMBERS} members (tar)"
                        )
                    if total > MAX_EXTRACT_BYTES:
                        raise DeployError(
                            f"archive rejected: declared uncompressed size "
                            f"{total} bytes exceeds the {MAX_EXTRACT_BYTES} "
                            f"byte extraction cap; refusing a probable "
                            f"decompression bomb (tar)"
                        )
            with tarfile.open(archive_path, "r") as tf:
                tf.extractall(dest, filter="data")
        elif zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, "r") as zf:
                # infolist() reads the central directory only — no member
                # data is touched before the caps below are enforced.
                members = [(i.filename, i.file_size) for i in zf.infolist()]
                _check_bomb(members, "zip")
                for name, _ in members:
                    _safe_join(dest, name)
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


# ---------------------------------------------------------------------------
# Process-wide host-port reservation (spec §42).
#
# pick_free_port(store.used_host_ports()) is a check-then-act race when two
# deploy() calls run on threads in the same worker process (the production
# claim loop dispatches on a ThreadPoolExecutor): both can read the same
# store state and pick the same port before either container binds it. The
# physical port checks catch the collision, but the losing deployment then
# fails spuriously.
#
# Reservation closes the race: a picked port is reserved process-wide from
# pick time until the deployment's own state row (which then carries the
# host_port) is persisted — later picks consult the store AND the
# reservation set. Compose deployments declare their own ports, so they
# reserve their declared ports for the same window instead of picking.
# ---------------------------------------------------------------------------
_port_alloc_lock = threading.Lock()
_port_reservations: dict[int, str] = {}  # host_port -> deployment_id


def reserve_free_port(store, log=None, deployment_id="") -> int:
    """Pick a free host port and reserve it for this deployment."""
    with _port_alloc_lock:
        used = set(store.used_host_ports()) | set(_port_reservations)
        port = pick_free_port(used, log=log)
        _port_reservations[port] = deployment_id
        return port


def reserve_declared_ports(ports, deployment_id="") -> list[int]:
    """Reserve app-declared (compose) host ports for this deployment.

    Refuses when another in-flight deployment already reserved one of
    them — a genuine collision, failed loudly before `compose up`.
    """
    wanted = sorted({int(p) for p in ports or []})
    with _port_alloc_lock:
        dupes = [p for p in wanted if p in _port_reservations]
        if dupes:
            raise DeployError(
                f"host port(s) {dupes} already reserved by a concurrent "
                f"deployment; refusing to collide")
        for p in wanted:
            _port_reservations[p] = deployment_id
    return wanted


def release_port_reservations(ports) -> None:
    """Release reservations; safe to call with None/empty/duplicates."""
    with _port_alloc_lock:
        for p in ports or []:
            try:
                _port_reservations.pop(int(p), None)
            except (TypeError, ValueError):
                pass


def reserved_port_snapshot() -> set:
    """Current reservations (for registry_used-style collision checks)."""
    with _port_alloc_lock:
        return set(_port_reservations)


# ---------------------------------------------------------------------------
# Process-wide resource admission (spec §11).
#
# The capacity check below used to be check-then-act: two deploy() calls on
# threads in the same worker process could both read the same reserved
# totals and both admit, over-committing host CPU/RAM. Admission is
# therefore a reservation, mirroring the port pattern above: the capacity
# check AND the record of this deployment's own request happen atomically
# under _resource_alloc_lock. The reservation is held from admission until
# the deployment's state row is persisted (the row then carries the
# resources itself, so accounting never double-counts) or until the deploy
# fails — the section-8 finally releases it on every path.
# ---------------------------------------------------------------------------
_resource_alloc_lock = threading.Lock()
_resource_reservations: dict[str, dict] = {}  # deployment_id -> {"cpu", "ram_mb"}


def reserved_resources_snapshot() -> dict:
    """Currently in-flight resource reservations (test introspection)."""
    with _resource_alloc_lock:
        return {dep: dict(r) for dep, r in _resource_reservations.items()}


def reserve_resources(deployment_id: str, cpu: float, ram_mb: float,
                     store, log=None) -> None:
    """Atomically check host capacity and reserve this deployment's request.

    ``allocated`` = persisted reservations from deployments in reserving
    states (health.collector.allocated_resources) PLUS in-flight
    reservations from concurrent deploys that have passed admission but
    not yet persisted their state row. Raises DeployError when the request
    does not fit — the reservation is then NOT recorded, so a rejected
    deploy holds nothing. Release with release_resource_reservation()
    once the state row is persisted (success) or on any failure path.
    """
    with _resource_alloc_lock:
        allocated = health_collector.allocated_resources(store)
        for r in _resource_reservations.values():
            allocated["cpu"] += r["cpu"]
            allocated["ram_mb"] += r["ram_mb"]
        if ram_mb:
            total_mb = health_collector.total_ram_mb()
            avail_mb = total_mb - allocated["ram_mb"]
            if log:
                log(f"resource check: need {ram_mb:g}MB ram, host total "
                    f"{total_mb}MB, reserved {allocated['ram_mb']}MB, "
                    f"available {avail_mb:.0f}MB")
            if total_mb > 0 and ram_mb > avail_mb:
                raise DeployError(
                    f"insufficient reservable memory: deployment requests "
                    f"{ram_mb:g}MB but only {max(avail_mb, 0):.0f}MB is "
                    f"available (host total {total_mb}MB, "
                    f"{allocated['ram_mb']}MB already reserved)"
                )
        if cpu:
            total_cpus = health_collector.total_cpu()
            avail_cpu = total_cpus - allocated["cpu"]
            if log:
                log(f"resource check: need {cpu:g} cpu, host total "
                    f"{total_cpus}, reserved {allocated['cpu']}, "
                    f"available {avail_cpu:.2f}")
            if total_cpus > 0 and cpu > avail_cpu:
                raise DeployError(
                    f"insufficient reservable CPU: deployment requests "
                    f"{cpu:g} but only {max(avail_cpu, 0):.2f} is "
                    f"available (host total {total_cpus}, "
                    f"{allocated['cpu']} already reserved)"
                )
        _resource_reservations[deployment_id] = {
            "cpu": float(cpu or 0.0), "ram_mb": float(ram_mb or 0.0)}


def release_resource_reservation(deployment_id: str) -> None:
    """Release an admission reservation; safe to call when none exists."""
    with _resource_alloc_lock:
        _resource_reservations.pop(deployment_id, None)


# ---------------------------------------------------------------------------
# Per-artifact download lock (spec §42).
#
# Two concurrent deploys of the SAME artifact_id write to the same
# artifacts/<id>.bin. Without serialization the bytes interleave and the
# SHA-256 verification fails (or worse, passes on a torn file neither
# deploy intended). One lock per artifact_id; different artifacts download
# fully in parallel.
# ---------------------------------------------------------------------------
_artifact_dl_guard = threading.Lock()
_artifact_dl_locks: dict[str, threading.Lock] = {}


def artifact_download_lock(artifact_id: str) -> threading.Lock:
    """Process-wide mutex for downloading one artifact id."""
    with _artifact_dl_guard:
        return _artifact_dl_locks.setdefault(str(artifact_id),
                                             threading.Lock())


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


def compose_project_name_for(project_id: str, safe_project: str) -> str:
    """Compose project name, isolated per project (§18).

    The sanitized project name alone is not a unique key: "My App" and
    "my-app" sanitize identically, and without the project_id suffix one
    project's `compose up` would adopt (and tear down) the other's stack.
    Pure function — unit tested.
    """
    pid8 = re.sub(r"[^a-z0-9]+", "", str(project_id).lower())[:8] or "noid"
    return f"uaht-{safe_project}-{pid8}"


def verify_host_port_free(docker, host_port: int, log=None,
                          exclude_names: Optional[set] = None,
                          tolerate_bound_by: Optional[set] = None) -> None:
    """Fail with DeployError unless host_port is free, twice over.

    Docker level: scan `docker ps -a` published ports — catches ports
    docker holds on a specific interface that a wildcard bind might miss.
    OS level: bind the port ourselves — catches anything listening on the
    host (including docker's own published-port proxy).

    ``exclude_names`` (optional): container names whose published ports do
    not count as a collision — used by rollback verification, where the
    target's own stopped container legitimately still shows its port in
    `docker ps -a` (`docker start` reuses that mapping).

    ``tolerate_bound_by`` (optional): container names allowed to be the
    SOLE reason an OS-bind probe fails. Used by rollback verification for
    the same-port case: the port is bound by the deployment that is about
    to be torn down, so the bind failure is expected and the port WILL be
    free by restore time. Only RUNNING tolerated containers explain a
    bind failure (a stopped container cannot hold a port); a bind failure
    caused (even partly) by anything else still raises. Never pass this
    on the deploy path — a new deployment must never tolerate a bound
    port.

    Called before every `docker run`; a collision fails the task with a
    clear error instead of a cryptic `docker run` failure.
    """
    try:
        rows = docker.ps(all=True)
    except Exception as exc:
        raise DeployError(
            f"could not list docker containers to verify port "
            f"{host_port} is free: {exc}"
        )
    excluded = set(exclude_names or ())
    tolerated = set(tolerate_bound_by or ())
    # Publishers of this port, with their docker state: only a RUNNING
    # container can actually hold the OS-level bind.
    publishers: dict = {}
    for row in rows or []:
        name = (row.get("Names") or "").lstrip("/")
        if host_port in published_host_ports(row.get("Ports")):
            publishers[name] = (row.get("State") or "").lower()
    foreign = [n for n in publishers if n not in excluded]
    if foreign:
        raise DeployError(
            f"host port {host_port} is already published by another docker "
            f"container; refusing docker run"
        )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # SO_REUSEADDR, like docker's own published-port bind: a
            # stopped container's connections can linger in TIME_WAIT and
            # must not read as "port in use" — only a live listener does.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", host_port))
    except OSError:
        # The target's own (stopped) container never binds: the only
        # legitimate explanation for a live bind is a RUNNING container
        # the caller is about to tear down (rollback same-port case).
        own_only = excluded - tolerated
        bound_by_tolerated = {n for n, s in publishers.items()
                              if n not in own_only and s == "running"}
        if tolerated and bound_by_tolerated and \
                bound_by_tolerated <= tolerated:
            # The port will be free by restore time. Anything else
            # holding it still fails below.
            if log:
                log(f"host port {host_port} is bound only by "
                    f"{sorted(bound_by_tolerated)} (being replaced); "
                    f"treating as free-after-teardown")
            return
        raise DeployError(
            f"host port {host_port} is already in use on this host "
            f"(OS bind test failed); refusing docker run"
        )
    if log:
        log(f"host port {host_port} verified free (OS bind + docker ps)")


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Minimum secret-value length the log scrubbers redact. Shorter values are
# skipped: redacting 1-3 char strings would mangle ordinary log text with
# false positives. Mirrors executor.dispatcher.SecretScrubber semantics.
_SCRUB_MIN_LEN = 4


def _chain_secret_scrubber(prev_scrub, values):
    """Return a scrub function that also redacts the given secret values.

    The dispatcher's SecretScrubber only knows payload-carried secrets and
    the host token; API-fetched project secrets (pulled mid-deploy) must be
    registered too, or a failing `docker run` — DockerError embeds the full
    `-e KEY=value` argv — would land in the task log, the progress error
    and the task.failed event in plaintext.
    """
    vals = sorted(
        {str(v) for v in (values or []) if v and len(str(v)) >= _SCRUB_MIN_LEN},
        key=len,
        reverse=True,
    )
    if not vals:
        return prev_scrub

    def scrub(text):
        if not isinstance(text, str):
            text = str(text)
        for value in vals:
            text = text.replace(value, "***")
        return prev_scrub(text)

    return scrub


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

def _fetch_verified_artifact(ctx, log, work_dir: Path, artifact_id: str,
                            payload: dict, state_dir: Path) -> str:
    """Download + SHA-256-verify + extract one artifact.

    Serialized per artifact_id (spec §42): concurrent deploys of the SAME
    artifact would otherwise interleave writes into artifacts/<id>.bin and
    corrupt each other's download. Different artifacts download fully in
    parallel. Extraction reads the shared dest file, so it stays inside
    the lock; the extract target itself is per-deployment.

    Returns the extraction directory. Raises DeployError before ANY docker
    interaction on checksum failure (the bad bytes are quarantined).
    """
    expected_checksum = payload.get("artifact_checksum")
    if not expected_checksum:
        raise DeployError(
            "refusing to deploy: payload has artifact_id but no "
            "artifact_checksum; unverified artifacts are never executed"
        )
    try:
        validate_artifact_id(artifact_id)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc
    with artifact_download_lock(artifact_id):
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
    return extract_dir


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

    # Active scrubber for this thread. The dispatcher installs its task
    # scrubber thread-locally (WorkerContext.scrub_scope); reading it via
    # effective_scrub() keeps concurrent dispatches on the shared ctx from
    # clobbering each other (spec §42). Duck-typed ctxs in unit tests may
    # lack effective_scrub; fall back to the plain attribute there (those
    # tests are single-threaded by construction).
    _get_effective = getattr(ctx, "effective_scrub", None)

    def _active_scrub():
        return _get_effective() if _get_effective else ctx.scrub

    def log(line: str) -> None:
        clean = _active_scrub()(line)
        ctx.log(task_id, clean)  # ctx.log re-applies the task scrubber; idempotent
        try:
            ctx.log_store.append_deployment(deployment_id, clean)
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
        extract_dir = _fetch_verified_artifact(
            ctx, log, work_dir, artifact_id, payload, state_dir)
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
    # W11: register the API-fetched secret values with the active log
    # scrubber. The dispatcher built its scrubber before this handler ran,
    # so it only covers payload-carried secrets + the host token; these
    # pulled values flow into docker argv (`-e KEY=VALUE`, visible in
    # DockerError messages) and container-log tails from here on and must
    # be redacted everywhere too.
    #
    # Installed thread-locally on a real WorkerContext (extend_scrub) so
    # concurrent dispatches on the shared ctx cannot leak each other's
    # secrets (spec §42); on duck-typed test ctxs the historical
    # ctx.scrub assignment is kept. Deliberately not restored on raise
    # paths: the dispatcher's failure logging must keep redacting these
    # values as well. The dispatcher's scrub_scope() restores the thread's
    # pre-task scrubber when the task ends.
    if fetched_secrets:
        chained = _chain_secret_scrubber(_active_scrub(),
                                         fetched_secrets.values())
        if hasattr(ctx, "extend_scrub"):
            ctx.extend_scrub(chained)
        else:
            ctx.scrub = chained

    # -- 3. host resource check (admission control on RESERVED resources) ----
    # allocated = sum of reservations from running deployments' manifests
    # (see health.collector.allocated_resources); utilized = instantaneous
    # measured usage, which is IRRELEVANT here. A deployment is admitted
    # only when total - allocated covers its request: a host at 2% CPU can
    # still refuse a deployment whose reservations are already full.
    # Note: the previous deployment of this project (if any) is still
    # 'running' at this point — both containers briefly exist during the
    # healthcheck window, so its reservation correctly counts as allocated.
    #
    # TRANSACTIONAL (spec §11): reserve_resources() checks capacity and
    # records this deployment's own reservation atomically under the
    # process-wide resource lock, so two simultaneous deploy() calls
    # cannot both consume the same capacity. The reservation is held until
    # the deployment's state row is persisted (success) or released by the
    # section-8 finally on any failure path.
    resource_reserved = False
    if resources.get("memory") or resources.get("cpu"):
        need_mb = (parse_memory_mb(resources["memory"])
                   if resources.get("memory") else 0.0)
        need_cpu = (float(resources["cpu"])
                    if resources.get("cpu") else 0.0)
        reserve_resources(deployment_id, need_cpu, need_mb, store, log=log)
        resource_reserved = True

    # §4-§5 run under this try/except: they execute after the §3 resource
    # reservation (and the §5 compose port reservation) is taken but before
    # the section-6 try/finally is entered. An exception here — docker
    # build failure, compose validation rejection, compose up failure —
    # must release those reservations, or they leak for the life of the
    # worker process and silently shrink all future admission.
    try:
        docker = ctx.require_docker()

        # -- 4. previous healthy deployment (rollback target) --------------------
        previous = store.latest_for_project(project_id, statuses=("running",))
        if previous:
            log(f"rollback target: previous deployment {previous['deployment_id']} "
                f"(container {previous.get('container_name')})")
        else:
            log("no previous healthy deployment; rollback target: none")

        # -- 5. build ------------------------------------------------------------
        # Process-wide port reservations taken below (compose-declared ports,
        # or the picked / control-plane-requested single-container port) are
        # held until the deployment's state row is persisted; the finally in
        # section 8 releases them on every path.
        reserved_ports: list[int] = []
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
            compose_project_name = compose_project_name_for(project_id,
                                                              safe_project)
            # Migration (§18): stacks created before project_id-suffixed names
            # used the bare "uaht-<project>" name. If the previous RUNNING
            # deployment of THIS project still owns such a legacy stack, tear
            # it down first — the new suffixed project replaces it in place.
            # Without this, the new stack's port check would fail against the
            # project's own old containers.
            legacy_project = f"uaht-{safe_project}"
            if (previous and previous.get("compose_project") == legacy_project
                    and legacy_project != compose_project_name):
                legacy_file = previous.get("compose_file")
                if legacy_file and os.path.isfile(legacy_file):
                    log(f"removing legacy compose project {legacy_project} "
                        f"(pre-isolation naming)")
                    try:
                        docker.compose_down(legacy_file,
                                            project_name=legacy_project)
                    except Exception as exc:
                        log(f"legacy compose down failed (continuing): {exc}")
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
                                      registry_used=(store.used_host_ports()
                                                     - previous_ports)
                                      | reserved_port_snapshot())
            # Reserve the declared ports process-wide: a concurrent
            # single-container deploy must not pick one of them (spec §42).
            # Released in the section-8 finally once this deployment's state
            # row (carrying compose_ports) is persisted.
            reserved_ports = reserve_declared_ports(wanted_ports, deployment_id)
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

    except Exception:
        release_port_reservations(reserved_ports)
        if resource_reserved:
            release_resource_reservation(deployment_id)
        raise

    # -- 6. run new container (compose runtime already running) -------------
    # Sections 6-8 run under the port reservation: it bridges the gap
    # between picking a port and persisting the deployment state row
    # that records it. The finally releases it on every path — on
    # success the state row itself keeps the port out of later picks;
    # on failure the port is genuinely free again.
    new_container_started = False
    host_port = None
    try:
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
                # Also reserve process-wide: a concurrent auto-pick must
                # not take this port (spec §42).
                reserved_ports = reserve_declared_ports([host_port],
                                                       deployment_id)
            else:
                host_port = reserve_free_port(store, log=log,
                                              deployment_id=deployment_id)
                reserved_ports = [host_port]
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
                "artifact_id": artifact_id,  # §13: recreate needs the artifact ref
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
        port_settle_record = None
        rollback_status_value = None
        rollback_error_note = None
        if action == "rollback_to_previous" and previous:
            # Unified rollback (deployments.rollback): verify-target ->
            # restore -> REAL health check -> commit state. The previous
            # deployment's health_status comes from the health check —
            # never assumed healthy just because its container restarted.
            try:
                outcome = rollback_mod.perform_rollback(
                    ctx, docker, log=log, target=previous,
                    healthcheck_timeout=int(
                        payload.get("healthcheck_timeout", 120)))
            except rollback_mod.RollbackError as exc:
                raise DeployError(
                    f"healthcheck failed for {project_name} {version} and "
                    f"rollback to {previous.get('deployment_id')} failed: "
                    f"{exc}"
                ) from exc
            rolled_back_to = outcome["rolled_back_to"]
            target_healthy = outcome["target_health_status"] != "unhealthy"
            # Port-reservation reconciliation with the control-plane
            # registry (spec §3): after rollback, Docker/OS state ==
            # deployment state == the port_allocations registry. A settle
            # failure never fails the rollback — it is logged and
            # recorded, not silently claimed. A skipped settle (no
            # control-plane session — tests/doubles only; production
            # always has one) is not a failure.
            settle = rollback_mod.settle_rollback_ports(
                ctx, log, deployment_id, rolled_back_to)
            port_settle_record = {"settled": settle["settled"],
                                  "reason": settle.get("reason")}
            settle_failed = (not settle["settled"]
                             and settle.get("reason")
                             != "no control-plane session")
            if not settle["settled"]:
                log(f"rollback port registry settle failed "
                    f"({settle.get('reason')}); recorded, not fatal")
            # Spec §6: a rollback is only "rolled_back" when the target
            # was restored AND verified healthy AND the port registry
            # settled. An unhealthy target or a failed settle persists
            # rollback_failed (partially_reconciled when only the settle
            # failed) — never a false success.
            if target_healthy and not settle_failed:
                rollback_status_value = "succeeded"
            elif target_healthy:
                rollback_status_value = "partially_reconciled"
                rollback_error_note = (
                    f"target restored and healthy but port-registry "
                    f"settle failed: {settle.get('reason')}")
            else:
                rollback_status_value = "failed"
                rollback_error_note = (
                    f"target {rolled_back_to} restored but health check "
                    f"failed (health_status="
                    f"{outcome['target_health_status']})")
        final_status = ("rolled_back"
                        if rolled_back_to and rollback_status_value == "succeeded"
                        else "failed" if not rolled_back_to
                        else "rollback_failed")
        store.save({
            "deployment_id": deployment_id,
            "task_id": task_id,
            "project_id": project_id,
            "project_name": project_name,
            "version": version,
            "artifact_id": artifact_id,  # §13: recreate needs the artifact ref
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
            "healthcheck": manifest_data.get("healthcheck"),
            "domains": manifest_data.get("domains"),
            "manifest": manifest_data,
            "healthcheck_path": healthcheck_path,
            "env": manifest_env,
            "status": final_status,
            "health_status": "unhealthy",
            "rollback_of": rolled_back_to,
            # §6: the rollback operation's durable outcome — never a false
            # "rolled_back" when the target is unhealthy or the registry
            # did not settle. Enough state (target id, settle record,
            # error note) for reconciliation/retry.
            "rollback_status": rollback_status_value,
            "rollback_error": rollback_error_note,
            # §3: port-registry settle outcome (None when no rollback ran);
            # a failed settle is recorded here, never silently claimed.
            "port_settle": port_settle_record,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        err = (f"healthcheck failed for {project_name} {version} "
               f"(GET :{host_port}{healthcheck_path} never returned 200); "
               + (f"rolled back to deployment {rolled_back_to}"
                  if rolled_back_to and rollback_status_value == "succeeded"
                  else f"rollback to deployment {rolled_back_to} restored "
                       f"the target but it is UNHEALTHY — rollback FAILED"
                  if rolled_back_to and rollback_status_value == "failed"
                  else f"rolled back to deployment {rolled_back_to} but "
                       f"port registry settle failed "
                       f"({port_settle_record['reason']}) — rollback "
                       f"PARTIALLY RECONCILED"
                  if rolled_back_to
                  else "no previous healthy deployment to roll back to"))
        raise DeployError(err)
    finally:
        release_port_reservations(reserved_ports)
        if resource_reserved:
            # On success the persisted state row (carrying the resources)
            # now accounts for this deployment; on failure nothing was
            # persisted. Either way the in-flight reservation is done.
            release_resource_reservation(deployment_id)


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
                              registry_used: Optional[set] = None,
                              exclude_names: Optional[set] = None,
                              tolerate_bound_by: Optional[set] = None
                              ) -> None:
    """Verify every host port a compose file wants to publish is free —
    the same OS-bind + docker-ps check `docker run` gets.

    Ports already held by THIS compose project's current stack are skipped:
    a redeploy replaces its own stack in place (`compose up` on the same
    project name), so its own ports are not a collision.

    ``registry_used`` (optional): host ports the control-plane port
    registry / deployment store has reserved for other deployments.
    Catches the window where a port is allocated but not yet bound.

    ``exclude_names`` / ``tolerate_bound_by`` (optional): passed through
    to verify_host_port_free — used by rollback verification for the
    same-port case (the deployment being replaced legitimately holds the
    target's ports until its teardown).
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
        verify_host_port_free(docker, port, log=log,
                              exclude_names=exclude_names,
                              tolerate_bound_by=tolerate_bound_by)
    if log and wanted_ports:
        log(f"compose ports verified: wanted={sorted(set(wanted_ports))} "
            f"already-held-by-own-stack={sorted(own)}")
