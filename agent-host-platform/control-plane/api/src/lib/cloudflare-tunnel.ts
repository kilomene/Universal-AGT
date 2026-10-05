import { HttpError } from './errors';
import { logger } from './log';
import { cfApi, isValidHostname } from './cloudflare';

// Remote Cloudflare Tunnel configuration (the ROUTING AUTHORITY).
//
// Control-authority statement (W9): for token-based (remotely-managed)
// tunnels — the only kind this system runs (`cloudflared tunnel --token
// <TOKEN> run`) — the route table that actually moves traffic lives in
// Cloudflare, not on the host. cloudflared IGNORES local config.yml
// ingress rules for remotely-managed tunnels; it pulls its configuration
// from the edge and picks up a new remote version within seconds (no
// restart needed). The worker's local config.yml is a NON-AUTHORITATIVE
// mirror for operators to inspect, nothing more.
//
// This module is the one code path that writes the authoritative table,
// via:
//
//   GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations
//   PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations
//       {config: {ingress: [{hostname, service}, …, {service:
//       "http_status:404"}]}}
//
// PUT is a FULL REPLACE of the ingress array, so every write is a
// read-modify-write: managed hostnames (this system's active tunnel
// domains) are upserted, operator-managed hostnames are preserved
// untouched, and the catch-all stays last. Every write is followed by a
// verifying GET.
//
// Credentials: an API token with "Account / Cloudflare Tunnel / Edit".
// Set CLOUDFLARE_TUNNEL_API_TOKEN for a dedicated tunnel token, or reuse
// CLOUDFLARE_API_TOKEN when that token also carries the tunnel permission.
// CLOUDFLARE_ACCOUNT_ID is required either way, and the tunnel id is
// parsed from TUNNEL_INGRESS_HOSTNAME (<tunnel-id>.cfargotunnel.com).
// When any piece is missing, getTunnelApiConfig() returns null and tunnel
// routes are NOT managed remotely — the domain flow then says so plainly
// instead of pretending the local config file does anything.
//
// The tunnel run-token (WORKER_TUNNEL_TOKEN on the host) CANNOT call this
// API — it only authorizes the tunnel connection itself.

export interface TunnelApiConfig {
  token: string;
  accountId: string;
  tunnelId: string;
}

export interface TunnelIngressRule {
  hostname?: string;
  service: string;
  originRequest?: Record<string, unknown>;
  [k: string]: unknown;
}

/** Minimal DB surface this module needs (kept structural for tests). */
export interface DbLike {
  query(sql: string, params?: unknown[]): Promise<{ rows: Array<Record<string, any>> }>;
}

const TUNNEL_HOST_RE =
  /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.cfargotunnel\.com$/i;

/** Parse the tunnel id out of `<tunnel-id>.cfargotunnel.com`. Null when not shaped like one. */
export function tunnelIdFromIngressHostname(hostname: string | null | undefined): string | null {
  if (typeof hostname !== 'string') return null;
  const m = hostname.trim().toLowerCase().match(TUNNEL_HOST_RE);
  return m ? m[1] : null;
}

export function getTunnelApiConfig(): TunnelApiConfig | null {
  const token = process.env.CLOUDFLARE_TUNNEL_API_TOKEN || process.env.CLOUDFLARE_API_TOKEN;
  const accountId = process.env.CLOUDFLARE_ACCOUNT_ID;
  const tunnelId = tunnelIdFromIngressHostname(process.env.TUNNEL_INGRESS_HOSTNAME);
  if (!token || !accountId || !tunnelId) return null;
  return { token, accountId, tunnelId };
}

export function tunnelApiMissingReason(): string {
  const missing: string[] = [];
  if (!process.env.CLOUDFLARE_TUNNEL_API_TOKEN && !process.env.CLOUDFLARE_API_TOKEN) {
    missing.push('CLOUDFLARE_TUNNEL_API_TOKEN (or CLOUDFLARE_API_TOKEN with the tunnel permission)');
  }
  if (!process.env.CLOUDFLARE_ACCOUNT_ID) missing.push('CLOUDFLARE_ACCOUNT_ID');
  if (!tunnelIdFromIngressHostname(process.env.TUNNEL_INGRESS_HOSTNAME)) {
    missing.push('TUNNEL_INGRESS_HOSTNAME shaped like <tunnel-id>.cfargotunnel.com');
  }
  return (
    `tunnel routes cannot be managed remotely: missing ${missing.join(', ')}. ` +
    'For token-based tunnels the route table lives in Cloudflare, not in the ' +
    'worker\'s local config.yml — add the public hostname in the Cloudflare ' +
    'dashboard (Zero Trust → Networks → Tunnels → Public hostnames), or set ' +
    'the variables above to let the control plane manage routes via the API.'
  );
}

function tunnelConfigPath(cfg: TunnelApiConfig): string {
  return `/accounts/${cfg.accountId}/cfd_tunnel/${cfg.tunnelId}/configurations`;
}

/** Fetch the tunnel's current remote ingress rules. */
export async function getRemoteTunnelIngress(cfg: TunnelApiConfig): Promise<TunnelIngressRule[]> {
  const result = (await cfApi(cfg.token, 'GET', tunnelConfigPath(cfg))) as {
    config?: { ingress?: TunnelIngressRule[] };
  };
  const ingress = result?.config?.ingress;
  if (!Array.isArray(ingress)) {
    throw new HttpError(502, 'bad_gateway', 'cloudflare returned a tunnel configuration without an ingress array');
  }
  return ingress;
}

/** The origin service for a managed hostname. Loopback ONLY — never an arbitrary URL. */
export function originServiceFor(port: number): string {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new HttpError(500, 'internal', `refusing tunnel route: invalid origin port ${port}`);
  }
  return `http://127.0.0.1:${port}`;
}

export interface DesiredRoute {
  hostname: string;
  port: number;
}

/**
 * Reconcile the remote tunnel ingress table against the desired routes.
 *
 * Read-modify-write: managed hostnames are upserted (service updated to
 * the current origin port, other rule fields preserved); managed
 * hostnames absent from `desired` (removed domains, dead deployments)
 * have their rules DROPPED; hostnames the operator manages outside this
 * system are left untouched; the catch-all is forced last. Returns the
 * verified remote rule list.
 *
 * Throws HttpError 502 when the verifying read does not show every
 * desired hostname with its exact service.
 */
export async function setRemoteTunnelIngress(
  cfg: TunnelApiConfig,
  desired: DesiredRoute[],
  managed: Set<string> = new Set(desired.map((d) => d.hostname.toLowerCase())),
): Promise<TunnelIngressRule[]> {
  for (const d of desired) {
    if (!isValidHostname(d.hostname)) {
      throw new HttpError(400, 'bad_request', `refusing tunnel route for invalid hostname ${d.hostname}`);
    }
  }
  const want = new Map<string, string>();
  for (const d of desired) want.set(d.hostname.toLowerCase(), originServiceFor(d.port));

  const current = await getRemoteTunnelIngress(cfg);
  const next: TunnelIngressRule[] = [];
  const handled = new Set<string>();
  let catchAll: TunnelIngressRule | null = null;

  for (const rule of current) {
    const name = typeof rule.hostname === 'string' ? rule.hostname.toLowerCase() : null;
    if (name && want.has(name)) {
      next.push({ ...rule, hostname: rule.hostname, service: want.get(name)! });
      handled.add(name);
    } else if (name && managed.has(name)) {
      // Ours, but no longer desired (domain removed, deployment dead):
      // drop the rule so traffic stops.
      logger.info('tunnel reconcile dropping stale rule', { hostname: rule.hostname });
    } else if (name) {
      next.push(rule); // operator-managed hostname: preserve verbatim
    } else if (rule.service === 'http_status:404') {
      catchAll = rule; // re-appended last below
    } else {
      next.push(rule); // hostname-less rule (e.g. path rules): preserve
    }
  }
  for (const [name, service] of want) {
    if (!handled.has(name)) {
      const original = desired.find((d) => d.hostname.toLowerCase() === name)!;
      next.push({ hostname: original.hostname, service, originRequest: {} });
    }
  }
  next.push(catchAll ?? { service: 'http_status:404' });

  await cfApi(cfg.token, 'PUT', tunnelConfigPath(cfg), { config: { ingress: next } });
  logger.info('tunnel remote ingress updated', {
    tunnel: cfg.tunnelId,
    managed: want.size,
    rules: next.length,
  });

  // Verify: the authority must show every desired hostname with its service.
  const verified = await getRemoteTunnelIngress(cfg);
  const byName = new Map(
    verified.filter((r) => typeof r.hostname === 'string')
      .map((r) => [(r.hostname as string).toLowerCase(), r.service]),
  );
  const missing: string[] = [];
  for (const [name, service] of want) {
    if (byName.get(name) !== service) missing.push(`${name} -> ${service}`);
  }
  if (missing.length > 0) {
    throw new HttpError(
      502,
      'bad_gateway',
      `tunnel route verification failed: remote config does not show ${missing.join(', ')}`,
    );
  }
  return verified;
}

/**
 * The host port a deployment's tunnel routes should terminate at.
 * Prefers the port registry (port_allocations, lowest port), falls back
 * to the worker-reported deployments.ports mapping (lowest host port).
 * Null when the deployment publishes nothing — the caller must then
 * refuse to create a tunnel route rather than guess.
 */
export async function deploymentHostPort(db: DbLike, deploymentId: string): Promise<number | null> {
  const reg = await db.query(
    'SELECT port FROM port_allocations WHERE deployment_id = $1 ORDER BY port ASC LIMIT 1',
    [deploymentId],
  );
  if (reg.rows[0] && Number.isInteger(reg.rows[0].port)) return reg.rows[0].port as number;
  const dep = await db.query('SELECT ports FROM deployments WHERE id = $1', [deploymentId]);
  const ports = dep.rows[0]?.ports;
  if (ports && typeof ports === 'object') {
    const nums = Object.keys(ports)
      .map((k) => Number(k))
      .filter((n) => Number.isInteger(n) && n >= 1 && n <= 65535)
      .sort((a, b) => a - b);
    if (nums.length > 0) return nums[0];
  }
  return null;
}

/** Deployments whose domains must not be routed (dead or dying). */
export const NON_ROUTABLE_DEPLOYMENT_STATUSES = ['failed', 'rolled_back', 'stopped', 'stopping'] as const;

export interface ReconcileResult {
  ok: boolean;
  managed: number;
  error?: string;
}

export interface CollectedRoutes {
  desired: DesiredRoute[];
  /** Every hostname this system manages (tunnel-mode rows in any live
   *  status). Remote rules for managed hostnames that are NOT in
   *  `desired` are dropped by the reconcile; anything else is left
   *  untouched as operator-managed. */
  managed: Set<string>;
  skipped: string[];
}

/**
 * Every ACTIVE tunnel-mode domain on a routable deployment, with the
 * origin port each route must terminate at. `extra` lets a caller include
 * a domain that is mid-provisioning (status 'configuring') so a single
 * reconcile both adds it and heals the rest of the table.
 */
export async function collectDesiredRoutes(db: DbLike, extra?: DesiredRoute): Promise<CollectedRoutes> {
  const { rows } = await db.query(
    `SELECT d.hostname, d.deployment_id
     FROM domains d
     JOIN deployments dep ON dep.id = d.deployment_id
     WHERE d.status = 'active'
       AND d.ingress = 'tunnel'
       AND NOT (dep.status = ANY($1))`,
    [Array.from(NON_ROUTABLE_DEPLOYMENT_STATUSES)],
  );
  const desired: DesiredRoute[] = [];
  const skipped: string[] = [];
  const seen = new Set<string>();
  if (extra) {
    desired.push(extra);
    seen.add(extra.hostname.toLowerCase());
  }
  for (const row of rows) {
    const hostname = row.hostname as string;
    if (seen.has(hostname.toLowerCase())) continue;
    seen.add(hostname.toLowerCase());
    const port = await deploymentHostPort(db, row.deployment_id as string);
    if (port === null) {
      skipped.push(`${hostname}: no published host port`);
      continue;
    }
    desired.push({ hostname, port });
  }
  // Hostnames this system owns: every tunnel-mode row that is not removed.
  // A managed hostname absent from `desired` (removed domain, dead
  // deployment) has its remote rule dropped by the reconcile.
  const owned = await db.query(
    `SELECT DISTINCT lower(hostname) AS hostname FROM domains
     WHERE ingress = 'tunnel' AND status <> 'removed'`,
  );
  const managed = new Set<string>(owned.rows.map((r) => r.hostname as string));
  if (extra) managed.add(extra.hostname.toLowerCase());
  return { desired, managed, skipped };
}

/**
 * Control-plane canonical tunnel sync: every ACTIVE tunnel-mode domain on
 * a routable deployment becomes a remote ingress rule; anything else
 * (removed domains, dead deployments) drops out of the desired set and is
 * therefore removed from the remote table. Idempotent.
 */
export async function reconcileTunnelRoutes(db: DbLike): Promise<ReconcileResult> {
  const cfg = getTunnelApiConfig();
  if (!cfg) {
    return { ok: false, managed: 0, error: tunnelApiMissingReason() };
  }
  const { desired, managed, skipped } = await collectDesiredRoutes(db);
  if (skipped.length > 0) {
    logger.warn('tunnel reconcile skipping domains without a host port', { skipped });
  }
  try {
    await setRemoteTunnelIngress(cfg, desired, managed);
    return { ok: true, managed: desired.length };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.error('tunnel reconcile failed', { err: msg });
    return { ok: false, managed: 0, error: msg };
  }
}
