import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { requireAgent, requirePermission } from '../middleware/auth';
import { isUuid } from './_helpers';
import { secretsRouter } from './secrets';

export const projectsRouter = Router();

// Nested secrets routes: /v1/projects/:id/secrets
projectsRouter.use('/:id/secrets', secretsRouter);

const RUNTIMES = ['docker', 'docker-compose', 'static'];

// Minimal agent.deploy.json manifest validation (PROTOCOL §4).
function validateConfiguration(configuration: unknown, projectName: string): string | null {
  if (typeof configuration !== 'object' || configuration === null || Array.isArray(configuration)) {
    return 'configuration must be an object';
  }
  const cfg = configuration as Record<string, unknown>;
  if (cfg.name !== undefined && cfg.name !== projectName) {
    return 'configuration.name must match the project name';
  }
  if (cfg.runtime !== undefined && !RUNTIMES.includes(cfg.runtime as string)) {
    return `configuration.runtime must be one of: ${RUNTIMES.join(', ')}`;
  }
  const service = cfg.service as Record<string, unknown> | undefined;
  if (service !== undefined) {
    const port = service.port;
    if (port !== undefined && (!Number.isInteger(port) || (port as number) < 1 || (port as number) > 65535)) {
      return 'configuration.service.port must be 1-65535';
    }
  }
  const resources = cfg.resources as Record<string, unknown> | undefined;
  if (resources !== undefined) {
    if (resources.memory !== undefined && !/^(\d+)(m|g)$/i.test(String(resources.memory))) {
      return "configuration.resources.memory must look like '256m' or '1g'";
    }
    if (resources.cpu !== undefined && !(typeof resources.cpu === 'number' && resources.cpu > 0)) {
      return 'configuration.resources.cpu must be a positive number';
    }
  }
  if (cfg.restart !== undefined && !['no', 'always', 'unless-stopped', 'on-failure'].includes(cfg.restart as string)) {
    return "configuration.restart must be one of: no, always, unless-stopped, on-failure";
  }
  return null;
}

// POST /v1/projects
projectsRouter.post('/', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    const { name, owner, repository, runtime, configuration } = req.body ?? {};
    if (typeof name !== 'string' || !name.trim()) {
      sendError(res, 400, 'bad_request', 'name is required');
      return;
    }
    if (runtime !== undefined && !RUNTIMES.includes(runtime)) {
      sendError(res, 400, 'bad_request', `runtime must be one of: ${RUNTIMES.join(', ')}`);
      return;
    }
    if (configuration !== undefined) {
      const err = validateConfiguration(configuration, name.trim());
      if (err) {
        sendError(res, 422, 'unprocessable', err);
        return;
      }
    }
    const pool = getPool();
    try {
      const { rows } = await pool.query(
        `INSERT INTO projects (name, owner, repository, runtime, configuration)
         VALUES ($1,$2,$3,$4,$5) RETURNING *`,
        [
          name.trim(),
          typeof owner === 'string' ? owner : req.auth!.name,
          typeof repository === 'string' ? repository : null,
          runtime ?? 'docker',
          JSON.stringify(configuration ?? {}),
        ],
      );
      res.status(201).json({ project: rows[0] });
    } catch (err: unknown) {
      if ((err as { code?: string }).code === '23505') {
        sendError(res, 409, 'conflict', `project name '${name.trim()}' already exists`);
        return;
      }
      throw err;
    }
  } catch (err) {
    next(err);
  }
});

// GET /v1/projects
projectsRouter.get('/', requireAgent, requirePermission('read_status'), async (_req, res, next) => {
  try {
    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM projects ORDER BY created_at ASC');
    res.json({ projects: rows });
  } catch (err) {
    next(err);
  }
});

// GET /v1/projects/:id
projectsRouter.get('/:id', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM projects WHERE id = $1', [req.params.id]);
    if (!rows[0]) {
      next(new HttpError(404, 'not_found', 'project not found'));
      return;
    }
    res.json({ project: rows[0] });
  } catch (err) {
    next(err);
  }
});

// PUT /v1/projects/:id — update configuration (validated manifest).
projectsRouter.put('/:id', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM projects WHERE id = $1', [req.params.id]);
    const project = rows[0];
    if (!project) {
      next(new HttpError(404, 'not_found', 'project not found'));
      return;
    }
    const { configuration, repository, runtime } = req.body ?? {};
    if (configuration !== undefined) {
      const err = validateConfiguration(configuration, project.name);
      if (err) {
        sendError(res, 422, 'unprocessable', err);
        return;
      }
    }
    if (runtime !== undefined && !RUNTIMES.includes(runtime)) {
      sendError(res, 400, 'bad_request', `runtime must be one of: ${RUNTIMES.join(', ')}`);
      return;
    }
    const updated = (
      await pool.query(
        `UPDATE projects SET
           configuration = COALESCE($2, configuration),
           repository = COALESCE($3, repository),
           runtime = COALESCE($4, runtime)
         WHERE id = $1 RETURNING *`,
        [
          project.id,
          configuration !== undefined ? JSON.stringify(configuration) : null,
          typeof repository === 'string' ? repository : null,
          typeof runtime === 'string' ? runtime : null,
        ],
      )
    ).rows[0];
    res.json({ project: updated });
  } catch (err) {
    next(err);
  }
});
