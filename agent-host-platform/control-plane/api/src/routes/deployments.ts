import { randomUUID } from 'crypto';
import { Router } from 'express';
import type { Pool } from 'pg';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { decideIdempotency, deploymentIdempotencyBody } from '../lib/idempotency';
import { logger } from '../lib/log';
import { requireAgent, requirePermission } from '../middleware/auth';
import { isUuid, parseLimit } from './_helpers';

export const deploymentsRouter = Router();

const MODES = ['automatic', 'manual'] as const;

async function latestTaskForDeployment(pool: Pool, deployment: Record<string, any>) {
  const { rows } = await pool.query(
    `SELECT * FROM tasks
     WHERE id = $1 OR payload->>'deployment_id' = $2
     ORDER BY created_at DESC LIMIT 1`,
    [deployment.task_id, deployment.id],
  );
  return rows[0] ?? null;
}

// POST /v1/deployments — creates a deployment row (status requested) AND a
// type=deploy task whose payload includes deployment_id. The idempotency_key
// dedupes across BOTH rows.
deploymentsRouter.post('/', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    const { project_id, host_id, version, artifact_id, mode, idempotency_key } = req.body ?? {};
    if (!isUuid(project_id)) {
      sendError(res, 400, 'bad_request', 'project_id (UUID) is required');
      return;
    }
    if (host_id !== undefined && host_id !== null && !isUuid(host_id)) {
      sendError(res, 400, 'bad_request', 'host_id must be a UUID');
      return;
    }
    if (typeof version !== 'string' || !version) {
      sendError(res, 400, 'bad_request', 'version is required');
      return;
    }
    if (artifact_id !== undefined && artifact_id !== null && !isUuid(artifact_id)) {
      sendError(res, 400, 'bad_request', 'artifact_id must be a UUID');
      return;
    }
    const deployMode = mode === undefined ? 'automatic' : mode;
    if (!MODES.includes(deployMode)) {
      sendError(res, 400, 'bad_request', "mode must be 'automatic' or 'manual'");
      return;
    }
    if (idempotency_key !== undefined && typeof idempotency_key !== 'string') {
      sendError(res, 400, 'bad_request', 'idempotency_key must be a string');
      return;
    }

    const pool = getPool();
    const project = await pool.query('SELECT id FROM projects WHERE id = $1', [project_id]);
    if (!project.rows[0]) {
      sendError(res, 404, 'not_found', 'project not found');
      return;
    }
    if (host_id) {
      const host = await pool.query('SELECT id FROM hosts WHERE id = $1', [host_id]);
      if (!host.rows[0]) {
        sendError(res, 404, 'not_found', 'host not found');
        return;
      }
    }
    if (artifact_id) {
      const art = await pool.query('SELECT id, project_id, status FROM artifacts WHERE id = $1', [artifact_id]);
      if (!art.rows[0]) {
        sendError(res, 404, 'not_found', 'artifact not found');
        return;
      }
      if (art.rows[0].project_id !== project_id) {
        sendError(res, 422, 'unprocessable', 'artifact does not belong to this project');
        return;
      }
      if (art.rows[0].status !== 'ready') {
        sendError(res, 422, 'unprocessable', `artifact is ${art.rows[0].status}; only ready artifacts can be deployed`);
        return;
      }
    }

    const incomingBody = deploymentIdempotencyBody({ project_id, host_id, version, artifact_id, mode: deployMode });

    if (typeof idempotency_key === 'string' && idempotency_key) {
      const { rows } = await pool.query('SELECT * FROM deployments WHERE idempotency_key = $1', [idempotency_key]);
      const existing = rows[0];
      if (existing) {
        const decision = decideIdempotency(
          { body: deploymentIdempotencyBody(existing) },
          incomingBody,
        );
        if (decision === 'replay') {
          const task = await latestTaskForDeployment(pool, existing);
          res.status(200).json({ deployment: existing, task, idempotent_replay: true });
          return;
        }
        sendError(res, 409, 'conflict', 'idempotency key already used with different deployment parameters');
        return;
      }
    }

    const deploymentId = randomUUID();
    const taskStatus = deployMode === 'manual' ? 'awaiting_approval' : 'queued';
    const taskPayload = {
      project_id,
      host_id: host_id ?? null,
      version,
      artifact_id: artifact_id ?? null,
      deployment_id: deploymentId,
    };

    const client = await pool.connect();
    let deployment: Record<string, any>;
    let task: Record<string, any>;
    try {
      await client.query('BEGIN');
      const depRows = await client.query(
        `INSERT INTO deployments (id, idempotency_key, project_id, host_id, version, status)
         VALUES ($1,$2,$3,$4,$5,'requested') RETURNING *`,
        [
          deploymentId,
          typeof idempotency_key === 'string' && idempotency_key ? idempotency_key : null,
          project_id,
          host_id ?? null,
          version,
        ],
      );
      deployment = depRows.rows[0];
      const taskRows = await client.query(
        `INSERT INTO tasks (idempotency_key, created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,$3,'deploy',$4,$5) RETURNING *`,
        [
          typeof idempotency_key === 'string' && idempotency_key ? `deploy:${idempotency_key}` : null,
          req.auth!.id,
          host_id ?? null,
          taskStatus,
          JSON.stringify(taskPayload),
        ],
      );
      task = taskRows.rows[0];
      await client.query('UPDATE deployments SET task_id = $2 WHERE id = $1', [deploymentId, task.id]);
      deployment.task_id = task.id;
      await client.query('COMMIT');
    } catch (err) {
      await client.query('ROLLBACK');
      if ((err as { code?: string }).code === '23505') {
        sendError(res, 409, 'conflict', 'a deployment of this project+host+version already exists');
        return;
      }
      throw err;
    } finally {
      client.release();
    }

    await appendEvent(pool, {
      type: 'deployment.requested',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      deployment_id: deploymentId,
      host_id: host_id ?? null,
      payload: { version, mode: deployMode },
    });
    await appendEvent(pool, {
      type: 'task.created',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: host_id ?? null,
      payload: { type: 'deploy', mode: deployMode },
    });
    logger.info('deployment requested', { deployment: deploymentId, version });
    res.status(201).json({ deployment, task });
  } catch (err) {
    next(err);
  }
});

// GET /v1/deployments?project_id=&host_id=&status=
deploymentsRouter.get('/', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const { project_id, host_id, status } = req.query;
    const limit = parseLimit(req.query.limit);
    const conds: string[] = [];
    const params: unknown[] = [];
    if (typeof project_id === 'string') {
      if (!isUuid(project_id)) { sendError(res, 400, 'bad_request', 'project_id must be a UUID'); return; }
      params.push(project_id);
      conds.push(`project_id = $${params.length}`);
    }
    if (typeof host_id === 'string') {
      if (!isUuid(host_id)) { sendError(res, 400, 'bad_request', 'host_id must be a UUID'); return; }
      params.push(host_id);
      conds.push(`host_id = $${params.length}`);
    }
    if (typeof status === 'string') {
      params.push(status);
      conds.push(`status = $${params.length}`);
    }
    const where = conds.length ? 'WHERE ' + conds.join(' AND ') : '';
    params.push(limit);
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT * FROM deployments ${where} ORDER BY created_at DESC LIMIT $${params.length}`,
      params,
    );
    res.json({ deployments: rows });
  } catch (err) {
    next(err);
  }
});

// GET /v1/deployments/:id — includes the latest task.
deploymentsRouter.get('/:id', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [req.params.id]);
    const deployment = rows[0];
    if (!deployment) {
      next(new HttpError(404, 'not_found', 'deployment not found'));
      return;
    }
    const task = await latestTaskForDeployment(pool, deployment);
    res.json({ deployment, task });
  } catch (err) {
    next(err);
  }
});

// POST /v1/deployments/:id/rollback — needs deploy; deploys the previous
// healthy version of the same project+host as a new deployment row linked
// via rollback_of. The worker's completion report emits deployment.rolled_back.
deploymentsRouter.post('/:id/rollback', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [req.params.id]);
    const current = rows[0];
    if (!current) {
      next(new HttpError(404, 'not_found', 'deployment not found'));
      return;
    }
    const prev = (
      await pool.query(
        `SELECT * FROM deployments
         WHERE project_id = $1 AND host_id = $2 AND id <> $3
           AND status = 'running' AND health_status = 'healthy'
         ORDER BY created_at DESC LIMIT 1`,
        [current.project_id, current.host_id, current.id],
      )
    ).rows[0];
    if (!prev) {
      sendError(res, 409, 'conflict', 'no previous healthy deployment of this project+host to roll back to');
      return;
    }

    const artForPrev = (
      await pool.query(
        `SELECT id FROM artifacts WHERE project_id = $1 AND version = $2 ORDER BY created_at DESC LIMIT 1`,
        [current.project_id, prev.version],
      )
    ).rows[0];

    const deploymentId = randomUUID();
    const taskPayload = {
      project_id: current.project_id,
      host_id: current.host_id,
      version: prev.version,
      artifact_id: artForPrev ? artForPrev.id : null,
      deployment_id: deploymentId,
      rollback: true,
      rollback_of: current.id,
      rollback_to_deployment_id: prev.id,
    };
    const client = await pool.connect();
    let deployment: Record<string, any>;
    let task: Record<string, any>;
    try {
      await client.query('BEGIN');
      const depRows = await client.query(
        `INSERT INTO deployments (id, idempotency_key, project_id, host_id, version, status, rollback_of)
         VALUES ($1,$2,$3,$4,$5,'requested',$6) RETURNING *`,
        [deploymentId, `rollback:${randomUUID()}`, current.project_id, current.host_id, prev.version, current.id],
      );
      deployment = depRows.rows[0];
      const taskRows = await client.query(
        `INSERT INTO tasks (created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,'deploy','queued',$3) RETURNING *`,
        [req.auth!.id, current.host_id, JSON.stringify(taskPayload)],
      );
      task = taskRows.rows[0];
      await client.query('UPDATE deployments SET task_id = $2 WHERE id = $1', [deploymentId, task.id]);
      deployment.task_id = task.id;
      await client.query('COMMIT');
    } catch (err) {
      await client.query('ROLLBACK');
      throw err;
    } finally {
      client.release();
    }

    await appendEvent(pool, {
      type: 'deployment.requested',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      deployment_id: deploymentId,
      host_id: current.host_id,
      payload: { version: prev.version, rollback: true, rollback_of: current.id },
    });
    await appendEvent(pool, {
      type: 'task.created',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: current.host_id,
      payload: { type: 'deploy', rollback: true },
    });
    logger.info('rollback deployment created', { deployment: deploymentId, from: current.id, to: prev.id });
    res.status(201).json({ deployment, task });
  } catch (err) {
    next(err);
  }
});
