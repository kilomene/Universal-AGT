import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { generateHostToken } from '../lib/tokens';
import { provisioningOrDeploy, requireAgent, requireHost, requirePermission } from '../middleware/auth';
import { publicHost } from './_helpers';

export const hostsRouter = Router();

// POST /v1/hosts/register — provisioning token OR agent with deploy permission
// (see provisioningOrDeploy; choice documented in README).
hostsRouter.post('/register', provisioningOrDeploy, async (req, res, next) => {
  try {
    const { name, host_type, capabilities, worker_version } = req.body ?? {};
    if (typeof name !== 'string' || !name.trim()) {
      sendError(res, 400, 'bad_request', 'name is required');
      return;
    }
    if (capabilities !== undefined && !Array.isArray(capabilities)) {
      sendError(res, 400, 'bad_request', 'capabilities must be an array');
      return;
    }
    const pool = getPool();
    const { token, hash } = generateHostToken();
    try {
      const { rows } = await pool.query(
        `INSERT INTO hosts (name, host_type, capabilities, worker_version, token_hash)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING id, name, host_type, status, capabilities, worker_version,
                   cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
                   total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
                   created_at, updated_at`,
        [
          name.trim(),
          typeof host_type === 'string' && host_type ? host_type : 'persistent-linux-host',
          JSON.stringify(capabilities ?? []),
          typeof worker_version === 'string' ? worker_version : null,
          hash,
        ],
      );
      const host = publicHost(rows[0]);
      await appendEvent(pool, {
        type: 'host.registered',
        actor_type: req.auth?.kind ?? 'system',
        actor_id: req.auth?.name ?? null,
        host_id: host.id as string,
        payload: { host_name: host.name },
      });
      logger.info('host registered', { name: host.name });
      res.status(201).json({ host, host_token: token });
    } catch (err: unknown) {
      if ((err as { code?: string }).code === '23505') {
        sendError(res, 409, 'conflict', `host name '${name.trim()}' already exists`);
        return;
      }
      throw err;
    }
  } catch (err) {
    next(err);
  }
});

// POST /v1/hosts/:id/rotate-token — HOST token only, and the token must
// belong to the host in the path. Returns the NEW host token (shown once);
// the old token stops working immediately. Emits host.token_rotated.
// (The host_id in the URL is validated as a UUID by the DB lookup; a
// non-existent id can never match req.auth.id, so it 403s like a mismatch.)
hostsRouter.post('/:id/rotate-token', requireHost, async (req, res, next) => {
  try {
    if (req.params.id !== req.auth!.id) {
      sendError(res, 403, 'forbidden', 'host token does not belong to this host');
      return;
    }
    const pool = getPool();
    const { token, hash } = generateHostToken();
    const { rowCount } = await pool.query(
      `UPDATE hosts SET token_hash = $2, updated_at = now() WHERE id = $1`,
      [req.auth!.id, hash],
    );
    if (!rowCount) {
      next(new HttpError(404, 'not_found', 'host not found'));
      return;
    }
    await appendEvent(pool, {
      type: 'host.token_rotated',
      actor_type: 'host',
      actor_id: req.auth!.name,
      host_id: req.auth!.id,
      payload: {},
    });
    logger.info('host token rotated', { host: req.auth!.name });
    res.json({ host_token: token });
  } catch (err) {
    next(err);
  }
});

// GET /v1/hosts — agent, read_status.
hostsRouter.get('/', requireAgent, requirePermission('read_status'), async (_req, res, next) => {
  try {
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT id, name, host_type, status, capabilities, worker_version,
              cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
              total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
              created_at, updated_at
       FROM hosts ORDER BY created_at ASC`,
    );
    res.json({ hosts: rows.map(publicHost) });
  } catch (err) {
    next(err);
  }
});

// GET /v1/hosts/:id — agent, read_status.
hostsRouter.get('/:id', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT id, name, host_type, status, capabilities, worker_version,
              cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
              total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
              created_at, updated_at
       FROM hosts WHERE id = $1`,
      [req.params.id],
    );
    if (!rows[0]) {
      next(new HttpError(404, 'not_found', 'host not found'));
      return;
    }
    res.json({ host: publicHost(rows[0]) });
  } catch (err) {
    next(err);
  }
});
