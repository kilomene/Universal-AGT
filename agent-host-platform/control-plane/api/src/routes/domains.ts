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
}

async function getDeployment(pool: ReturnType<typeof getPool>, id: string) {
  const { rows } = await pool.query(
    'SELECT id, project_id, host_id, domains FROM deployments WHERE id = $1',
    [id],
  );
  return rows[0] as { id: string; project_id: string; host_id: string; domains: DomainEntry[] } | undefined;
}

// POST /v1/domains {deployment_id, hostname}
// Records the domain on the deployment; when Cloudflare is configured,
// creates a CNAME hostname -> PUBLIC_INGRESS_HOSTNAME (proxied).
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
    };
    const cfg = getCloudflareConfig();
    if (cfg) {
      try {
        entry.cf_record_id = await ensureCnameRecord(cfg, hostname);
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
    await appendEvent(pool, {
      type: cfg ? 'domain.added' : 'domain.requested',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      deployment_id,
      host_id: dep.host_id,
      payload: { hostname, status: entry.status, dns_managed: cfg !== null },
    });
    res.status(201).json({ domain: entry });
  } catch (err) {
    next(err);
  }
});

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
    await appendEvent(pool, {
      type: 'domain.removed',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      deployment_id,
      host_id: dep.host_id,
      payload: { hostname: removed.hostname },
    });
    res.json({ removed: removed.hostname });
  } catch (err) {
    next(err);
  }
});
