import { Router, type Request } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { sha256Hex, verifyToken } from '../lib/tokens';
import { provisioningTokenFrom, requireAgent, requireOperator, requirePermission } from '../middleware/auth';
import {
  authorizeProjectAccess,
  inList,
  listAccessibleProjectIds,
} from '../lib/authz';
import { isUuid } from './_helpers';
import { secretsRouter } from './secrets';
import { DEPLOYMENT_MANIFEST_VERSION } from '../lib/versions';

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
  // §61: the deployment manifest schema carries a meaningful version. It is
  // optional (older manifests predate it) but when present it must be a
  // dotted version, and its major version must not be newer than the schema
  // this control plane implements — a newer major means a manifest shape we
  // cannot understand, so it fails fast here instead of mid-deploy.
  if (cfg.manifestVersion !== undefined) {
    if (typeof cfg.manifestVersion !== 'string' || !/^\d+\.\d+\.\d+$/.test(cfg.manifestVersion)) {
      return 'configuration.manifestVersion must look like "1.0.0"';
    }
    const manifestMajor = parseInt(cfg.manifestVersion.split('.')[0] as string, 10);
    const supportedMajor = parseInt(DEPLOYMENT_MANIFEST_VERSION.split('.')[0] as string, 10);
    if (manifestMajor > supportedMajor) {
      return (
        `configuration.manifestVersion major version ${manifestMajor} is not supported ` +
        `(this control plane implements manifest schema ${DEPLOYMENT_MANIFEST_VERSION})`
      );
    }
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
      // §10: the creating agent owns the project it creates. The legacy
      // `owner` name column keeps its backwards-compatible value; authority
      // lives in owner_agent_id.
      const { rows } = await pool.query(
        `INSERT INTO projects (name, owner, owner_agent_id, repository, runtime, configuration)
         VALUES ($1,$2,$3,$4,$5,$6) RETURNING *`,
        [
          name.trim(),
          typeof owner === 'string' ? owner : req.auth!.name,
          req.auth!.id,
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

// GET /v1/projects — §10: only projects the agent may access.
projectsRouter.get('/', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const pool = getPool();
    const ids = await listAccessibleProjectIds(pool, req.auth!.id);
    if (ids.length === 0) {
      res.json({ projects: [] });
      return;
    }
    const params: unknown[] = [];
    const cond = inList('id', ids, params);
    const { rows } = await pool.query(
      `SELECT * FROM projects WHERE ${cond} ORDER BY created_at ASC`,
      params,
    );
    res.json({ projects: rows });
  } catch (err) {
    next(err);
  }
});

// GET /v1/projects/:id — §10: project ACL.
projectsRouter.get('/:id', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    await authorizeProjectAccess(pool, req.auth!.id, req.params.id);
    const { rows } = await pool.query('SELECT * FROM projects WHERE id = $1', [req.params.id]);
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
    await authorizeProjectAccess(pool, req.auth!.id, req.params.id);
    const { rows } = await pool.query('SELECT * FROM projects WHERE id = $1', [req.params.id]);
    const project = rows[0];
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

// ---------------------------------------------------------------------------
// Project membership + owner management (§10 ACL administration, §11
// administrative recovery path).
//
// Membership may be managed by the project owner or by an operator.
// The owner itself may only be (re)assigned by an operator — this is the
// recovery path for legacy projects whose owner name could not be resolved
// (migration 011 records those in migration_reports).
// ---------------------------------------------------------------------------

// Operator check for membership management: the project owner, or an
// operator (provisioning token, or an agent carrying the admin permission).
async function requireOwnerOrOperator(
  req: Request,
  pool: ReturnType<typeof getPool>,
  projectId: string,
): Promise<{ id: string; owner_agent_id: string | null }> {
  const proj = await authorizeProjectAccess(pool, req.auth!.id, projectId);
  if (proj.owner_agent_id !== null && proj.owner_agent_id === req.auth!.id) return proj;
  const expected = process.env.UAHT_PROVISIONING_TOKEN;
  const presented = provisioningTokenFrom(req);
  if (expected && presented && verifyToken(presented, sha256Hex(expected))) return proj;
  if (req.auth?.kind === 'agent' && req.auth.permissions?.['admin'] === true) return proj;
  throw new HttpError(403, 'forbidden', 'only the project owner or an operator may manage membership');
}

// GET /v1/projects/:id/members — any agent with project access.
projectsRouter.get('/:id/members', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const proj = await authorizeProjectAccess(pool, req.auth!.id, req.params.id);
    const { rows } = await pool.query(
      `SELECT a.id, a.name, a.type, a.status, m.created_at AS granted_at
       FROM project_members m JOIN agents a ON a.id = m.agent_id
       WHERE m.project_id = $1 ORDER BY a.name ASC`,
      [req.params.id],
    );
    res.json({ project_id: req.params.id, owner_agent_id: proj.owner_agent_id, members: rows });
  } catch (err) {
    next(err);
  }
});

// POST /v1/projects/:id/members {agent_id} — owner or operator.
projectsRouter.post('/:id/members', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const { agent_id } = req.body ?? {};
    if (!isUuid(agent_id)) {
      sendError(res, 400, 'bad_request', 'agent_id (UUID) is required');
      return;
    }
    const pool = getPool();
    await requireOwnerOrOperator(req, pool, req.params.id);
    const agent = (await pool.query('SELECT id, name FROM agents WHERE id = $1', [agent_id])).rows[0];
    if (!agent) {
      next(new HttpError(404, 'not_found', 'agent not found'));
      return;
    }
    await pool.query(
      `INSERT INTO project_members (project_id, agent_id) VALUES ($1,$2)
       ON CONFLICT (project_id, agent_id) DO NOTHING`,
      [req.params.id, agent_id],
    );
    await appendEvent(pool, {
      type: 'project.member_added',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      payload: { project_id: req.params.id, agent_id, agent_name: agent.name },
    });
    logger.info('project member added', { project: req.params.id, agent: agent.name });
    res.status(201).json({ project_id: req.params.id, agent_id, agent_name: agent.name });
  } catch (err) {
    next(err);
  }
});

// DELETE /v1/projects/:id/members/:agent_id — owner or operator.
projectsRouter.delete('/:id/members/:agent_id', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id) || !isUuid(req.params.agent_id)) {
      sendError(res, 400, 'bad_request', 'project id and agent_id must be UUIDs');
      return;
    }
    const pool = getPool();
    await requireOwnerOrOperator(req, pool, req.params.id);
    const { rowCount } = await pool.query(
      'DELETE FROM project_members WHERE project_id = $1 AND agent_id = $2',
      [req.params.id, req.params.agent_id],
    );
    if (!rowCount) {
      next(new HttpError(404, 'not_found', 'membership not found'));
      return;
    }
    await appendEvent(pool, {
      type: 'project.member_removed',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      payload: { project_id: req.params.id, agent_id: req.params.agent_id },
    });
    logger.info('project member removed', { project: req.params.id, agent: req.params.agent_id });
    res.status(204).end();
  } catch (err) {
    next(err);
  }
});

// POST /v1/projects/:id/owner {agent_id} — OPERATOR ONLY (§11 recovery path).
// Assigns (or reassigns) the explicit project owner. The legacy `owner` name
// column follows the agent's name for backwards compatibility.
projectsRouter.post('/:id/owner', requireOperator, async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const { agent_id } = req.body ?? {};
    if (!isUuid(agent_id)) {
      sendError(res, 400, 'bad_request', 'agent_id (UUID) is required');
      return;
    }
    const pool = getPool();
    const project = (await pool.query('SELECT id, owner_agent_id FROM projects WHERE id = $1', [req.params.id])).rows[0];
    if (!project) {
      next(new HttpError(404, 'not_found', 'project not found'));
      return;
    }
    const agent = (await pool.query('SELECT id, name FROM agents WHERE id = $1', [agent_id])).rows[0];
    if (!agent) {
      next(new HttpError(404, 'not_found', 'agent not found'));
      return;
    }
    const updated = (
      await pool.query(
        'UPDATE projects SET owner = $2, owner_agent_id = $3 WHERE id = $1 RETURNING *',
        [req.params.id, agent.name, agent.id],
      )
    ).rows[0];
    await appendEvent(pool, {
      type: 'project.owner_changed',
      actor_type: req.auth?.kind ?? 'operator',
      actor_id: req.auth?.name ?? null,
      payload: {
        project_id: req.params.id,
        previous_owner_agent_id: project.owner_agent_id,
        owner_agent_id: agent.id,
        owner_name: agent.name,
      },
    });
    logger.info('project owner assigned', { project: req.params.id, owner: agent.name });
    res.json({ project: updated });
  } catch (err) {
    next(err);
  }
});
