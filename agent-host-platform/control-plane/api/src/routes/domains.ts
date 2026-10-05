import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { requireAgent, requirePermission } from '../middleware/auth';
import {
  deleteDnsRecord,
  ensureCnameRecord,
  findDnsRecord,
  getCloudflareConfig,
  isValidHostname,
  cnameTargetFor,
  requireTunnelHostname,
  resolveIngressMode,
  type CfConfig,
  type IngressMode,
} from '../lib/cloudflare';
import {
  collectDesiredRoutes,
  deploymentHostPort,
  getTunnelApiConfig,
  getRemoteTunnelIngress,
  originServiceFor,
  reconcileTunnelRoutes,
  setRemoteTunnelIngress,
  tunnelApiMissingReason,
  NON_ROUTABLE_DEPLOYMENT_STATUSES,
} from '../lib/cloudflare-tunnel';
import { isUuid } from './_helpers';

export const domainsRouter = Router();

// Domain lifecycle (migration 007, table `domains`):
//
//   requested -> configuring -> active -> failed -> removing -> removed
//                                  \-> failed (deployment died; reprovisionable)
//   failed -> configuring (retry) is the only way back.
//
// The table is the source of truth. `deployments.domains` (JSONB) is kept
// as a derived summary mirror for GET /v1/services and the dashboard; it
// is rebuilt from the table on every transition and never contains
// 'removed' rows.
//
// Provisioning (POST) performs, in order:
//   1. hostname validation (strict: no wildcards, no localhost/internal/
//      special-use names, no IP literals)
//   2. DNS: proxied CNAME hostname -> ingress target (Cloudflare DNS API)
//   3. tunnel mode: remote tunnel ingress rule hostname ->
//      http://127.0.0.1:<deployment host port> (Cloudflare tunnel API —
//      the REMOTE configuration is the routing authority for token-based
//      tunnels; the worker's local config.yml is only a mirror)
//   4. verification: re-query the Cloudflare API to confirm the record
//      and the route exist
//   5. HTTPS reachability probe (informational: recorded, never blocks
//      'active')
// Removal (DELETE) reverses it: DNS record deleted, tunnel route removed
// via a full reconcile, both verified gone, then 'removed'.

type DomainStatus = 'requested' | 'configuring' | 'active' | 'failed' | 'removing' | 'removed';

interface DomainRow {
  id: string;
  hostname: string;
  deployment_id: string;
  host_id: string;
  ingress: IngressMode;
  status: DomainStatus;
  cf_record_id: string | null;
  dns_configured: boolean;
  tunnel_configured: boolean;
  https_reachable: boolean;
  https_status: number | null;
  https_checked_at: string | null;
  verified_at: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

type Pool = ReturnType<typeof getPool>;

const TERMINAL_DEPLOYMENT = new Set<string>(NON_ROUTABLE_DEPLOYMENT_STATUSES);

const ALLOWED_TRANSITIONS: Record<DomainStatus, DomainStatus[]> = {
  requested: ['configuring', 'removing'],
  configuring: ['active', 'failed', 'removing'],
  active: ['failed', 'removing', 'configuring'],
  failed: ['configuring', 'removing'],
  removing: ['removed'],
  removed: [],
};

/** Public shape of a domain row (no secrets; cf_record_id is not secret). */
function publicDomain(row: DomainRow, tunnelHost: string | null) {
  return {
    id: row.id,
    hostname: row.hostname,
    deployment_id: row.deployment_id,
    host_id: row.host_id,
    ingress: row.ingress,
    status: row.status,
    dns_configured: row.dns_configured,
    tunnel_configured: row.tunnel_configured,
    https_reachable: row.https_reachable,
    https_status: row.https_status,
    https_checked_at: row.https_checked_at,
    verified_at: row.verified_at,
    error: row.error,
    tunnel_host: row.ingress === 'tunnel' ? tunnelHost : null,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

async function getDomain(pool: Pool, id: string): Promise<DomainRow | undefined> {
  const { rows } = await pool.query('SELECT * FROM domains WHERE id = $1', [id]);
  return rows[0] as DomainRow | undefined;
}

async function getLiveDomain(
  pool: Pool,
  deploymentId: string,
  hostname: string,
): Promise<DomainRow | undefined> {
  const { rows } = await pool.query(
    `SELECT * FROM domains
     WHERE deployment_id = $1 AND lower(hostname) = lower($2) AND status <> 'removed'`,
    [deploymentId, hostname],
  );
  return rows[0] as DomainRow | undefined;
}

async function setDomainStatus(
  pool: Pool,
  id: string,
  from: DomainStatus,
  to: DomainStatus,
  fields: Record<string, unknown> = {},
): Promise<DomainRow> {
  if (!ALLOWED_TRANSITIONS[from].includes(to)) {
    throw new HttpError(409, 'conflict', `illegal domain transition ${from} -> ${to}`);
  }
  const keys = Object.keys(fields);
  const params: unknown[] = [to];
  const setClauses = keys.map((k, i) => {
    params.push(fields[k]);
    return `${k} = $${i + 2}`;
  });
  setClauses.unshift('status = $1');
  params.push(id);
  const { rows } = await pool.query(
    `UPDATE domains SET ${setClauses.join(', ')} WHERE id = $${params.length} RETURNING *`,
    params,
  );
  const row = rows[0] as DomainRow;
  await syncDeploymentDomainsMirror(pool, row.deployment_id);
  return row;
}

/**
 * Rebuild the deployments.domains JSONB summary mirror from the domains
 * table (excludes 'removed'). The table is authoritative; this is a
 * read convenience for /v1/services and the dashboard.
 */
export async function syncDeploymentDomainsMirror(pool: Pool, deploymentId: string): Promise<void> {
  const { rows } = await pool.query(
    `SELECT hostname, status, ingress, cf_record_id, error, created_at
     FROM domains WHERE deployment_id = $1 AND status <> 'removed'
     ORDER BY created_at ASC`,
    [deploymentId],
  );
  const mirror = rows.map((r) => ({
    hostname: r.hostname,
    status: r.status,
    ingress: r.ingress,
    cf_record_id: r.cf_record_id,
    error: r.error,
    added_at: r.created_at instanceof Date ? r.created_at.toISOString() : r.created_at,
  }));
  await pool.query('UPDATE deployments SET domains = $1::jsonb WHERE id = $2', [
    JSON.stringify(mirror),
    deploymentId,
  ]);
}

async function getDeployment(pool: Pool, id: string) {
  const { rows } = await pool.query(
    'SELECT id, project_id, host_id, status FROM deployments WHERE id = $1',
    [id],
  );
  return rows[0] as
    | { id: string; project_id: string; host_id: string; status: string }
    | undefined;
}

/** HTTPS reachability probe. Informational only: any HTTP response (even
 *  4xx/5xx) proves the path resolves through the edge to an origin. */
async function probeHttps(hostname: string): Promise<{ reachable: boolean; status: number | null }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(`https://${hostname}/`, {
      signal: ctrl.signal,
      redirect: 'follow',
    });
    try {
      await res.body?.cancel();
    } catch {
      /* best effort */
    }
    return { reachable: true, status: res.status };
  } catch {
    return { reachable: false, status: null };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Idempotent provisioning: requested/configuring/failed -> configuring ->
 * active (or failed with a clear error). Every step persists its own
 * state, so a crash mid-provision leaves 'configuring' with partial
 * flags and the next attempt resumes. Safe to call repeatedly.
 */
export async function provisionDomain(pool: Pool, domainId: string): Promise<DomainRow> {
  let domain = await getDomain(pool, domainId);
  if (!domain) throw new HttpError(404, 'not_found', 'domain not found');
  if (domain.status === 'removed' || domain.status === 'removing') return domain;

  const dep = await getDeployment(pool, domain.deployment_id);
  if (!dep) {
    return failDomain(pool, domain, 'deployment row is gone');
  }
  if (TERMINAL_DEPLOYMENT.has(dep.status)) {
    return failDomain(pool, domain, `deployment is ${dep.status}; domain cannot route to it`);
  }

  if (domain.status === 'requested' || domain.status === 'failed') {
    domain = await setDomainStatus(pool, domain.id, domain.status, 'configuring', { error: null });
  }

  const cfCfg = getCloudflareConfig();
  const tunnelCfg = domain.ingress === 'tunnel' ? getTunnelApiConfig() : null;
  const patch: Record<string, unknown> = {};

  // -- step 1: DNS -------------------------------------------------------
  if (cfCfg) {
    try {
      const target = cnameTargetFor(cfCfg, domain.ingress);
      const recordId = await ensureCnameRecord(cfCfg, domain.hostname, target);
      // Verify the record is really there.
      const found = await findDnsRecord(cfCfg, domain.hostname);
      if (!found) {
        throw new HttpError(502, 'bad_gateway', 'DNS record creation not visible on re-read');
      }
      patch.cf_record_id = recordId;
      patch.dns_configured = true;
    } catch (err) {
      return failDomain(pool, domain, `DNS configuration failed: ${errMessage(err)}`, patch);
    }
  }

  // -- step 2: tunnel route (the remote routing authority) ---------------
  if (domain.ingress === 'tunnel') {
    if (!tunnelCfg) {
      return failDomain(pool, domain, tunnelApiMissingReason(), patch);
    }
    try {
      const port = await deploymentHostPort(pool, domain.deployment_id);
      if (port === null) {
        throw new HttpError(
          422,
          'unprocessable',
          `deployment ${domain.deployment_id} publishes no host port; cannot create a tunnel route`,
        );
      }
      // Reconcile the whole remote table (self-healing) with this domain
      // included: it is still 'configuring', so it is passed as `extra`
      // rather than being picked up by the active-only collection.
      const { desired, managed } = await collectDesiredRoutes(pool, { hostname: domain.hostname, port });
      await setRemoteTunnelIngress(tunnelCfg, desired, managed);
      // Verify this hostname's rule in the authoritative remote config.
      const rules = await getRemoteTunnelIngress(tunnelCfg);
      const wantName = domain.hostname.toLowerCase();
      const rule = rules.find(
        (r) => typeof r.hostname === 'string' && r.hostname.toLowerCase() === wantName,
      );
      const wantService = originServiceFor(port);
      if (!rule || rule.service !== wantService) {
        throw new HttpError(
          502,
          'bad_gateway',
          'tunnel route not visible in the remote configuration on re-read',
        );
      }
      patch.tunnel_configured = true;
    } catch (err) {
      return failDomain(pool, domain, `tunnel route configuration failed: ${errMessage(err)}`, patch);
    }
  }

  // -- step 3: HTTPS reachability probe (informational) -------------------
  if (patch.dns_configured || patch.tunnel_configured || domain.dns_configured || domain.tunnel_configured) {
    const probe = await probeHttps(domain.hostname);
    patch.https_reachable = probe.reachable;
    patch.https_status = probe.status;
    patch.https_checked_at = new Date().toISOString();
  }
  patch.verified_at = new Date().toISOString();

  domain = await setDomainStatus(pool, domain.id, domain.status, 'active', patch);
  await appendEvent(pool, {
    type: 'domain.active',
    actor_type: 'system',
    actor_id: 'domain-provisioner',
    deployment_id: domain.deployment_id,
    host_id: domain.host_id,
    payload: {
      hostname: domain.hostname,
      ingress: domain.ingress,
      dns_configured: domain.dns_configured,
      tunnel_configured: domain.tunnel_configured,
      https_reachable: domain.https_reachable,
      https_status: domain.https_status,
    },
  });
  logger.info('domain active', { hostname: domain.hostname, ingress: domain.ingress });
  return domain;
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function failDomain(
  pool: Pool,
  domain: DomainRow,
  error: string,
  patch: Record<string, unknown> = {},
): Promise<DomainRow> {
  const message = error.length > 500 ? `${error.slice(0, 497)}…` : error;
  const row = await setDomainStatus(pool, domain.id, domain.status, 'failed', {
    ...patch,
    error: message,
  });
  await appendEvent(pool, {
    type: 'domain.failed',
    actor_type: 'system',
    actor_id: 'domain-provisioner',
    deployment_id: domain.deployment_id,
    host_id: domain.host_id,
    payload: { hostname: domain.hostname, ingress: domain.ingress, error: message },
  });
  logger.warn('domain failed', { hostname: domain.hostname, error: message });
  return row;
}

/**
 * Clean removal: delete the DNS record, drop the tunnel route via a full
 * reconcile, verify both are gone, then mark 'removed'. Verified-gone
 * failures keep the row in 'removing' with the error (retry by re-issuing
 * DELETE) rather than pretending the detachment happened.
 */
export async function removeDomain(pool: Pool, domainId: string, actorName: string): Promise<DomainRow> {
  const loaded = await getDomain(pool, domainId);
  if (!loaded) throw new HttpError(404, 'not_found', 'domain not found');
  if (loaded.status === 'removed') return loaded;
  // From here on the row is known-present; keep it in a non-optional
  // variable so closures don't trip TS narrowing.
  let domain: DomainRow =
    loaded.status !== 'removing'
      ? await setDomainStatus(pool, loaded.id, loaded.status, 'removing', {})
      : loaded;

  const cfCfg = getCloudflareConfig();
  const errors: string[] = [];

  // -- DNS teardown ------------------------------------------------------
  if (cfCfg && (domain.cf_record_id || domain.dns_configured)) {
    try {
      if (domain.cf_record_id) await deleteDnsRecord(cfCfg, domain.cf_record_id);
      const stillThere = await findDnsRecord(cfCfg, domain.hostname);
      if (stillThere) {
        // A same-named record exists that we did not create (or the
        // delete did not take): do not claim removal.
        errors.push('DNS record still present after delete');
      } else {
        await pool.query(
          'UPDATE domains SET dns_configured = false, cf_record_id = null WHERE id = $1',
          [domain.id],
        );
        domain = (await getDomain(pool, domain.id))!;
      }
    } catch (err) {
      errors.push(`DNS delete failed: ${errMessage(err)}`);
    }
  }

  // -- tunnel route teardown (remote authority) ---------------------------
  if (domain.ingress === 'tunnel' && domain.tunnel_configured) {
    const tunnelCfg = getTunnelApiConfig();
    if (tunnelCfg) {
      try {
        const result = await reconcileTunnelRoutes(pool);
        if (!result.ok) throw new HttpError(502, 'bad_gateway', result.error ?? 'reconcile failed');
        const rules = await getRemoteTunnelIngress(tunnelCfg);
        const wantName = domain.hostname.toLowerCase();
        const stillThere = rules.some(
          (r) => typeof r.hostname === 'string' && r.hostname.toLowerCase() === wantName,
        );
        if (stillThere) {
          errors.push('tunnel route still present in remote config after reconcile');
        } else {
          await pool.query('UPDATE domains SET tunnel_configured = false WHERE id = $1', [domain.id]);
          domain = (await getDomain(pool, domain.id))!;
        }
      } catch (err) {
        errors.push(`tunnel route removal failed: ${errMessage(err)}`);
      }
    } else {
      errors.push(
        'tunnel route cannot be removed remotely: tunnel API is not configured; ' +
          'remove the public hostname in the Cloudflare dashboard, then re-issue DELETE',
      );
    }
  }

  if (errors.length > 0) {
    const message = errors.join('; ');
    await pool.query('UPDATE domains SET error = $2 WHERE id = $1', [domain.id, message]);
    await syncDeploymentDomainsMirror(pool, domain.deployment_id);
    await appendEvent(pool, {
      type: 'domain.remove_failed',
      actor_type: 'system',
      actor_id: 'domain-provisioner',
      deployment_id: domain.deployment_id,
      host_id: domain.host_id,
      payload: { hostname: domain.hostname, error: message },
    });
    throw new HttpError(502, 'bad_gateway', `domain detachment incomplete: ${message}`);
  }

  domain = await setDomainStatus(pool, domain.id, domain.status, 'removed', { error: null });
  await appendEvent(pool, {
    type: 'domain.removed',
    actor_type: 'agent',
    actor_id: actorName,
    deployment_id: domain.deployment_id,
    host_id: domain.host_id,
    payload: { hostname: domain.hostname, ingress: domain.ingress },
  });
  logger.info('domain removed', { hostname: domain.hostname });
  return domain;
}

/**
 * Deployment-death hook: a domain must never silently point at a dead
 * deployment. Called when a deployment goes failed/stopped/rolled_back —
 * marks attached live domains 'failed' (with an event each) and
 * reconciles the remote tunnel table so the routes actually stop.
 * Domains are NOT deleted: the rows stay as the control plane's record,
 * and they reprovision automatically if the deployment comes back.
 */
export async function failDomainsForDeployment(pool: Pool, deploymentId: string, reason: string): Promise<number> {
  const { rows } = await pool.query(
    `SELECT * FROM domains
     WHERE deployment_id = $1 AND status IN ('requested', 'configuring', 'active')`,
    [deploymentId],
  );
  let count = 0;
  for (const row of rows as DomainRow[]) {
    await failDomain(pool, row, reason);
    count += 1;
  }
  if (count > 0) {
    // Drop the dead routes from the authoritative remote table.
    try {
      await reconcileTunnelRoutes(pool);
    } catch (err) {
      logger.warn('tunnel reconcile after deployment death failed', {
        deployment: deploymentId,
        err: errMessage(err),
      });
    }
  }
  return count;
}

/**
 * Deployment-recovery hook: when a deployment comes back to 'running'
 * (fresh deploy or explicit start), retry provisioning its failed
 * domains so hostnames heal without operator action.
 */
export async function reprovisionDomainsForDeployment(pool: Pool, deploymentId: string): Promise<number> {
  const { rows } = await pool.query(
    `SELECT id FROM domains WHERE deployment_id = $1 AND status = 'failed'`,
    [deploymentId],
  );
  let count = 0;
  for (const row of rows as Array<{ id: string }>) {
    try {
      await provisionDomain(pool, row.id);
      count += 1;
    } catch (err) {
      logger.warn('domain reprovision failed', { domain: row.id, err: errMessage(err) });
    }
  }
  return count;
}

// POST /v1/domains {deployment_id, hostname, ingress?}
// Full flow: validate -> requested -> configuring -> active|failed.
// `ingress` accepts 'tunnel' | 'direct'; tunnel mode without
// TUNNEL_INGRESS_HOSTNAME configured is a 422. Attaching to a terminal
// deployment is a 422 — a domain must never point at a dead deployment.
domainsRouter.post('/', requireAgent, requirePermission('manage_domains'), async (req, res, next) => {
  try {
    const { deployment_id, hostname } = req.body ?? {};
    if (!isUuid(deployment_id)) {
      sendError(res, 400, 'bad_request', 'deployment_id must be a UUID');
      return;
    }
    if (!isValidHostname(hostname)) {
      sendError(
        res,
        400,
        'bad_request',
        'hostname is not a valid public DNS name (no wildcards, no localhost/internal/special-use names, no IP literals)',
      );
      return;
    }
    let mode: IngressMode;
    try {
      mode = resolveIngressMode(req.body?.ingress);
    } catch (err) {
      next(err);
      return;
    }
    const pool = getPool();
    const dep = await getDeployment(pool, deployment_id);
    if (!dep) {
      next(new HttpError(404, 'not_found', 'deployment not found'));
      return;
    }
    if (TERMINAL_DEPLOYMENT.has(dep.status)) {
      next(
        new HttpError(
          422,
          'unprocessable',
          `cannot attach a domain: deployment is ${dep.status}`,
        ),
      );
      return;
    }
    if (mode === 'tunnel') {
      // 422 when the operator asked for tunnel mode without configuring it.
      requireTunnelHostname();
    }
    if (await getLiveDomain(pool, deployment_id, hostname)) {
      sendError(res, 409, 'conflict', 'hostname already attached to this deployment');
      return;
    }
    // A hostname may only be live on one deployment at a time.
    const { rows: clash } = await pool.query(
      `SELECT deployment_id FROM domains
       WHERE lower(hostname) = lower($1) AND status <> 'removed' LIMIT 1`,
      [hostname],
    );
    if (clash.length > 0) {
      sendError(res, 409, 'conflict', 'hostname is already attached to another deployment');
      return;
    }

    const inserted = await pool.query(
      `INSERT INTO domains (hostname, deployment_id, host_id, ingress, status)
       VALUES ($1, $2, $3, $4, 'requested') RETURNING *`,
      [hostname, deployment_id, dep.host_id, mode],
    );
    let domain = inserted.rows[0] as DomainRow;
    await appendEvent(pool, {
      type: 'domain.requested',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      deployment_id,
      host_id: dep.host_id,
      payload: { hostname, ingress: mode },
    });

    let ingressTaskId: string | null = null;
    try {
      domain = await provisionDomain(pool, domain.id);
    } catch (err) {
      // provisionDomain only throws for unexpected failures; the row
      // keeps whatever state was persisted and the error is reported.
      logger.error('domain provisioning threw', { hostname, err: errMessage(err) });
    }
    if (mode === 'tunnel') {
      ingressTaskId = await enqueueIngressSync(pool, dep.host_id, req.auth!.id, {
        trigger: 'domain.added',
        hostname,
      });
    }
    const tunnelHost = mode === 'tunnel' ? process.env.TUNNEL_INGRESS_HOSTNAME ?? null : null;
    res.status(201).json({ domain: publicDomain(domain, tunnelHost), ingress_task_id: ingressTaskId });
  } catch (err) {
    if (err instanceof Error && 'code' in err && (err as { code: string }).code === '23505') {
      sendError(res, 409, 'conflict', 'hostname is already attached to another deployment');
      return;
    }
    next(err);
  }
});

/**
 * Queue an `ingress-sync` task for a host so its worker refreshes its
 * LOCAL route mirror (config.yml). Note: for token-based tunnels this
 * does not change routing — the authoritative table is the remote tunnel
 * configuration, which the control plane updates directly via the
 * Cloudflare API. Returns the task id, or null when the insert fails.
 */
async function enqueueIngressSync(
  pool: Pool,
  hostId: string,
  createdBy: string,
  trigger: Record<string, string>,
): Promise<string | null> {
  try {
    const { rows } = await pool.query(
      `INSERT INTO tasks (created_by, assigned_to, type, status, payload)
       VALUES ($1, $2, 'ingress-sync', 'queued', $3) RETURNING id`,
      [createdBy, hostId, JSON.stringify(trigger)],
    );
    logger.info('ingress-sync task queued', { task: rows[0].id, host: hostId, trigger });
    return rows[0].id as string;
  } catch (err) {
    logger.error('failed to queue ingress-sync task', { host: hostId, err });
    return null;
  }
}

// GET /v1/domains?deployment_id= — list domains for a deployment.
domainsRouter.get('/', requireAgent, requirePermission('manage_domains'), async (req, res, next) => {
  try {
    const deploymentId = req.query.deployment_id;
    if (typeof deploymentId !== 'string' || !isUuid(deploymentId)) {
      sendError(res, 400, 'bad_request', 'deployment_id query param (UUID) is required');
      return;
    }
    const pool = getPool();
    const dep = await getDeployment(pool, deploymentId);
    if (!dep) {
      next(new HttpError(404, 'not_found', 'deployment not found'));
      return;
    }
    const { rows } = await pool.query(
      `SELECT * FROM domains WHERE deployment_id = $1 AND status <> 'removed' ORDER BY created_at ASC`,
      [deploymentId],
    );
    const tunnelHost = process.env.TUNNEL_INGRESS_HOSTNAME ?? null;
    res.json({ domains: (rows as DomainRow[]).map((r) => publicDomain(r, tunnelHost)) });
  } catch (err) {
    next(err);
  }
});

// GET /v1/domains/:hostname — single domain lifecycle row (live only).
domainsRouter.get('/:hostname', requireAgent, requirePermission('manage_domains'), async (req, res, next) => {
  try {
    const hostname = req.params.hostname;
    if (!isValidHostname(hostname)) {
      sendError(res, 400, 'bad_request', 'hostname is not a valid public DNS name');
      return;
    }
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT * FROM domains WHERE lower(hostname) = lower($1) AND status <> 'removed' LIMIT 1`,
      [hostname],
    );
    const row = rows[0] as DomainRow | undefined;
    if (!row) {
      next(new HttpError(404, 'not_found', 'hostname is not attached to any deployment'));
      return;
    }
    const tunnelHost = process.env.TUNNEL_INGRESS_HOSTNAME ?? null;
    res.json({ domain: publicDomain(row, tunnelHost) });
  } catch (err) {
    next(err);
  }
});

// DELETE /v1/domains {deployment_id, hostname} — detach: remove DNS + tunnel
// route (verified gone), then mark 'removed'.
domainsRouter.delete('/', requireAgent, requirePermission('manage_domains'), async (req, res, next) => {
  try {
    const { deployment_id, hostname } = req.body ?? {};
    if (!isUuid(deployment_id) || typeof hostname !== 'string') {
      sendError(res, 400, 'bad_request', 'deployment_id (UUID) and hostname are required');
      return;
    }
    const pool = getPool();
    const domain = await getLiveDomain(pool, deployment_id, hostname);
    if (!domain) {
      next(new HttpError(404, 'not_found', 'hostname not attached to this deployment'));
      return;
    }
    const removed = await removeDomain(pool, domain.id, req.auth!.name);
    let ingressTaskId: string | null = null;
    if (removed.ingress === 'tunnel') {
      const dep = await getDeployment(pool, deployment_id);
      if (dep) {
        ingressTaskId = await enqueueIngressSync(pool, dep.host_id, req.auth!.id, {
          trigger: 'domain.removed',
          hostname: removed.hostname,
        });
      }
    }
    res.json({ removed: removed.hostname, status: removed.status, ingress_task_id: ingressTaskId });
  } catch (err) {
    next(err);
  }
});
