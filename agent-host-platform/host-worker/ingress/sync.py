"""Ingress route mirror sync (Phase 7).

The single code path that turns "current deployments' domains" into the
worker's LOCAL route mirror. Used two ways:

1. the ``ingress-sync`` task type (executor/handlers.handle_ingress_sync) —
   for manual or remote (control-plane-triggered) syncs;
2. in-process, via maybe_sync_on_change(), after a successful deploy or
   remove that touches domains.

The worker learns the current domains from the control plane
(``GET /v1/worker/domains``, host token) and resolves each deployment's
host port from its own local deployment store. Only entries with
``ingress: 'tunnel'`` become mirror routes; 'direct' entries are the
operator's own ingress and are left alone.

CONTROL AUTHORITY: for token-based tunnels the routing authority is the
REMOTE tunnel configuration, which the control plane writes via the
Cloudflare API. This sync only refreshes the local config.yml mirror
(non-authoritative, diagnostic) — it never routes traffic and never
restarts the tunnel.
"""
from __future__ import annotations

import logging

from ingress import build_routes

LOG = logging.getLogger("agent-host-worker.ingress")


def sync_ingress(ctx) -> dict:
    """Refresh the provider's local route mirror from current domains.

    For token-based tunnels this only rewrites the non-authoritative
    config.yml mirror — routing authority is the remote tunnel
    configuration (control plane writes it via the Cloudflare API), and
    the tunnel is never restarted by a sync.

    Returns a JSON-able result dict (also used as the ingress-sync task
    result). Never raises for a disabled provider; raises for a provider
    that is enabled but failing, so the task is reported failed honestly.
    """
    provider = getattr(ctx, "ingress", None)
    if provider is None or not provider.enabled:
        return {"status": "disabled",
                "reason": "ingress is not enabled on this host",
                "routes": []}

    body = ctx.api.list_host_domains()
    domain_entries = body.get("domains") or []
    states = ctx.deployment_store.list_all() or []
    routes, skipped = build_routes(states, domain_entries)
    changed = provider.sync_routes(routes)
    result = {
        "status": "ok",
        "provider": provider.name,
        "changed": changed,
        "routes": routes,
        "skipped": skipped,
        "route_count": len(routes),
    }
    LOG.info("ingress sync: %d routes (%d skipped), changed=%s",
             len(routes), len(skipped), changed)
    return result


def maybe_sync_on_change(ctx, deployment_id: str) -> dict | None:
    """Best-effort in-process sync after a successful deploy/remove.

    Returns the sync result, or None when ingress is disabled. Never
    raises: a sync failure must not fail the deploy/remove task that
    triggered it — it is logged and the next ingress-sync heals it.
    """
    provider = getattr(ctx, "ingress", None)
    if provider is None or not provider.enabled:
        return None
    try:
        result = sync_ingress(ctx)
        LOG.info("ingress sync after change on %s: %s (%d routes)",
                 deployment_id, result.get("status"),
                 len(result.get("routes", [])))
        return result
    except Exception as exc:
        LOG.warning("ingress sync after change on %s failed: %s",
                    deployment_id, exc)
        return {"status": "error", "error": str(exc)[:500], "routes": []}
