"""Provider-neutral public-ingress abstraction (Phase 7).

The honest architectural problem (spec section 28): the persistent host is
OUTBOUND-ONLY. It dials out to the control plane and never listens for
inbound connections. DNS CNAME records alone therefore CANNOT make an
unreachable private host publicly reachable — there must be SOME ingress
mechanism that the host dials out to. This package does not pretend
otherwise: every provider defined here dials OUT. Nothing in this package
opens a listening socket or accepts inbound connections.

CONTROL AUTHORITY (W9): for token-based (remotely-managed) tunnels — the
only kind this system runs — the route table that actually moves traffic
lives in CLOUDFLARE, not on the host. ``cloudflared tunnel --token <TOKEN>
run`` IGNORES local config.yml ingress rules; it pulls its configuration
from the edge and picks up a new remote version within seconds, no restart
needed. The authoritative route table is written by the CONTROL PLANE via
the Cloudflare tunnel-config API (PUT
/accounts/{id}/cfd_tunnel/{id}/configurations); the worker's local
config.yml is a NON-AUTHORITATIVE mirror — an operator checklist and
debugging artifact, never the routing authority. Nothing in this package
implies otherwise.

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
#  config.yml. Same shape as the control plane's isValidHostname, including
#  the W10 hardening (no wildcards, no localhost/internal/special-use
#  names, no IP literals).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$",
    re.IGNORECASE,
)

#: Suffixes that must never be routed, mirroring the control plane's
#: BLOCKED_SUFFIXES (RFC 6761/6762 special-use + internal-only convention).
_BLOCKED_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "invalid",
    "example",
    "test",
    "onion",
)


def _is_blocked_hostname(hostname: str) -> bool:
    lower = hostname.lower()
    return any(lower == s or lower.endswith("." + s)
               for s in _BLOCKED_SUFFIXES)


class IngressError(Exception):
    """Raised when ingress cannot be set up or a provider fails."""


def is_valid_hostname(hostname) -> bool:
    if not isinstance(hostname, str):
        return False
    if "*" in hostname:
        return False
    if not _HOSTNAME_RE.match(hostname):
        return False
    if _is_blocked_hostname(hostname):
        return False
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", hostname):
        return False
    return True


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
    # §27: ingress routes terminate ONLY at 127.0.0.1 — the literal
    # loopback address, never a name that could resolve elsewhere.
    # "localhost" is deliberately NOT accepted: it is a localhost variant
    # that can resolve to the IPv6 loopback (::1) or be shadowed by a
    # resolver, and the spec requires the exact 127.0.0.1 origin.
    if target_host != "127.0.0.1":
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
        "# NON-AUTHORITATIVE LOCAL MIRROR — rewritten on every ingress sync;",
        "# do not edit by hand. For token-based (remotely-managed) tunnels,",
        "# cloudflared IGNORES this file's ingress rules: the authoritative",
        "# route table is the tunnel's REMOTE configuration, written by the",
        "# control plane via the Cloudflare API. This file is an operator",
        "# checklist / debugging artifact only. See docs/cloudflare.md.",
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
