"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.workerRouter = void 0;
const express_1 = require("express");
const promises_1 = require("fs/promises");
const path_1 = require("path");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const stateMachine_1 = require("../lib/stateMachine");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.workerRouter = (0, express_1.Router)();
function repoRoot() {
    return (0, path_1.resolve)(__dirname, '..', '..', '..', '..');
}
function logDir() {
    const configured = process.env.LOG_DIR;
    return configured ? (0, path_1.resolve)(configured) : (0, path_1.join)(repoRoot(), 'data', 'logs');
}
function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
}
// ---------------------------------------------------------------------------
// POST /v1/hosts/:id/heartbeat — HOST token only; the token must belong to
// the host identified in the path.
// ---------------------------------------------------------------------------
exports.workerRouter.post('/:id/heartbeat', auth_1.requireHost, async (req, res, next) => {
    try {
        const hostId = req.params.id;
        if (hostId !== req.auth.id) {
            (0, errors_1.sendError)(res, 403, 'forbidden', 'host token does not belong to this host');
            return;
        }
        const body = req.body ?? {};
        const numeric = (v) => typeof v === 'number' && Number.isFinite(v) ? v : null;
        const pool = (0, pool_1.getPool)();
        const current = await pool.query('SELECT status FROM hosts WHERE id = $1', [hostId]);
        if (!current.rows[0]) {
            next(new errors_1.HttpError(404, 'not_found', 'host not found'));
            return;
        }
        const wasOnline = current.rows[0].status === 'online';
        await pool.query(`UPDATE hosts SET
         status = 'online',
         cpu_pct = COALESCE($2, cpu_pct),
         ram_pct = COALESCE($3, ram_pct),
         disk_pct = COALESCE($4, disk_pct),
         docker_status = COALESCE($5, docker_status),
         running_apps = COALESCE($6, running_apps),
         worker_version = COALESCE($7, worker_version),
         total_cpu = COALESCE($8, total_cpu),
         total_ram_mb = COALESCE($9, total_ram_mb),
         total_disk_gb = COALESCE($10, total_disk_gb),
         last_seen = now()
       WHERE id = $1`, [
            hostId,
            numeric(body.cpu_pct),
            numeric(body.ram_pct),
            numeric(body.disk_pct),
            typeof body.docker_status === 'string' ? body.docker_status : null,
            Array.isArray(body.running_apps) ? JSON.stringify(body.running_apps) : null,
            typeof body.worker_version === 'string' ? body.worker_version : null,
            numeric(body.total_cpu),
            numeric(body.total_ram_mb),
            numeric(body.total_disk_gb),
        ]);
        if (!wasOnline) {
            await (0, events_1.appendEvent)(pool, {
                type: 'host.online',
                actor_type: 'host',
                actor_id: req.auth.name,
                host_id: hostId,
                payload: {},
            });
            log_1.logger.info('host came online', { host: req.auth.name });
        }
        // pending_tasks: queued work this host may claim.
        const pending = await pool.query(`SELECT count(*)::int AS n FROM tasks
       WHERE status = 'queued' AND (assigned_to IS NULL OR assigned_to = $1)`, [hostId]);
        const hostRow = (await pool.query(`SELECT id, name, host_type, status, capabilities, worker_version,
                cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
                total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
                created_at, updated_at FROM hosts WHERE id = $1`, [hostId])).rows[0];
        res.json({ host: (0, _helpers_1.publicHost)(hostRow), pending_tasks: pending.rows[0].n });
    }
    catch (err) {
        next(err);
    }
});
// ---------------------------------------------------------------------------
// POST /v1/worker/tasks/claim?wait=N — HOST token only; body {host_id}.
// Atomic claim: exactly one host gets a queued task (FOR UPDATE SKIP LOCKED).
// ?wait=N (max 30): hold the request, polling every 1s, until work appears.
// 204 when nothing is claimable within the window.
// ---------------------------------------------------------------------------
async function tryClaim(pool, hostId) {
    const { rows } = await pool.query(`UPDATE tasks SET
       status = 'claimed',
       claimed_by = $1,
       assigned_to = $1,
       attempts = attempts + 1,
       started_at = COALESCE(started_at, now())
     WHERE id = (
       SELECT id FROM tasks
       WHERE status = 'queued' AND (assigned_to IS NULL OR assigned_to = $1)
       ORDER BY priority DESC, created_at ASC
       LIMIT 1
       FOR UPDATE SKIP LOCKED
     )
     RETURNING *`, [hostId]);
    return rows[0] ?? null;
}
exports.workerRouter.post('/tasks/claim', auth_1.requireHost, async (req, res, next) => {
    try {
        const hostId = (req.body ?? {}).host_id;
        if (typeof hostId !== 'string' || !(0, _helpers_1.isUuid)(hostId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'host_id (UUID) is required in the body');
            return;
        }
        if (hostId !== req.auth.id) {
            (0, errors_1.sendError)(res, 403, 'forbidden', 'host token does not match host_id');
            return;
        }
        let wait = Number(req.query.wait ?? 0);
        if (!Number.isFinite(wait) || wait < 0)
            wait = 0;
        wait = Math.min(Math.floor(wait), 30);
        const pool = (0, pool_1.getPool)();
        const deadline = Date.now() + wait * 1000;
        let claimed = null;
        let clientGone = false;
        req.on('close', () => {
            clientGone = true;
        });
        for (;;) {
            claimed = await tryClaim(pool, hostId);
            if (claimed || clientGone)
                break;
            if (Date.now() >= deadline)
                break;
            await sleep(1000);
        }
        if (!claimed) {
            res.status(204).end();
            return;
        }
        await (0, events_1.appendEvent)(pool, {
            type: 'task.claimed',
            actor_type: 'host',
            actor_id: req.auth.name,
            task_id: claimed.id,
            host_id: hostId,
            payload: { type: claimed.type, attempts: claimed.attempts },
        });
        log_1.logger.info('task claimed', { task: claimed.id, host: req.auth.name });
        res.json({ task: claimed });
    }
    catch (err) {
        next(err);
    }
});
// ---------------------------------------------------------------------------
// POST /v1/worker/tasks/:id/progress — HOST token only; only the claiming
// host may report. Validates the transition via the state machine, appends
// log_chunk to LOG_DIR/<task_id>.log, sets completed_at on terminal states,
// mirrors deployment status for deploy/rollback/service tasks, emits events.
// ---------------------------------------------------------------------------
const PROGRESS_STATUSES = ['claimed', 'running', 'awaiting_approval', 'completed', 'failed'];
async function mirrorDeploymentState(pool, task, nextStatus, actorName) {
    const payload = (task.payload ?? {});
    const deploymentId = payload.deployment_id;
    if (typeof deploymentId !== 'string' || !(0, _helpers_1.isUuid)(deploymentId))
        return;
    const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [deploymentId]);
    const deployment = rows[0];
    if (!deployment)
        return;
    const isRollbackTask = task.type === 'deploy' && payload.rollback === true;
    const serviceEvent = task.type === 'restart' ? 'service.restarted' : task.type === 'stop' ? 'service.stopped' : task.type === 'start' ? 'service.started' : null;
    let depStatus = null;
    let depEvent = null;
    if (nextStatus === 'running') {
        if (['requested', 'approved'].includes(deployment.status)) {
            depStatus = task.type === 'deploy' ? 'building' : deployment.status;
            depEvent = task.type === 'deploy' ? (isRollbackTask ? null : 'deployment.started') : null;
        }
    }
    else if (nextStatus === 'completed') {
        if (isRollbackTask) {
            depStatus = 'rolled_back';
            depEvent = 'deployment.rolled_back';
        }
        else if (task.type === 'deploy') {
            depStatus = 'running';
            depEvent = 'deployment.completed';
        }
        else if (serviceEvent) {
            depStatus = task.type === 'stop' ? 'stopped' : 'running';
            depEvent = serviceEvent;
        }
    }
    else if (nextStatus === 'failed') {
        depStatus = 'failed';
        depEvent = task.type === 'deploy' && !isRollbackTask ? 'deployment.failed' : serviceEvent ? null : 'deployment.failed';
    }
    if (depStatus) {
        await pool.query(`UPDATE deployments SET status = $2, health_status = CASE WHEN $2 = 'running' THEN 'healthy' WHEN $2 = 'failed' THEN 'unhealthy' ELSE health_status END WHERE id = $1`, [deploymentId, depStatus]);
    }
    if (depEvent) {
        await (0, events_1.appendEvent)(pool, {
            type: depEvent,
            actor_type: 'host',
            actor_id: actorName,
            task_id: task.id,
            deployment_id: deploymentId,
            host_id: task.claimed_by,
            payload: { version: deployment.version },
        });
    }
}
exports.workerRouter.post('/tasks/:id/progress', auth_1.requireHost, async (req, res, next) => {
    try {
        const taskId = req.params.id;
        if (!(0, _helpers_1.isUuid)(taskId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const { status, log_chunk, result, error } = req.body ?? {};
        if (typeof status !== 'string' || !PROGRESS_STATUSES.includes(status)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', `status must be one of: ${PROGRESS_STATUSES.join(', ')}`);
            return;
        }
        if (log_chunk !== undefined && typeof log_chunk !== 'string') {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'log_chunk must be a string');
            return;
        }
        if (result !== undefined && (typeof result !== 'object' || result === null || Array.isArray(result))) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'result must be an object');
            return;
        }
        if (error !== undefined && typeof error !== 'string') {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'error must be a string');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM tasks WHERE id = $1', [taskId]);
        const task = rows[0];
        if (!task) {
            next(new errors_1.HttpError(404, 'not_found', 'task not found'));
            return;
        }
        if (task.claimed_by !== req.auth.id) {
            (0, errors_1.sendError)(res, 403, 'forbidden', 'only the claiming host may report progress on this task');
            return;
        }
        if (!(0, stateMachine_1.canTransitionTask)(task.status, status)) {
            (0, errors_1.sendError)(res, 409, 'conflict', `cannot transition task from ${task.status} to ${status}`);
            return;
        }
        if (log_chunk) {
            const dir = logDir();
            await (0, promises_1.mkdir)(dir, { recursive: true });
            await (0, promises_1.appendFile)((0, path_1.join)(dir, `${taskId}.log`), log_chunk, 'utf8');
        }
        const terminal = (0, stateMachine_1.isTerminalTaskStatus)(status);
        const updated = (await pool.query(`UPDATE tasks SET
           status = $2,
           result = COALESCE($3, result),
           error = COALESCE($4, error),
           started_at = CASE WHEN $2 = 'running' AND started_at IS NULL THEN now() ELSE started_at END,
           completed_at = CASE WHEN $5 THEN now() ELSE completed_at END
         WHERE id = $1 RETURNING *`, [
            taskId,
            status,
            result !== undefined ? JSON.stringify(result) : null,
            error !== undefined ? error : null,
            terminal,
        ])).rows[0];
        const eventFor = {
            running: 'task.started',
            awaiting_approval: 'task.awaiting_approval',
            completed: 'task.completed',
            failed: 'task.failed',
        };
        if (eventFor[status]) {
            await (0, events_1.appendEvent)(pool, {
                type: eventFor[status],
                actor_type: 'host',
                actor_id: req.auth.name,
                task_id: taskId,
                host_id: task.claimed_by,
                payload: status === 'failed' ? { error: error ?? null } : {},
            });
        }
        await mirrorDeploymentState(pool, task, status, req.auth.name);
        res.json({ task: updated });
    }
    catch (err) {
        next(err);
    }
});
