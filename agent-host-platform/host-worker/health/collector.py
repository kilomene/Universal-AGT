"""Host resource metrics from /proc and the OS (no external tools).

Two distinct resource views are reported:

  * UTILIZATION (instantaneous, measured): cpu_pct / ram_pct / disk_pct —
    what the host is consuming right now.
  * RESERVATION (accounting, from deployment manifests): allocated_cpu /
    allocated_ram_mb — the sum of cpu/memory reservations of running
    deployments; available_* = total - allocated. Admission control
    (deployments.pipeline) decides on reservations, never on instantaneous
    utilization: a host at 2% CPU still refuses a deployment when its
    reservations are full.
"""
from __future__ import annotations

import os
import time

from deployments.manifest import normalize_resources

# Deployment statuses that hold a resource reservation on this host.
RESERVING_STATUSES = ("running", "starting", "healthcheck")


def _read_cpu_times() -> tuple[int, int]:
    """Return (idle, total) jiffies from the aggregate cpu line of /proc/stat."""
    with open("/proc/stat", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:]]
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
                return idle, sum(parts)
    raise OSError("/proc/stat has no aggregate cpu line")


def cpu_percent(sample_secs: float = 0.2) -> float:
    idle0, total0 = _read_cpu_times()
    time.sleep(sample_secs)
    idle1, total1 = _read_cpu_times()
    idle_d, total_d = idle1 - idle0, total1 - total0
    if total_d <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - idle_d / total_d) * 100.0)), 1)


def memory_percent() -> float:
    total = avail = None
    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1])
            if total is not None and avail is not None:
                break
    if not total:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - avail / total) * 100.0)), 1)


def disk_percent(path: str = "/") -> float:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - free / total) * 100.0)), 1)


def total_ram_mb() -> float:
    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    return 0.0


def total_disk_gb(path: str = "/") -> float:
    st = os.statvfs(path)
    return round(st.f_blocks * st.f_frsize / (1024.0 ** 3), 1)


def disk_free_gb(path: str = "/") -> float:
    st = os.statvfs(path)
    return round(st.f_bavail * st.f_frsize / (1024.0 ** 3), 1)


def total_cpu() -> float:
    return float(os.cpu_count() or 1)


def allocated_resources(deployment_store=None) -> dict:
    """Sum of resource RESERVATIONS from deployment manifests.

    allocated != utilized: this is what running deployments reserved in
    their manifests (persisted on each deployment's state by the deploy
    pipeline), not what they are currently consuming. Deployments with no
    reservation — or with malformed values — contribute 0. A corrupt or
    unreadable store degrades to zero reservations, never to a crash.

    Returns {"cpu": float, "ram_mb": float}.
    """
    cpu = 0.0
    ram_mb = 0.0
    if deployment_store is None:
        return {"cpu": cpu, "ram_mb": ram_mb}
    try:
        states = deployment_store.list_all()
    except Exception:
        return {"cpu": cpu, "ram_mb": ram_mb}
    for state in states or []:
        if state.get("status") not in RESERVING_STATUSES:
            continue
        # Part 1B: the single authoritative resource parser — identical rule
        # to the control-plane scheduler's normalizeResources.
        normalized = normalize_resources(state.get("resources") or {})
        if normalized["cpu"] is not None:
            cpu += normalized["cpu"]
        if normalized["ram_mb"] is not None:
            ram_mb += normalized["ram_mb"]
    return {"cpu": round(cpu, 3), "ram_mb": round(ram_mb, 1)}


def collect_metrics(docker_client=None, deployment_store=None,
                    config=None, disk_path: str = "/") -> dict:
    """Build the heartbeat payload body (PROTOCOL §3.3).

    docker_status: "ok" | "missing" | "error: ..."
    running_apps: [{deployment_id, project, status}]
    """
    if docker_client is not None:
        try:
            # Short timeout: a wedged docker daemon must never stall heartbeats.
            docker_client.version(timeout=5)
            docker_status = "ok"
        except Exception as exc:  # DockerMissing, DockerError, OSError, TimeoutError
            docker_status = f"error: {type(exc).__name__}: {str(exc)[:200]}"
            if type(exc).__name__ == "DockerMissing":
                docker_status = "missing"
    else:
        docker_status = "missing"

    running_apps = []
    if deployment_store is not None:
        for state in deployment_store.list_all():
            if state.get("status") in RESERVING_STATUSES:
                running_apps.append({
                    "deployment_id": state.get("deployment_id"),
                    "project": state.get("project_name"),
                    "status": state.get("status"),
                })

    # Reservation accounting (manifest sums) — computed once, reused by
    # the payload and kept separate from instantaneous utilization.
    allocated = allocated_resources(deployment_store)
    total_cpus = total_cpu()
    total_ram = total_ram_mb()

    payload = {
        # Instantaneous UTILIZATION (measured right now) ...
        "cpu_pct": cpu_percent(),
        "ram_pct": memory_percent(),
        "disk_pct": disk_percent(disk_path),
        # ... kept SEPARATE from RESERVATION accounting (manifest sums):
        "allocated_cpu": allocated["cpu"],
        "allocated_ram_mb": allocated["ram_mb"],
        # Manifests carry no disk reservations, so disk allocated is 0 and
        # available is simply free space; reported explicitly so consumers
        # never confuse it with disk_pct (utilization).
        "allocated_disk_gb": 0.0,
        "available_cpu": max(0.0, round(total_cpus - allocated["cpu"], 3)),
        "available_ram_mb": max(0.0, round(total_ram - allocated["ram_mb"], 1)),
        "available_disk_gb": disk_free_gb(disk_path),
        "docker_status": docker_status,
        "running_apps": running_apps,
        "total_cpu": total_cpus,
        "total_ram_mb": total_ram,
        "total_disk_gb": total_disk_gb(disk_path),
    }
    if config is not None and getattr(config, "worker_version", None):
        payload["worker_version"] = config.worker_version
    return payload
