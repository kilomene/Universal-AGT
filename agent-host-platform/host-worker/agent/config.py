"""Worker configuration loading.

Loads, in increasing order of precedence:
  1. /opt/agent-host/config/worker.env (or the path in $WORKER_CONFIG,
     or the --config CLI flag)
  2. WORKER_* environment variables
  3. explicit keyword arguments

worker.env is a KEY=VALUE file ("export KEY=VALUE" lines and comments
are tolerated). No secrets are ever written to stdout or logs by this
module.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = "/opt/agent-host/config/worker.env"
ENV_PREFIX = "WORKER_"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _parse_env_file(path: str) -> dict:
    values: dict = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # tolerate quoted values: FOO="a b" / FOO='a b'
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    return values


def _as_int(value, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field_name} must be an integer, got {value!r}")


@dataclass
class WorkerConfig:
    control_plane_url: str = ""
    host_name: str = ""
    host_token: str = ""
    host_id: str = ""
    poll_wait: int = 25
    heartbeat_interval: int = 30
    work_dir: str = "/opt/agent-host"
    apps_dir: str = "/srv/agent-apps"
    worker_version: str = "0.1.0"
    capabilities: list = field(default_factory=list)
    config_path: str = ""
    # Crash-loop detection (see deployments.crashloop).
    crash_loop_threshold: int = 5    # restarts inside the window -> crash loop
    crash_loop_window_s: int = 300   # observation window, seconds

    @classmethod
    def load(cls, config_path: str | None = None, **overrides) -> "WorkerConfig":
        """Load configuration from file, environment, and overrides."""
        path = (
            config_path
            or os.environ.get("WORKER_CONFIG")
            or DEFAULT_CONFIG_PATH
        )
        values: dict = {}
        if os.path.isfile(path):
            for file_key, file_value in _parse_env_file(path).items():
                # worker.env uses WORKER_-prefixed keys; normalize to the
                # unprefixed field names used below.
                if file_key.startswith(ENV_PREFIX):
                    norm_key = file_key[len(ENV_PREFIX):].lower()
                else:
                    norm_key = file_key.lower()
                values[norm_key] = file_value

        env_map = {
            "control_plane_url": ENV_PREFIX + "CONTROL_PLANE_URL",
            "host_name": ENV_PREFIX + "HOST_NAME",
            "host_token": ENV_PREFIX + "HOST_TOKEN",
            "host_id": ENV_PREFIX + "HOST_ID",
            "poll_wait": ENV_PREFIX + "POLL_WAIT",
            "heartbeat_interval": ENV_PREFIX + "HEARTBEAT_INTERVAL",
            "work_dir": ENV_PREFIX + "WORK_DIR",
            "apps_dir": ENV_PREFIX + "APPS_DIR",
            "worker_version": ENV_PREFIX + "WORKER_VERSION",
            "capabilities": ENV_PREFIX + "CAPABILITIES",  # comma-separated
            "crash_loop_threshold": ENV_PREFIX + "CRASH_LOOP_THRESHOLD",
            "crash_loop_window_s": ENV_PREFIX + "CRASH_LOOP_WINDOW_S",
        }
        for field_name, env_name in env_map.items():
            if env_name in os.environ:
                values[field_name] = os.environ[env_name]
        values.update({k: v for k, v in overrides.items() if v is not None})

        cfg = cls(
            control_plane_url=str(values.get("control_plane_url", "")).rstrip("/"),
            host_name=str(values.get("host_name", "")),
            host_token=str(values.get("host_token", "")),
            host_id=str(values.get("host_id", "")),
            poll_wait=_as_int(values.get("poll_wait", 25), "poll_wait"),
            heartbeat_interval=_as_int(
                values.get("heartbeat_interval", 30), "heartbeat_interval"
            ),
            work_dir=str(values.get("work_dir", "/opt/agent-host")),
            apps_dir=str(values.get("apps_dir", "/srv/agent-apps")),
            worker_version=str(values.get("worker_version", "0.1.0")),
            config_path=path,
            crash_loop_threshold=_as_int(
                values.get("crash_loop_threshold", 5), "crash_loop_threshold"
            ),
            crash_loop_window_s=_as_int(
                values.get("crash_loop_window_s", 300), "crash_loop_window_s"
            ),
        )
        caps = values.get("capabilities", "")
        if isinstance(caps, str) and caps.strip():
            cfg.capabilities = [c.strip() for c in caps.split(",") if c.strip()]
        elif isinstance(caps, (list, tuple)):
            cfg.capabilities = list(caps)

        if not 1 <= cfg.poll_wait <= 30:
            raise ConfigError(
                f"poll_wait must be 1..30 (server max for ?wait=), got {cfg.poll_wait}"
            )
        if cfg.heartbeat_interval < 5:
            raise ConfigError(
                f"heartbeat_interval must be >= 5, got {cfg.heartbeat_interval}"
            )
        return cfg

    def validate(self) -> None:
        """Raise ConfigError if anything required for the main loop is missing."""
        missing = []
        if not self.control_plane_url:
            missing.append("CONTROL_PLANE_URL")
        if not self.host_name:
            missing.append("HOST_NAME")
        if not self.host_token:
            missing.append("HOST_TOKEN")
        if not self.host_id:
            missing.append("HOST_ID")
        if missing:
            raise ConfigError(
                "missing required worker configuration: "
                + ", ".join(missing)
                + f" (config file: {self.config_path or DEFAULT_CONFIG_PATH}). "
                "Run the host installer or provision the host first."
            )
        if not (
            self.control_plane_url.startswith("https://")
            or self.control_plane_url.startswith("http://localhost")
            or self.control_plane_url.startswith("http://127.0.0.1")
        ):
            raise ConfigError(
                "CONTROL_PLANE_URL must be https:// in production "
                f"(got {self.control_plane_url!r})"
            )

    def redacted(self) -> dict:
        """Config dict safe to log (secret values masked)."""
        return {
            "control_plane_url": self.control_plane_url,
            "host_name": self.host_name,
            "host_token": "***" if self.host_token else "",
            "host_id": self.host_id,
            "poll_wait": self.poll_wait,
            "heartbeat_interval": self.heartbeat_interval,
            "work_dir": self.work_dir,
            "apps_dir": self.apps_dir,
            "worker_version": self.worker_version,
            "capabilities": self.capabilities,
            "config_path": self.config_path,
            "crash_loop_threshold": self.crash_loop_threshold,
            "crash_loop_window_s": self.crash_loop_window_s,
        }
