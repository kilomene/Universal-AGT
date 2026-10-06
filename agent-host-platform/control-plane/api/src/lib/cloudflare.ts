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

const DEFAULT_API_BASE = 'https://api.cloudflare.com/client/v4';

/**
 * Base URL for the Cloudflare API. Overridable via CLOUDFLARE_API_BASE —
 * this is the TEST hook: the automated suites point it at a local fake
 * Cloudflare server (see test/fakeCloudflare.ts). In production it is
 * always the real api.cloudflare.com endpoint; there is no reason to set
 * it outside tests.
 */
export function cloudflareApiBase(): string {
  const override = process.env.CLOUDFLARE_API_BASE;
  if (override && override.trim()) return override.replace(/\/+$/, '');
  return DEFAULT_API_BASE;
}

const API_BASE = DEFAULT_API_BASE;

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

/**
 * Suffixes that must never be served as public hostnames. RFC 6761/6762
 * special-use names plus the internal-only convention this fleet uses.
 * A hostname equal to one of these, or nested under it, is rejected.
 */
const BLOCKED_SUFFIXES = [
  'localhost',
  'local',      // mDNS (RFC 6762)
  'internal',   // internal-only convention
  'invalid',    // RFC 2606
  'example',    // RFC 2606
  'test',       // RFC 2606
  'onion',      // RFC 7686
];

export function isBlockedHostname(hostname: string): boolean {
  const lower = hostname.toLowerCase();
  return BLOCKED_SUFFIXES.some(
    (s) => lower === s || lower.endsWith(`.${s}`),
  );
}

/**
 * Strict public-hostname validation (W10 hardening).
 *
 * Rejects, in addition to malformed names:
 *  - wildcards (`*.example.com` and any label containing `*`) — a wildcard
 *    DNS record or tunnel route is a standing privilege escalation; every
 *    hostname here is explicit.
 *  - localhost / special-use / internal-only names (`localhost`,
 *    `*.localhost`, `*.local`, `*.internal`, `*.invalid`, `*.example`,
 *    `*.test`, `*.onion`).
 *  - IP literals (`127.0.0.1`, `10.0.0.5`, …): the base pattern already
 *    rejects most of these (TLD must be alpha); the explicit check below
 *    closes the remainder.
 */
export function isValidHostname(hostname: string): boolean {
  if (typeof hostname !== 'string' || hostname.length > 253 || hostname.length < 3) return false;
  if (hostname.includes('*')) return false;
  // RFC 1035-ish: labels of alphanumerics/hyphens, dots between, TLD alpha.
  if (!/^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/i.test(hostname)) return false;
  if (isBlockedHostname(hostname)) return false;
  // Defensive: a dotted quad that somehow passed the pattern is still an IP.
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(hostname)) return false;
  return true;
}

/**
 * Validate a CNAME *target* (the right-hand side of the record). Targets
 * come only from operator-controlled environment config, but they are
 * validated anyway: a target must be a valid public hostname — never an
 * IP literal (127.x / 10/8 / 172.16/12 / 192.168/16 included), never a
 * URL, never an internal-only name. Throws HttpError 422 on violation.
 */
export function validateCnameTarget(target: string): void {
  if (typeof target !== 'string' || /[:/?#@]/.test(target) || !isValidHostname(target)) {
    throw new HttpError(
      422,
      'unprocessable',
      `refusing CNAME target ${JSON.stringify(target)}: must be a valid ` +
        'public hostname (never an IP address, URL, or internal-only name)',
    );
  }
}

interface CfResult {
  id: string;
  name: string;
  type: string;
  /** Record content (the CNAME target). Present on DNS record reads. */
  content?: string;
}

/**
 * Raw Cloudflare API call with the standard success envelope. Shared by
 * the DNS module and the tunnel module (cloudflare-tunnel.ts). Throws
 * HttpError 502 on transport or API errors.
 */
export async function cfApi(
  token: string,
  method: string,
  path: string,
  body?: unknown,
): Promise<unknown> {
  const res = await fetch(`${cloudflareApiBase()}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
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

async function cfFetch(cfg: CfConfig, method: string, path: string, body?: unknown): Promise<unknown> {
  return cfApi(cfg.token, method, path, body);
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
 *
 * SECURITY (W16 audit): an existing record is only adopted when its
 * content matches the desired target (case-insensitive, trailing dot
 * ignored). Adopting a same-named record that points elsewhere would let
 * a domain claim TAKE OVER an operator-managed hostname — and the later
 * DELETE would then delete the operator's record. A mismatch is a 409.
 */
export async function ensureCnameRecord(
  cfg: CfConfig,
  hostname: string,
  target?: string,
): Promise<string> {
  const want = target ?? cfg.ingressHostname;
  validateCnameTarget(want);
  const existing = await findDnsRecord(cfg, hostname);
  if (existing && existing.type === 'CNAME') {
    const current = (existing.content ?? '').toLowerCase().replace(/\.$/, '');
    if (current === want.toLowerCase().replace(/\.$/, '')) {
      return existing.id;
    }
    throw new HttpError(
      409,
      'conflict',
      `refusing to adopt DNS record for ${hostname}: it already points at ` +
        `${existing.content || '(unknown target)'}, not the ingress target`,
    );
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
