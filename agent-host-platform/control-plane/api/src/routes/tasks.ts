import { Router } from 'express';
import type { NextFunction, Request, Response } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { decideIdempotency, findTaskByIdempotencyKey, taskIdempotencyBody } from '../lib/idempotency';
import { canTransitionTask, isTaskStatus } from '../lib/stateMachine';
import { requireAgent, requirePermission } from '../middleware/auth';
import { decodeCursor, encodeCursor, isUuid, parseLimit } from './_helpers';

export const tasksRouter = Router();

// PROTOCOL §3.2 — the 17 allowed task types.
export const TASK_TYPES = [
  'deploy',
  'restart',
  'stop',
  'start',
  'remove',
  'rollback',
  'logs',
  'status',
  'healthcheck',
  'build',
  'docker-build',
  'docker-run',
  'docker-compose',
  'environment-update',
  'artifact-download',
  'artifact-upload',
  'system-info',
] as const;

function permissionForTaskType(type: string): string {
  // Deploying new code is the privileged action; everything else is at
  // minimum a status read. (restart/stop/remove gates live on services.)
  return type === 'deploy' ? 'deploy' : 'read_status';
}

async function getTask(pool: ReturnType<typeof getPool>, id: string) {
  const { rows } = await pool.query('SELECT * FROM tasks WHERE id = $1', [id]);
  return rows[0] ?? null;
}

// POST /v1/tasks
tasksRouter.post('/', requireAgent, async (req, res, next) => {
  try {
    const { type, payload, idempotency_key, priority, host_id, mode } = req.body ?? {};

    if (typeof type !== 'string' || !(TASK_TYPES as readonly string[]).includes(type)) {
      sendError(res, 400, 'bad_request', `type must be one of: ${TASK_TYPES.join(', ')}`);
      return;
    }
    const auth = req.auth!;
    const needed = permissionForTaskType(type);
    if (auth.permissions?.[needed] !== true) {
      sendError(res, 403, 'forbidden', `missing permission: ${needed}`);
      return;
    }
    if (payload !== undefined && (typeof payload !== 'object' || payload === null || Array.isArray(payload))) {
      sendError(res, 400, 'bad_request', 'payload must be an object');
      return;
    }
    if (idempotency_key !== undefined && typeof idempotency_key !== 'string') {
      sendError(res, 400, 'bad_request', 'idempotency_key must be a string');
      return;
    }
    const prio = priority === undefined ? 0 : Number(priority);
    if (!Number.isInteger(prio)) {
      sendError(res, 400, 'bad_request', 'priority must be an integer');
      return;
    }
    const taskMode = mode === undefined ? 'automatic' : mode;
    if (taskMode !== 'automatic' && taskMode !== 'manual') {
      sendError(res, 400, 'bad_request', "mode must be 'automatic' or 'manual'");
      return;
    }
    const pool = getPool();
    if (host_id !== undefined && host_id !== null) {
      if (!isUuid(host_id)) {
        sendError(res, 400, 'bad_request', 'host_id must be a UUID');
        return;
      }
      const { rows } = await pool.query('SELECT id FROM hosts WHERE id = $1', [host_id]);
      if (!rows[0]) {
        sendError(res, 404, 'not_found', 'host not found');
        return;
      }
    }

    // Idempotency (PROTOCOL §2): same key + equal body -> replay; same key +
    // different body -> 409 conflict.
    if (typeof idempotency_key === 'string' && idempotency_key) {
      const existing = await findTaskByIdempotencyKey(pool, idempotency_key);
      if (existing) {
        const decision = decideIdempotency(
          { body: taskIdempotencyBody(existing) },
          taskIdempotencyBody({ type, payload }),
        );
        if (decision === 'replay') {
          const task = await getTask(pool, existing.id);
          res.status(200).json({ task, idempotent_replay: true });
          return;
        }
        sendError(res, 409, 'conflict', 'idempotency key already used with a different payload');
        return;
      }
    }

    const status = taskMode === 'manual' ? 'awaiting_approval' : 'queued';
    const { rows } = await pool.query(
      `INSERT INTO tasks (idempotency_key, created_by, assigned_to, type, status, priority, payload)
       VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *`,
      [
        typeof idempotency_key === 'string' && idempotency_key ? idempotency_key : null,
        auth.id,
        host_id ?? null,
        type,
        status,
        prio,
        JSON.stringify(payload ?? {}),
      ],
    );
    const task = rows[0];
    await appendEvent(pool, {
      type: 'task.created',
      actor_type: 'agent',
      actor_id: auth.name,
      task_id: task.id,
      host_id: task.assigned_to,
      payload: { type, mode: taskMode, idempotency_key: idempotency_key ?? null },
    });
    res.status(201).json({ task });
  } catch (err) {
    next(err);
  }
});

// GET /v1/tasks?status=&type=&host_id=&limit=&cursor=
tasksRouter.get('/', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const { status, type, host_id } = req.query;
    const limit = parseLimit(req.query.limit);
    const cursor = decodeCursor(req.query.cursor);
    if (req.query.cursor && !cursor) {
      sendError(res, 400, 'bad_request', 'invalid cursor');
      return;
    }
    const conds: string[] = [];
    const params: unknown[] = [];
    if (typeof status === 'string') {
      if (!isTaskStatus(status)) {
        sendError(res, 400, 'bad_request', `unknown status: ${status}`);
        return;
      }
      params.push(status);
      conds.push(`status = $${params.length}`);
    }
    if (typeof type === 'string') {
      params.push(type);
      conds.push(`type = $${params.length}`);
    }
    if (typeof host_id === 'string') {
      if (!isUuid(host_id)) {
        sendError(res, 400, 'bad_request', 'host_id must be a UUID');
        return;
      }
      params.push(host_id);
      conds.push(`assigned_to = $${params.length}`);
    }
    if (cursor) {
      params.push(cursor.created_at, cursor.id);
      conds.push(`(created_at, id) < ($${params.length - 1}, $${params.length})`);
    }
    const where = conds.length ? 'WHERE ' + conds.join(' AND ') : '';
    params.push(limit + 1);
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT * FROM tasks ${where} ORDER BY created_at DESC, id DESC LIMIT $${params.length}`,
      params,
    );
    const hasMore = rows.length > limit;
    const page = hasMore ? rows.slice(0, limit) : rows;
    const nextCursor =
      hasMore && page.length ? encodeCursor(page[page.length - 1].created_at, page[page.length - 1].id) : null;
    res.json({ tasks: page, next_cursor: nextCursor });
  } catch (err) {
    next(err);
  }
});

// GET /v1/tasks/:id
tasksRouter.get('/:id', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const task = await getTask(getPool(), req.params.id);
    if (!task) {
      next(new HttpError(404, 'not_found', 'task not found'));
      return;
    }
    res.json({ task });
  } catch (err) {
    next(err);
  }
});

// POST /v1/tasks/:id/cancel — uses the state machine; terminal states -> 409.
tasksRouter.post('/:id/cancel', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const task = await getTask(pool, req.params.id);
    if (!task) {
      next(new HttpError(404, 'not_found', 'task not found'));
      return;
    }
    if (!canTransitionTask(task.status, 'cancelled')) {
      sendError(res, 409, 'conflict', `task is ${task.status}; cannot cancel from a terminal state`);
      return;
    }
    const { rows } = await pool.query(
      `UPDATE tasks SET status = 'cancelled', completed_at = now() WHERE id = $1 RETURNING *`,
      [task.id],
    );
    await appendEvent(pool, {
      type: 'task.cancelled',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: task.assigned_to,
      payload: { previous_status: task.status },
    });
    res.json({ task: rows[0] });
  } catch (err) {
    next(err);
  }
});

async function decideApproval(req: Request, res: Response, next: NextFunction, approve: boolean) {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    const task = await getTask(pool, req.params.id);
    if (!task) {
      next(new HttpError(404, 'not_found', 'task not found'));
      return;
    }
    const target = approve ? 'queued' : 'cancelled';
    if (task.status !== 'awaiting_approval' || !canTransitionTask(task.status, target)) {
      sendError(res, 409, 'conflict', `task is ${task.status}; only awaiting_approval tasks can be ${approve ? 'approved' : 'rejected'}`);
      return;
    }
    const { rows } = await pool.query(
      `UPDATE tasks SET status = $1, completed_at = CASE WHEN $1 = 'cancelled' THEN now() ELSE NULL END
       WHERE id = $2 RETURNING *`,
      [target, task.id],
    );
    const updated = rows[0];
    const deploymentId = (task.payload as Record<string, unknown> | null)?.deployment_id;
    if (approve && typeof deploymentId === 'string' && isUuid(deploymentId)) {
      await pool.query(`UPDATE deployments SET status = 'approved' WHERE id = $1`, [deploymentId]);
      await appendEvent(pool, {
        type: 'deployment.approved',
        actor_type: 'agent',
        actor_id: req.auth!.name,
        task_id: task.id,
        deployment_id: deploymentId,
        host_id: task.assigned_to,
        payload: {},
      });
    }
    await appendEvent(pool, {
      type: approve ? 'task.approved' : 'task.rejected',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: task.assigned_to,
      payload: {},
    });
    res.json({ task: updated });
  } catch (err) {
    next(err);
  }
}

// POST /v1/tasks/:id/approve — needs approve_deployments; awaiting_approval -> queued.
tasksRouter.post('/:id/approve', requireAgent, requirePermission('approve_deployments'), (req, res, next) =>
  decideApproval(req, res, next, true),
);

// POST /v1/tasks/:id/reject — needs approve_deployments; awaiting_approval -> cancelled.
tasksRouter.post('/:id/reject', requireAgent, requirePermission('approve_deployments'), (req, res, next) =>
  decideApproval(req, res, next, false),
);
