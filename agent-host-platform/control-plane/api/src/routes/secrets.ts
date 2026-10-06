import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { decryptSecret, encryptSecret } from '../lib/secrets';
import { requireAgent, requirePermission } from '../middleware/auth';
import { authorizeProjectAccess } from '../lib/authz';
import { isUuid } from './_helpers';

export const secretsRouter = Router({ mergeParams: true });

// All three routes require the manage_secrets permission.
// Plaintext values are encrypted before INSERT and never returned by the API.

// POST /v1/projects/:id/secrets {name, value} — upsert, encrypted at rest.
secretsRouter.post('/', requireAgent, requirePermission('manage_secrets'), async (req, res, next) => {
  try {
    const projectId = req.params.id;
    if (!isUuid(projectId)) {
      sendError(res, 400, 'bad_request', 'project id must be a UUID');
      return;
    }
    const { name, value } = req.body ?? {};
    if (typeof name !== 'string' || !name.trim() || name.length > 128) {
      sendError(res, 400, 'bad_request', 'name is required (max 128 chars)');
      return;
    }
    if (typeof value !== 'string' || !value) {
      sendError(res, 400, 'bad_request', 'value is required');
      return;
    }
    const pool = getPool();
    // §10: via secret -> project -> project ACL (also 404s on a missing project).
    await authorizeProjectAccess(pool, req.auth!.id, projectId);
    let encrypted: Buffer;
    try {
      encrypted = encryptSecret(value);
    } catch (err) {
      next(new HttpError(500, 'internal', `secret encryption failed: ${String(err)}`));
      return;
    }
    const { rows } = await pool.query(
      `INSERT INTO secrets (project_id, name, encrypted_value)
       VALUES ($1,$2,$3)
       ON CONFLICT (project_id, name) DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value
       RETURNING id, project_id, name, created_at, updated_at`,
      [projectId, name.trim(), encrypted],
    );
    // W11: secret rotation/change emits `secret.changed` — the payload
    // carries the name only; the value is never written to the journal.
    await appendEvent(pool, {
      type: 'secret.changed',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      payload: { project_id: projectId, name: name.trim() },
    });
    logger.info('project secret stored', { project: projectId, name: name.trim() });
    res.status(201).json({ secret: rows[0] });
  } catch (err) {
    next(err);
  }
});

// GET /v1/projects/:id/secrets — names only, never values.
secretsRouter.get('/', requireAgent, requirePermission('manage_secrets'), async (req, res, next) => {
  try {
    const projectId = req.params.id;
    if (!isUuid(projectId)) {
      sendError(res, 400, 'bad_request', 'project id must be a UUID');
      return;
    }
    const pool = getPool();
    // §10: via secret -> project -> project ACL (also 404s on a missing project).
    await authorizeProjectAccess(pool, req.auth!.id, projectId);
    const { rows } = await pool.query(
      'SELECT name, created_at, updated_at FROM secrets WHERE project_id = $1 ORDER BY name ASC',
      [projectId],
    );
    res.json({ secrets: rows });
  } catch (err) {
    next(err);
  }
});

// DELETE /v1/projects/:id/secrets/:name
secretsRouter.delete('/:name', requireAgent, requirePermission('manage_secrets'), async (req, res, next) => {
  try {
    const projectId = req.params.id;
    if (!isUuid(projectId)) {
      sendError(res, 400, 'bad_request', 'project id must be a UUID');
      return;
    }
    const pool = getPool();
    // §10: via secret -> project -> project ACL (also 404s on a missing project).
    await authorizeProjectAccess(pool, req.auth!.id, projectId);
    const { rowCount } = await pool.query('DELETE FROM secrets WHERE project_id = $1 AND name = $2', [
      projectId,
      req.params.name,
    ]);
    if (!rowCount) {
      next(new HttpError(404, 'not_found', 'secret not found'));
      return;
    }
    // W11: deletion emits `secret.deleted` — name only, never the value.
    await appendEvent(pool, {
      type: 'secret.deleted',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      payload: { project_id: projectId, name: req.params.name },
    });
    res.status(204).end();
  } catch (err) {
    next(err);
  }
});

// Internal helper for the worker: decrypted values for a deployment's project.
// Used server-side only — never exposed over HTTP.
export async function getDecryptedProjectSecrets(
  pool: ReturnType<typeof getPool>,
  projectId: string,
): Promise<Record<string, string>> {
  const { rows } = await pool.query('SELECT name, encrypted_value FROM secrets WHERE project_id = $1', [projectId]);
  const out: Record<string, string> = {};
  for (const row of rows) {
    out[row.name as string] = decryptSecret(row.encrypted_value as Buffer);
  }
  return out;
}
