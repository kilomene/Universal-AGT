import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { requireAgent, requirePermission } from '../middleware/auth';
import {
  deleteDnsRecord,
  ensureCnameRecord,
  getCloudflareConfig,
  isValidHostname,
  cnameTargetFor,
  requireTunnelHostname,
  resolveIngressMode,
  type IngressMode,
} from '../lib/cloudflare';
import { isUuid } from './_helpers';

export const domainsRouter = Router();

// All domain routes require the manage_domains permission.

interface DomainEntry {
  hostname: string;
  cf_record_id?: string;
  status: 'active' | 'dns_pending' | 'error';
  error?: string;
  added_at: string;
  /** Phase 7: which ingress this hostname points at. Defaults to 'direct'
   *  for entries recorded before ingress modes existed. */
  ingress: IngressMode;
  /** Phase 7: the tunnel CNAME target, set on tunnel-mode entries. */
  tunnel_host?: string;
}

async function getDeployment(pool: ReturnType<typeof getPool>, id: string) {
  const { rows } = await pool.query(
    'SELECT id, project_id, host_id, domains FROM deployments WHERE id = $1',
    [id],
  );
  return rows[0] as { id: string; project_id: string; host_id: string; domains: DomainEntry[] } | undefined;
}

// POST /v1/domains {deployment_id, hostname, ingress?}
// Records the domain on the deployment; when Cloudflare is configured,
// creates a proxied CNAME hostname -> ingress target (tunnel hostname in
// tunnel mode, PUBLIC_INGRESS_HOSTNAME in direct mode, the default).
// `ingress` accepts 'tunnel' | 'direct'; tunnel mode without
// TUNNEL_INGRESS_HOSTNAME configured is a 422. Tunnel-mode changes enqueue
// an `ingress-sync` task for the host so the worker rebuilds its tunnel
// routes.
domainsRouter.post('/', requireAgent, requirePermission('manage_domains'), async (req, res, next) => {
  try {
    const { deployment_id, hostname } = req.body ?? {};
    if (!isUuid(deployment_id)) {
      sendError(res, 400, 'bad_request', 'deployment_id must be a UUID');
      return;
    }
    if (!isValidHostname(hostname)) {
      sendError(res, 400, 'bad_request', 'hostname is not a valid DNS name');
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
    const domains: DomainEntry[] = Array.isArray(dep.domains) ? dep.domains : [];
    if (domains.some((d) => d.hostname.toLowerCase() === hostname.toLowerCase())) {
      sendError(res, 409, 'conflict', 'hostname already attached to this deployment');
      return;
    }

    const entry: DomainEntry = {
      hostname,
      status: 'dns_pending',
      added_at: new Date().toISOString(),
      ingress: mode,
    };
    const cfg = getCloudflareConfig();
    if (mode === 'tunnel') {
      // 422 when the operator asked for tunnel mode without configuring it.
      // Reads the env directly: tunnel routing doesn't need the DNS token.
      entry.tunnel_host = requireTunnelHostname();
    }
    if (cfg) {
      try {
        entry.cf_record_id = await ensureCnameRecord(
          cfg, hostname, cnameTargetFor(cfg, mode));
        entry.status = 'active';
      } catch (err) {
        entry.status = 'error';
        entry.error = err instanceof Error ? err.message : String(err);
        logger.error('domain dns failed', { hostname, err: entry.error });
      }
    }
    domains.push(entry);
    await pool.query('UPDATE deployments SET domains = $1::jsonb WHERE id = $2', [
      JSON.stringify(domains),
      deployment_id,
    ]);
    let ingressTaskId: string | null = null;
    if (mode === 'tunnel') {
      ingressTaskId = await enqueueIngressSync(pool, dep.host_id, req.auth!.id,
        { trigger: 'domain.added', hostname });
    }
    await appendEvent(pool, {
      type: cfg ? 'domain.added' : 'domain.requested',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      deployment_id,
      host_id: dep.host_id,
      payload: {
        hostname, status: entry.status, dns_managed: cfg !== null,
        ingress: mode, tunnel_host: entry.tunnel_host ?? null,
        ingress_task_id: ingressTaskId,
      },
    });
    res.status(201).json({ domain: entry });
  } catch (err) {
    next(err);
  }
});

/**
 * Queue an `ingress-sync` task for a host so its worker rebuilds the
 * tunnel route table (used when tunnel-mode domains change). Returns the
 * task id, or null when the insert fails (logged; the domain record is the
 * source of truth and a later manual ingress-sync heals it).
 */
async function enqueueIngressSync(
  pool: ReturnType<typeof getPool>,
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
    res.json({ domains: Array.isArray(dep.domains) ? dep.domains : [] });
  } catch (err) {
    next(err);
  }
});

// DELETE /v1/domains {deployment_id, hostname} — detach + remove DNS record.
domainsRouter.delete('/', requireAgent, requirePermission('manage_domains'), async (req, res, next) => {
  try {
    const { deployment_id, hostname } = req.body ?? {};
    if (!isUuid(deployment_id) || typeof hostname !== 'string') {
      sendError(res, 400, 'bad_request', 'deployment_id (UUID) and hostname are required');
      return;
    }
    const pool = getPool();
    const dep = await getDeployment(pool, deployment_id);
    if (!dep) {
      next(new HttpError(404, 'not_found', 'deployment not found'));
      return;
    }
    const domains: DomainEntry[] = Array.isArray(dep.domains) ? dep.domains : [];
    const idx = domains.findIndex((d) => d.hostname.toLowerCase() === hostname.toLowerCase());
    if (idx === -1) {
      next(new HttpError(404, 'not_found', 'hostname not attached to this deployment'));
      return;
    }
    const [removed] = domains.splice(idx, 1);
    const cfg = getCloudflareConfig();
    if (cfg && removed.cf_record_id) {
      await deleteDnsRecord(cfg, removed.cf_record_id);
    }
    await pool.query('UPDATE deployments SET domains = $1::jsonb WHERE id = $2', [
      JSON.stringify(domains),
      deployment_id,
    ]);
    let ingressTaskId: string | null = null;
    if ((removed.ingress ?? 'direct') === 'tunnel') {
      ingressTaskId = await enqueueIngressSync(pool, dep.host_id, req.auth!.id,
        { trigger: 'domain.removed', hostname: removed.hostname });
    }
    await appendEvent(pool, {
      type: 'domain.removed',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      deployment_id,
      host_id: dep.host_id,
      payload: { hostname: removed.hostname, ingress_task_id: ingressTaskId },
    });
    res.json({ removed: removed.hostname });
  } catch (err) {
    next(err);
  }
});
