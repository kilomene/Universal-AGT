"""Host resource metrics from /proc and the OS (no external tools)."""
from __future__ import annotations

import os
import time
from typing import Optional


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


def total_cpu() -> float:
    return float(os.cpu_count() or 1)


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
            if state.get("status") in ("running", "starting", "healthcheck"):
                running_apps.append({
                    "deployment_id": state.get("deployment_id"),
                    "project": state.get("project_name"),
                    "status": state.get("status"),
                })

    payload = {
        "cpu_pct": cpu_percent(),
        "ram_pct": memory_percent(),
        "disk_pct": disk_percent(disk_path),
        "docker_status": docker_status,
        "running_apps": running_apps,
        "total_cpu": total_cpu(),
        "total_ram_mb": total_ram_mb(),
        "total_disk_gb": total_disk_gb(disk_path),
    }
    if config is not None and getattr(config, "worker_version", None):
        payload["worker_version"] = config.worker_version
    return payload
