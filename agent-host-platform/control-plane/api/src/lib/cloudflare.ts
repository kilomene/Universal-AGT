import { HttpError } from './errors';
import { logger } from './log';

// Optional Cloudflare DNS automation (Phase 5).
//
// Architectural rule: this system never stores host IP addresses, so the
// control plane CANNOT create A-records pointing at a host. The supported
// model is CNAME -> PUBLIC_INGRESS_HOSTNAME, where the ingress is something
// the operator runs in front of the host (e.g. a cloudflared tunnel, a
// reverse proxy, or a load balancer). DNS is only touched when all three
// env vars are set; otherwise domain requests are recorded as metadata
// (status 'dns_pending') and an event notes that DNS was skipped.
//
// Credentials are scoped: use a Cloudflare API token limited to
// "Zone / DNS / Edit" on exactly the zone(s) you serve.

const API_BASE = 'https://api.cloudflare.com/client/v4';

export interface CfConfig {
  token: string;
  zoneId: string;
  ingressHostname: string;
}

export function getCloudflareConfig(): CfConfig | null {
  const token = process.env.CLOUDFLARE_API_TOKEN;
  const zoneId = process.env.CLOUDFLARE_ZONE_ID;
  const ingressHostname = process.env.PUBLIC_INGRESS_HOSTNAME;
  if (!token || !zoneId || !ingressHostname) return null;
  return { token, zoneId, ingressHostname };
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
 * Ensure a CNAME `hostname` -> ingress hostname exists. Idempotent: if an
 * identical record already exists, its id is returned without changes.
 */
export async function ensureCnameRecord(cfg: CfConfig, hostname: string): Promise<string> {
  const existing = await findDnsRecord(cfg, hostname);
  if (existing && existing.type === 'CNAME') {
    return existing.id;
  }
  const created = (await cfFetch(cfg, 'POST', `/zones/${cfg.zoneId}/dns_records`, {
    type: 'CNAME',
    name: hostname,
    content: cfg.ingressHostname,
    ttl: 300,
    proxied: true,
  })) as CfResult;
  logger.info('cloudflare cname created', { hostname, target: cfg.ingressHostname, id: created.id });
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
