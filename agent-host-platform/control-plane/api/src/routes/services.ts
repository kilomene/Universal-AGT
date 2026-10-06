import { Router } from 'express';
import type { NextFunction, Request, Response } from 'express';
import { getPool } from '../db/pool';
import { sendError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { requireAgent, requirePermission } from '../middleware/auth';
import { authorizeDeploymentAccess, inList, listAccessibleProjectIds } from '../lib/authz';
import { isUuid, parseLimit } from './_helpers';

export const servicesRouter = Router();

// Service = a deployment in a lifecycle status, presented in a friendly shape.
const SERVICE_STATUSES = ['running', 'failed', 'building', 'starting', 'healthcheck', 'stopped', 'stopping'];

function toServiceView(row: Record<string, any>) {
  return {
    id: row.id,
    project_id: row.project_id,
    project_name: row.project_name,
    host_id: row.host_id,
    host_name: row.host_name,
    version: row.version,
    status: row.status,
    health_status: row.health_status,
    container_ids: row.container_ids,
    ports: row.ports,
    domains: row.domains,
    updated_at: row.updated_at,
  };
}

// GET /v1/services?host_id= — agent, read_status.
servicesRouter.get('/', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const { host_id } = req.query;
    const limit = parseLimit(req.query.limit);
    const conds = [`d.status = ANY($1)`];
    const params: unknown[] = [SERVICE_STATUSES];
    // §10: only services of projects the agent may access.
    const accessibleIds = await listAccessibleProjectIds(getPool(), req.auth!.id);
    if (accessibleIds.length === 0) {
      res.json({ services: [] });
      return;
    }
    conds.push(inList('d.project_id', accessibleIds, params) as string);
    if (typeof host_id === 'string') {
      if (!isUuid(host_id)) {
        sendError(res, 400, 'bad_request', 'host_id must be a UUID');
        return;
      }
      params.push(host_id);
      conds.push(`d.host_id = $${params.length}`);
    }
    params.push(limit);
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT d.*, p.name AS project_name, h.name AS host_name
       FROM deployments d
       JOIN projects p ON p.id = d.project_id
       JOIN hosts h ON h.id = d.host_id
       WHERE ${conds.join(' AND ')}
       ORDER BY d.updated_at DESC LIMIT $${params.length}`,
      params,
    );
    res.json({ services: rows.map(toServiceView) });
  } catch (err) {
    next(err);
  }
});

// Creates a control task (restart|stop|start) targeting the deployment's host.
async function controlTask(req: Request, res: Response, next: NextFunction, taskType: 'restart' | 'stop' | 'start') {
  try {
    const deploymentId = req.params.id;
    if (!isUuid(deploymentId)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    // §10: via deployment -> project -> owner/ACL.
    const deployment = await authorizeDeploymentAccess(pool, req.auth!.id, deploymentId);
    const taskRows = (
      await pool.query(
        `INSERT INTO tasks (created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,$3,'queued',$4) RETURNING *`,
        [req.auth!.id, deployment.host_id, taskType, JSON.stringify({ deployment_id: deploymentId })],
      )
    ).rows;
    const task = taskRows[0];
    await appendEvent(pool, {
      type: 'task.created',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: deployment.host_id,
      payload: { type: taskType, deployment_id: deploymentId },
    });
    logger.info('service control task created', { type: taskType, deployment: deploymentId });
    res.status(201).json({ task });
  } catch (err) {
    next(err);
  }
}

// POST /v1/services/:id/restart — needs restart.
servicesRouter.post('/:id/restart', requireAgent, requirePermission('restart'), (req, res, next) =>
  controlTask(req, res, next, 'restart'),
);

// POST /v1/services/:id/stop — needs stop.
servicesRouter.post('/:id/stop', requireAgent, requirePermission('stop'), (req, res, next) =>
  controlTask(req, res, next, 'stop'),
);

// POST /v1/services/:id/start — needs restart (per PROTOCOL §3.6).
servicesRouter.post('/:id/start', requireAgent, requirePermission('restart'), (req, res, next) =>
  controlTask(req, res, next, 'start'),
);
