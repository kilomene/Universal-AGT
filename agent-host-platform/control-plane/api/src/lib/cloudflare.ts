import { HttpError } from './errors';
import { logger } from './log';

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

/** Which ingress a domain record points at. */
export type IngressMode = 'tunnel' | 'direct';

export interface CfConfig {
  token: string;
  zoneId: string;
  /** CNAME target for 'direct' mode. Empty when only tunnel mode configured. */
  ingressHostname: string;
  /** CNAME target for 'tunnel' mode, e.g. <tunnel-id>.cfargotunnel.com. */
  tunnelHostname: string | null;
}

export function getCloudflareConfig(): CfConfig | null {
  const token = process.env.CLOUDFLARE_API_TOKEN;
  const zoneId = process.env.CLOUDFLARE_ZONE_ID;
  const ingressHostname = process.env.PUBLIC_INGRESS_HOSTNAME ?? '';
  const tunnelHostname = process.env.TUNNEL_INGRESS_HOSTNAME ?? null;
  if (!token || !zoneId || (!ingressHostname && !tunnelHostname)) return null;
  return { token, zoneId, ingressHostname, tunnelHostname };
}

/**
 * Resolve the ingress mode for a domain request. An explicit 'tunnel' /
 * 'direct' wins; otherwise tunnel mode is the default when
 * TUNNEL_INGRESS_HOSTNAME is configured, else direct. Throws 400 on a
 * garbage value.
 */
export function resolveIngressMode(requested?: unknown): IngressMode {
  if (requested === undefined || requested === null || requested === '') {
    return process.env.TUNNEL_INGRESS_HOSTNAME ? 'tunnel' : 'direct';
  }
  if (requested === 'tunnel' || requested === 'direct') return requested;
  throw new HttpError(400, 'bad_request', `ingress must be 'tunnel' or 'direct'`);
}

/**
 * The tunnel CNAME target, read straight from the environment (tunnel
 * routing does not need the DNS API token — only the hostname).
 * 422 when tunnel mode isn't configured.
 */
export function requireTunnelHostname(): string {
  const host = process.env.TUNNEL_INGRESS_HOSTNAME;
  if (!host) {
    throw new HttpError(
      422,
      'unprocessable',
      'tunnel ingress requested but TUNNEL_INGRESS_HOSTNAME is not configured ' +
        'on the control plane (set it to the tunnel hostname, e.g. ' +
        '<tunnel-id>.cfargotunnel.com, or request ingress "direct")',
    );
  }
  return host;
}

/** CNAME target for a mode: tunnel -> tunnel hostname, direct -> ingress. */
export function cnameTargetFor(cfg: CfConfig, mode: IngressMode): string {
  if (mode === 'tunnel') return requireTunnelHostname();
  if (!cfg.ingressHostname) {
    throw new HttpError(
      422,
      'unprocessable',
      'direct ingress requested but PUBLIC_INGRESS_HOSTNAME is not configured ' +
        'on the control plane',
    );
  }
  return cfg.ingressHostname;
}

export function isValidHostname(hostname: string): boolean {
  if (typeof hostname !== 'string' || hostname.length > 253 || hostname.length < 3) return false;
  // RFC 1035-ish: labels of alphanumerics/hyphens, dots between, TLD alpha.
  return /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/i.test(hostname);
}

interface CfResult {
  id: string;
  name: string;
  type: string;
}

async function cfFetch(cfg: CfConfig, method: string, path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${cfg.token}`,
      'Content-Type': 'application/json',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: {
    success?: boolean;
    result?: unknown;
    errors?: Array<{ code: number; message: string }>;
  } = {};
  try {
    data = (await res.json()) as typeof data;
  } catch {
    throw new HttpError(502, 'bad_gateway', 'cloudflare returned a non-JSON response');
  }
  if (!res.ok || data.success !== true) {
    const msg = data.errors?.map((e) => `${e.code}: ${e.message}`).join('; ') || `HTTP ${res.status}`;
    logger.error('cloudflare api error', { method, path, msg });
    throw new HttpError(502, 'bad_gateway', `cloudflare API error: ${msg}`);
  }
  return data.result;
}

/** Find an existing DNS record by exact name. Returns null when absent. */
export async function findDnsRecord(cfg: CfConfig, hostname: string): Promise<CfResult | null> {
  const result = (await cfFetch(
    cfg,
    'GET',
    `/zones/${cfg.zoneId}/dns_records?type=CNAME&name=${encodeURIComponent(hostname)}`,
  )) as CfResult[];
  return result[0] ?? null;
}

/**
 * Ensure a CNAME `hostname` -> `target` exists. Idempotent: if an
 * identical record already exists, its id is returned without changes.
 * `target` defaults to the direct-mode ingress hostname (backwards
 * compatible); tunnel mode passes the tunnel hostname.
 */
export async function ensureCnameRecord(
  cfg: CfConfig,
  hostname: string,
  target?: string,
): Promise<string> {
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
  })) as CfResult;
  logger.info('cloudflare cname created', { hostname, target: want, id: created.id });
  return created.id;
}

/** Delete a DNS record by id. Missing records are treated as success. */
export async function deleteDnsRecord(cfg: CfConfig, recordId: string): Promise<void> {
  try {
    await cfFetch(cfg, 'DELETE', `/zones/${cfg.zoneId}/dns_records/${recordId}`);
  } catch (err) {
    // Cloudflare error 81044 = record already gone; anything else rethrows.
    if (err instanceof HttpError && err.message.includes('81044')) return;
    throw err;
  }
  logger.info('cloudflare dns record deleted', { recordId });
}
