"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.getCloudflareConfig = getCloudflareConfig;
exports.resolveIngressMode = resolveIngressMode;
exports.requireTunnelHostname = requireTunnelHostname;
exports.cnameTargetFor = cnameTargetFor;
exports.isValidHostname = isValidHostname;
exports.findDnsRecord = findDnsRecord;
exports.ensureCnameRecord = ensureCnameRecord;
exports.deleteDnsRecord = deleteDnsRecord;
const errors_1 = require("./errors");
const log_1 = require("./log");
// Optional Cloudflare DNS automation (Phase 5, extended Phase 7).
//
// Architectural rule: this system never stores host IP addresses, so the
// control plane CANNOT create A-records pointing at a host. The supported
// model is CNAME -> an ingress hostname, where the ingress is something the
// operator runs in front of the host. Two modes (Phase 7):
//
//   direct (default) — CNAME hostname -> PUBLIC_INGRESS_HOSTNAME. The
//     operator runs their own ingress (reverse proxy / LB / tunnel they
//     manage); reaching the host is their responsibility.
//   tunnel — CNAME hostname -> TUNNEL_INGRESS_HOSTNAME
//     (e.g. <tunnel-id>.cfargotunnel.com). The host runs a worker-managed
//     cloudflared tunnel (outbound-only) and the worker syncs routes for
//     tunnel-mode domains via the `ingress-sync` task type.
//
// Honesty note (spec section 28): DNS alone never makes an outbound-only
// host reachable. Without (b) direct or (c) tunnel ingress actually
// running, domains resolve but traffic cannot reach the host.
//
// DNS is only touched when CLOUDFLARE_API_TOKEN + CLOUDFLARE_ZONE_ID and at
// least one ingress hostname are set; otherwise domain requests are recorded
// as metadata (status 'dns_pending') and an event notes DNS was skipped.
//
// Credentials are scoped: use a Cloudflare API token limited to
// "Zone / DNS / Edit" on exactly the zone(s) you serve.
const API_BASE = 'https://api.cloudflare.com/client/v4';
function getCloudflareConfig() {
    const token = process.env.CLOUDFLARE_API_TOKEN;
    const zoneId = process.env.CLOUDFLARE_ZONE_ID;
    const ingressHostname = process.env.PUBLIC_INGRESS_HOSTNAME ?? '';
    const tunnelHostname = process.env.TUNNEL_INGRESS_HOSTNAME ?? null;
    if (!token || !zoneId || (!ingressHostname && !tunnelHostname))
        return null;
    return { token, zoneId, ingressHostname, tunnelHostname };
}
/**
 * Resolve the ingress mode for a domain request. An explicit 'tunnel' /
 * 'direct' wins; otherwise tunnel mode is the default when
 * TUNNEL_INGRESS_HOSTNAME is configured, else direct. Throws 400 on a
 * garbage value.
 */
function resolveIngressMode(requested) {
    if (requested === undefined || requested === null || requested === '') {
        return process.env.TUNNEL_INGRESS_HOSTNAME ? 'tunnel' : 'direct';
    }
    if (requested === 'tunnel' || requested === 'direct')
        return requested;
    throw new errors_1.HttpError(400, 'bad_request', `ingress must be 'tunnel' or 'direct'`);
}
/**
 * The tunnel CNAME target, read straight from the environment (tunnel
 * routing does not need the DNS API token — only the hostname).
 * 422 when tunnel mode isn't configured.
 */
function requireTunnelHostname() {
    const host = process.env.TUNNEL_INGRESS_HOSTNAME;
    if (!host) {
        throw new errors_1.HttpError(422, 'unprocessable', 'tunnel ingress requested but TUNNEL_INGRESS_HOSTNAME is not configured ' +
            'on the control plane (set it to the tunnel hostname, e.g. ' +
            '<tunnel-id>.cfargotunnel.com, or request ingress "direct")');
    }
    return host;
}
/** CNAME target for a mode: tunnel -> tunnel hostname, direct -> ingress. */
function cnameTargetFor(cfg, mode) {
    if (mode === 'tunnel')
        return requireTunnelHostname();
    if (!cfg.ingressHostname) {
        throw new errors_1.HttpError(422, 'unprocessable', 'direct ingress requested but PUBLIC_INGRESS_HOSTNAME is not configured ' +
            'on the control plane');
    }
    return cfg.ingressHostname;
}
function isValidHostname(hostname) {
    if (typeof hostname !== 'string' || hostname.length > 253 || hostname.length < 3)
        return false;
    // RFC 1035-ish: labels of alphanumerics/hyphens, dots between, TLD alpha.
    return /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/i.test(hostname);
}
async function cfFetch(cfg, method, path, body) {
    const res = await fetch(`${API_BASE}${path}`, {
        method,
        headers: {
            Authorization: `Bearer ${cfg.token}`,
            'Content-Type': 'application/json',
        },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try {
        data = (await res.json());
    }
    catch {
        throw new errors_1.HttpError(502, 'bad_gateway', 'cloudflare returned a non-JSON response');
    }
    if (!res.ok || data.success !== true) {
        const msg = data.errors?.map((e) => `${e.code}: ${e.message}`).join('; ') || `HTTP ${res.status}`;
        log_1.logger.error('cloudflare api error', { method, path, msg });
        throw new errors_1.HttpError(502, 'bad_gateway', `cloudflare API error: ${msg}`);
    }
    return data.result;
}
/** Find an existing DNS record by exact name. Returns null when absent. */
async function findDnsRecord(cfg, hostname) {
    const result = (await cfFetch(cfg, 'GET', `/zones/${cfg.zoneId}/dns_records?type=CNAME&name=${encodeURIComponent(hostname)}`));
    return result[0] ?? null;
}
/**
 * Ensure a CNAME `hostname` -> `target` exists. Idempotent: if an
 * identical record already exists, its id is returned without changes.
 * `target` defaults to the direct-mode ingress hostname (backwards
 * compatible); tunnel mode passes the tunnel hostname.
 */
async function ensureCnameRecord(cfg, hostname, target) {
    const want = target ?? cfg.ingressHostname;
    const existing = await findDnsRecord(cfg, hostname);
    if (existing && existing.type === 'CNAME') {
        return existing.id;
    }
    const created = (await cfFetch(cfg, 'POST', `/zones/${cfg.zoneId}/dns_records`, {
        type: 'CNAME',
        name: hostname,
        content: want,
        ttl: 300,
        proxied: true,
    }));
    log_1.logger.info('cloudflare cname created', { hostname, target: want, id: created.id });
    return created.id;
}
/** Delete a DNS record by id. Missing records are treated as success. */
async function deleteDnsRecord(cfg, recordId) {
    try {
        await cfFetch(cfg, 'DELETE', `/zones/${cfg.zoneId}/dns_records/${recordId}`);
    }
    catch (err) {
        // Cloudflare error 81044 = record already gone; anything else rethrows.
        if (err instanceof errors_1.HttpError && err.message.includes('81044'))
            return;
        throw err;
    }
    log_1.logger.info('cloudflare dns record deleted', { recordId });
}
