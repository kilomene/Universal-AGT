"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TASK_TYPES = exports.tasksRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const idempotency_1 = require("../lib/idempotency");
const stateMachine_1 = require("../lib/stateMachine");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.tasksRouter = (0, express_1.Router)();
// PROTOCOL §3.2 — the 17 allowed task types.
exports.TASK_TYPES = [
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
];
function permissionForTaskType(type) {
    // Deploying new code is the privileged action; everything else is at
    // minimum a status read. (restart/stop/remove gates live on services.)
    return type === 'deploy' ? 'deploy' : 'read_status';
}
async function getTask(pool, id) {
    const { rows } = await pool.query('SELECT * FROM tasks WHERE id = $1', [id]);
    return rows[0] ?? null;
}
// POST /v1/tasks
exports.tasksRouter.post('/', auth_1.requireAgent, async (req, res, next) => {
    try {
        const { type, payload, idempotency_key, priority, host_id, mode } = req.body ?? {};
        if (typeof type !== 'string' || !exports.TASK_TYPES.includes(type)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', `type must be one of: ${exports.TASK_TYPES.join(', ')}`);
            return;
        }
        const auth = req.auth;
        const needed = permissionForTaskType(type);
        if (auth.permissions?.[needed] !== true) {
            (0, errors_1.sendError)(res, 403, 'forbidden', `missing permission: ${needed}`);
            return;
        }
        if (payload !== undefined && (typeof payload !== 'object' || payload === null || Array.isArray(payload))) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'payload must be an object');
            return;
        }
        if (idempotency_key !== undefined && typeof idempotency_key !== 'string') {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'idempotency_key must be a string');
            return;
        }
        const prio = priority === undefined ? 0 : Number(priority);
        if (!Number.isInteger(prio)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'priority must be an integer');
            return;
        }
        const taskMode = mode === undefined ? 'automatic' : mode;
        if (taskMode !== 'automatic' && taskMode !== 'manual') {
            (0, errors_1.sendError)(res, 400, 'bad_request', "mode must be 'automatic' or 'manual'");
            return;
        }
        const pool = (0, pool_1.getPool)();
        if (host_id !== undefined && host_id !== null) {
            if (!(0, _helpers_1.isUuid)(host_id)) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'host_id must be a UUID');
                return;
            }
            const { rows } = await pool.query('SELECT id FROM hosts WHERE id = $1', [host_id]);
            if (!rows[0]) {
                (0, errors_1.sendError)(res, 404, 'not_found', 'host not found');
                return;
            }
        }
        // Idempotency (PROTOCOL §2): same key + equal body -> replay; same key +
        // different body -> 409 conflict.
        if (typeof idempotency_key === 'string' && idempotency_key) {
            const existing = await (0, idempotency_1.findTaskByIdempotencyKey)(pool, idempotency_key);
            if (existing) {
                const decision = (0, idempotency_1.decideIdempotency)({ body: (0, idempotency_1.taskIdempotencyBody)(existing) }, (0, idempotency_1.taskIdempotencyBody)({ type, payload }));
                if (decision === 'replay') {
                    const task = await getTask(pool, existing.id);
                    res.status(200).json({ task, idempotent_replay: true });
                    return;
                }
                (0, errors_1.sendError)(res, 409, 'conflict', 'idempotency key already used with a different payload');
                return;
            }
        }
        const status = taskMode === 'manual' ? 'awaiting_approval' : 'queued';
        const { rows } = await pool.query(`INSERT INTO tasks (idempotency_key, created_by, assigned_to, type, status, priority, payload)
       VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *`, [
            typeof idempotency_key === 'string' && idempotency_key ? idempotency_key : null,
            auth.id,
            host_id ?? null,
            type,
            status,
            prio,
            JSON.stringify(payload ?? {}),
        ]);
        const task = rows[0];
        await (0, events_1.appendEvent)(pool, {
            type: 'task.created',
            actor_type: 'agent',
            actor_id: auth.name,
            task_id: task.id,
            host_id: task.assigned_to,
            payload: { type, mode: taskMode, idempotency_key: idempotency_key ?? null },
        });
        res.status(201).json({ task });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/tasks?status=&type=&host_id=&limit=&cursor=
exports.tasksRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        const { status, type, host_id } = req.query;
        const limit = (0, _helpers_1.parseLimit)(req.query.limit);
        const cursor = (0, _helpers_1.decodeCursor)(req.query.cursor);
        if (req.query.cursor && !cursor) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'invalid cursor');
            return;
        }
        const conds = [];
        const params = [];
        if (typeof status === 'string') {
            if (!(0, stateMachine_1.isTaskStatus)(status)) {
                (0, errors_1.sendError)(res, 400, 'bad_request', `unknown status: ${status}`);
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
            if (!(0, _helpers_1.isUuid)(host_id)) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'host_id must be a UUID');
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
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT * FROM tasks ${where} ORDER BY created_at DESC, id DESC LIMIT $${params.length}`, params);
        const hasMore = rows.length > limit;
        const page = hasMore ? rows.slice(0, limit) : rows;
        const nextCursor = hasMore && page.length ? (0, _helpers_1.encodeCursor)(page[page.length - 1].created_at, page[page.length - 1].id) : null;
        res.json({ tasks: page, next_cursor: nextCursor });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/tasks/:id
exports.tasksRouter.get('/:id', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const task = await getTask((0, pool_1.getPool)(), req.params.id);
        if (!task) {
            next(new errors_1.HttpError(404, 'not_found', 'task not found'));
            return;
        }
        res.json({ task });
    }
    catch (err) {
        next(err);
    }
});
// POST /v1/tasks/:id/cancel — uses the state machine; terminal states -> 409.
exports.tasksRouter.post('/:id/cancel', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const task = await getTask(pool, req.params.id);
        if (!task) {
            next(new errors_1.HttpError(404, 'not_found', 'task not found'));
            return;
        }
        if (!(0, stateMachine_1.canTransitionTask)(task.status, 'cancelled')) {
            (0, errors_1.sendError)(res, 409, 'conflict', `task is ${task.status}; cannot cancel from a terminal state`);
            return;
        }
        const { rows } = await pool.query(`UPDATE tasks SET status = 'cancelled', completed_at = now() WHERE id = $1 RETURNING *`, [task.id]);
        await (0, events_1.appendEvent)(pool, {
            type: 'task.cancelled',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            host_id: task.assigned_to,
            payload: { previous_status: task.status },
        });
        res.json({ task: rows[0] });
    }
    catch (err) {
        next(err);
    }
});
async function decideApproval(req, res, next, approve) {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const task = await getTask(pool, req.params.id);
        if (!task) {
            next(new errors_1.HttpError(404, 'not_found', 'task not found'));
            return;
        }
        const target = approve ? 'queued' : 'cancelled';
        if (task.status !== 'awaiting_approval' || !(0, stateMachine_1.canTransitionTask)(task.status, target)) {
            (0, errors_1.sendError)(res, 409, 'conflict', `task is ${task.status}; only awaiting_approval tasks can be ${approve ? 'approved' : 'rejected'}`);
            return;
        }
        const { rows } = await pool.query(`UPDATE tasks SET status = $1, completed_at = CASE WHEN $1 = 'cancelled' THEN now() ELSE NULL END
       WHERE id = $2 RETURNING *`, [target, task.id]);
        const updated = rows[0];
        const deploymentId = task.payload?.deployment_id;
        if (approve && typeof deploymentId === 'string' && (0, _helpers_1.isUuid)(deploymentId)) {
            await pool.query(`UPDATE deployments SET status = 'approved' WHERE id = $1`, [deploymentId]);
            await (0, events_1.appendEvent)(pool, {
                type: 'deployment.approved',
                actor_type: 'agent',
                actor_id: req.auth.name,
                task_id: task.id,
                deployment_id: deploymentId,
                host_id: task.assigned_to,
                payload: {},
            });
        }
        await (0, events_1.appendEvent)(pool, {
            type: approve ? 'task.approved' : 'task.rejected',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            host_id: task.assigned_to,
            payload: {},
        });
        res.json({ task: updated });
    }
    catch (err) {
        next(err);
    }
}
// POST /v1/tasks/:id/approve — needs approve_deployments; awaiting_approval -> queued.
exports.tasksRouter.post('/:id/approve', auth_1.requireAgent, (0, auth_1.requirePermission)('approve_deployments'), (req, res, next) => decideApproval(req, res, next, true));
// POST /v1/tasks/:id/reject — needs approve_deployments; awaiting_approval -> cancelled.
exports.tasksRouter.post('/:id/reject', auth_1.requireAgent, (0, auth_1.requirePermission)('approve_deployments'), (req, res, next) => decideApproval(req, res, next, false));
