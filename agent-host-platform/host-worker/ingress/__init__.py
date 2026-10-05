"""Provider-neutral public-ingress abstraction (Phase 7).

The honest architectural problem (spec section 28): the persistent host is
OUTBOUND-ONLY. It dials out to the control plane and never listens for
inbound connections. DNS CNAME records alone therefore CANNOT make an
unreachable private host publicly reachable — there must be SOME ingress
mechanism that the host dials out to. This package does not pretend
otherwise: every provider defined here dials OUT. Nothing in this package
opens a listening socket or accepts inbound connections.

Interface every ingress provider implements::

    IngressProvider: name, setup(config), add_route(...), remove_route(...),
                     sync_routes(...), status(), shutdown()

Providers are optional and disabled by default. The only bundled provider is
``cloudflare-tunnel`` (see ingress/cloudflared.py): a supervised
``cloudflared`` process holding an outbound tunnel to Cloudflare's edge,
which carries inbound HTTP back to the host's local container ports.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from agent.config import ConfigError

# Registry of bundled providers: name -> "module.Class" (lazy import so a
# broken/optional provider never breaks worker startup).
PROVIDERS: dict[str, str] = {
    "cloudflare-tunnel": "ingress.cloudflared.CloudflaredTunnelProvider",
}

#: Deployments in these states never get ingress routes.
TERMINAL_DEPLOYMENT_STATUSES = frozenset(
    {"removed", "rolled_back", "failed"}
)

#: Hostnames are validated before they ever reach a provider config file —
#  a crafted hostname must not be able to inject YAML into cloudflared's
#  config.yml. Same shape as the control plane's isValidHostname.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$",
    re.IGNORECASE,
)


class IngressError(Exception):
    """Raised when ingress cannot be set up or a provider fails."""


def is_valid_hostname(hostname) -> bool:
    return isinstance(hostname, str) and bool(_HOSTNAME_RE.match(hostname))


@dataclass
class IngressConfig:
    """Ingress settings, derived from the worker config (agent/config.py).

    Disabled by default. Enable explicitly per host::

        WORKER_INGRESS_ENABLED=true
        WORKER_INGRESS_PROVIDER=cloudflare-tunnel
        WORKER_TUNNEL_TOKEN=<redacted>   # from the Cloudflare dashboard; never in the repo
    """

    enabled: bool = False
    provider: str = ""
    tunnel_token: str = ""
    work_dir: str = "/opt/agent-host"

    def validate(self) -> None:
        """Raise ConfigError when enabled but unusable. Disabled is a no-op."""
        if not self.enabled:
            return
        if self.provider not in PROVIDERS:
            raise ConfigError(
                "ingress enabled but WORKER_INGRESS_PROVIDER is not a known "
                f"provider: {self.provider!r}; known providers: "
                f"{sorted(PROVIDERS)}"
            )
        if self.provider == "cloudflare-tunnel" and not self.tunnel_token:
            raise ConfigError(
                "ingress provider 'cloudflare-tunnel' is enabled but no "
                "tunnel token is configured: set WORKER_TUNNEL_TOKEN in the "
                "host worker.env (the installer accepts UAHT_TUNNEL_TOKEN; "
                "it is the token shown when you create the tunnel in the "
                "Cloudflare dashboard). Refusing to start "
                "ingress without it."
            )

    def redacted(self) -> dict:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "tunnel_token": "***" if self.tunnel_token else "",
            "work_dir": self.work_dir,
        }


def build_ingress_config(worker_config) -> IngressConfig:
    """Derive IngressConfig from a WorkerConfig (duck-typed)."""
    return IngressConfig(
        enabled=bool(getattr(worker_config, "ingress_enabled", False)),
        provider=str(getattr(worker_config, "ingress_provider", "") or ""),
        tunnel_token=str(getattr(worker_config, "tunnel_token", "") or ""),
        work_dir=str(getattr(worker_config, "work_dir", "/opt/agent-host")
                     or "/opt/agent-host"),
    )


def get_provider(name: str) -> "IngressProvider":
    """Instantiate a bundled provider by name; IngressError when unknown."""
    target = PROVIDERS.get(name)
    if not target:
        raise IngressError(
            f"unknown ingress provider {name!r}; "
            f"known providers: {sorted(PROVIDERS)}"
        )
    module_name, _, class_name = target.rpartition(".")
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)()


class IngressProvider(ABC):
    """Provider-neutral ingress interface.

    Implementations MUST be outbound-only: they may open connections from
    the host outward (tunnel to an edge, registration with a relay) and
    must never listen on a local port for traffic initiated from outside.
    """

    #: Route records handled by every provider.
    #: {"hostname": str, "target_host": str, "target_port": int}

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name, e.g. 'cloudflare-tunnel'."""

    @abstractmethod
    def setup(self, config: IngressConfig) -> "IngressProvider":
        """Validate config, provision anything needed (binary download,
        directories), and prepare to run. Returns self. Raises
        IngressError/ConfigError when setup is impossible."""

    @abstractmethod
    def start(self) -> None:
        """Start serving (e.g. spawn the supervised tunnel process)."""

    @abstractmethod
    def shutdown(self) -> None:
        """Stop serving; terminate supervised processes."""

    @abstractmethod
    def status(self) -> dict:
        """JSON-able status: enabled, running, routes, errors. Never
        includes secrets."""

    # -- route table -----------------------------------------------------
    # The default implementations keep an in-memory route table and call
    # _apply_routes() whenever it changes; providers override _apply_routes.

    def __init__(self) -> None:
        self._routes: dict[str, dict] = {}
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def routes(self) -> list[dict]:
        """Current route table snapshot, sorted by hostname."""
        return [self._routes[h] for h in sorted(self._routes)]

    def add_route(self, hostname: str, target_host: str,
                  target_port: int) -> None:
        _check_route(hostname, target_host, target_port)
        self._routes[hostname] = {
            "hostname": hostname,
            "target_host": target_host,
            "target_port": int(target_port),
        }
        self._apply_routes()

    def remove_route(self, hostname: str) -> None:
        if hostname in self._routes:
            del self._routes[hostname]
            self._apply_routes()

    def sync_routes(self, routes: list[dict]) -> bool:
        """Reconcile the full route table; returns True when it changed."""
        wanted = {}
        for route in routes:
            _check_route(route["hostname"], route["target_host"],
                         route["target_port"])
            wanted[route["hostname"]] = {
                "hostname": route["hostname"],
                "target_host": route["target_host"],
                "target_port": int(route["target_port"]),
            }
        if wanted == self._routes:
            return False
        self._routes = wanted
        self._apply_routes()
        return True

    def _apply_routes(self) -> None:
        """Push the route table to the underlying mechanism."""
        raise NotImplementedError


def _check_route(hostname, target_host, target_port) -> None:
    if not is_valid_hostname(hostname):
        raise IngressError(f"refusing route for invalid hostname: {hostname!r}")
    if target_host not in ("127.0.0.1", "localhost"):
        # Ingress routes always terminate on this host's loopback: the
        # provider must never be steered at an arbitrary network target.
        raise IngressError(
            f"refusing route for {hostname!r}: target_host must be "
            f"127.0.0.1 (got {target_host!r})"
        )
    port = int(target_port)
    if not 1 <= port <= 65535:
        raise IngressError(f"refusing route for {hostname!r}: bad port {port}")


def build_routes(deployment_states, domain_entries
                 ) -> tuple[list[dict], list[dict]]:
    """Build tunnel routes from deployment states + domain entries.

    deployment_states: dicts with deployment_id, host_port, status.
    domain_entries: dicts with deployment_id, hostname, ingress
        ('tunnel' | 'direct'; only 'tunnel' entries become tunnel routes).

    Returns (routes, skipped); each route is
    {"hostname", "target_host": "127.0.0.1", "target_port"} and each skipped
    entry carries a human reason. Pure function — easy to test.
    """
    by_id = {s.get("deployment_id"): s for s in (deployment_states or [])}
    routes: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for entry in domain_entries or []:
        hostname = entry.get("hostname")
        deployment_id = entry.get("deployment_id")
        if not is_valid_hostname(hostname):
            skipped.append({"hostname": hostname, "reason": "invalid hostname"})
            continue
        if entry.get("ingress", "direct") != "tunnel":
            skipped.append({"hostname": hostname,
                            "reason": "ingress mode is not 'tunnel'"})
            continue
        state = by_id.get(deployment_id)
        if state is None:
            skipped.append({"hostname": hostname,
                            "reason": f"no local deployment {deployment_id}"})
            continue
        if state.get("status") in TERMINAL_DEPLOYMENT_STATUSES:
            skipped.append({"hostname": hostname,
                            "reason": f"deployment status {state.get('status')}"})
            continue
        host_port = state.get("host_port")
        if not host_port:
            skipped.append({"hostname": hostname,
                            "reason": "deployment exposes no host port"})
            continue
        if hostname in seen:
            skipped.append({"hostname": hostname,
                            "reason": "duplicate hostname; first wins"})
            continue
        seen.add(hostname)
        routes.append({"hostname": hostname, "target_host": "127.0.0.1",
                       "target_port": int(host_port)})
    routes.sort(key=lambda r: r["hostname"])
    return routes, skipped


def render_cloudflared_config(routes: list[dict]) -> str:
    """Render a cloudflared config.yml ingress section from routes.

    Every route becomes ``hostname -> http://127.0.0.1:<port>`` with a
    catch-all 404. Hostnames are re-validated here so a bad entry can
    never inject YAML.
    """
    lines = [
        "# Managed by Universal-AGT ingress (cloudflare-tunnel provider).",
        "# Canonical route table — rewritten on every ingress sync; do not edit by hand.",
        "# See docs/cloudflare.md. NOTE: for dashboard-created (token) tunnels,",
        "# Cloudflare reads public-hostname routes from the tunnel's dashboard",
        "# configuration, not from this file; mirror the hostnames below in",
        "# Zero Trust -> Networks -> Tunnels -> Public hostnames.",
        "ingress:",
    ]
    for route in sorted(routes, key=lambda r: r["hostname"]):
        hostname = route["hostname"]
        if not is_valid_hostname(hostname):
            raise IngressError(
                f"refusing to render invalid hostname {hostname!r}")
        port = int(route["target_port"])
        lines.append(f"  - hostname: {hostname}")
        lines.append(f"    service: http://127.0.0.1:{port}")
    lines.append("  # catch-all")
    lines.append("  - service: http_status:404")
    return "\n".join(lines) + "\n"
