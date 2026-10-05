"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.workerRouter = void 0;
exports.resetCrashLoopSeenForTests = resetCrashLoopSeenForTests;
exports.hostMayReadProjectSecrets = hostMayReadProjectSecrets;
const express_1 = require("express");
const promises_1 = require("fs/promises");
const path_1 = require("path");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const ports_1 = require("../lib/ports");
const retryPolicy_1 = require("../lib/retryPolicy");
const stateMachine_1 = require("../lib/stateMachine");
const taskSweeper_1 = require("../lib/taskSweeper");
const auth_1 = require("../middleware/auth");
const secrets_1 = require("./secrets");
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
// First-appearance dedupe for worker-reported crash loops: the heartbeat
// payload keeps carrying the issue while the flag stands, but the
// service.crash_loop event fires once per (host, deployment) per process.
const crashLoopSeen = new Set();
function resetCrashLoopSeenForTests() {
    crashLoopSeen.clear();
}
async function emitWorkerIssues(pool, hostId, hostName, issues) {
    if (!Array.isArray(issues))
        return;
    for (const issue of issues) {
        if (typeof issue !== 'object' || issue === null)
            continue;
        const rec = issue;
        if (rec.issue !== 'crash_loop')
            continue; // unknown issue kinds: ignored
        const deploymentId = typeof rec.deployment_id === 'string' ? rec.deployment_id : null;
        if (!deploymentId)
            continue;
        const key = `${hostId}:${deploymentId}`;
        if (crashLoopSeen.has(key))
            continue;
        crashLoopSeen.add(key);
        await (0, events_1.appendEvent)(pool, {
            type: 'service.crash_loop',
            actor_type: 'host',
            actor_id: hostName,
            deployment_id: deploymentId,
            host_id: hostId,
            payload: {
                project: typeof rec.project === 'string' ? rec.project : null,
                container: typeof rec.container === 'string' ? rec.container : null,
                restarts: typeof rec.restarts === 'number' ? rec.restarts : null,
            },
        });
        log_1.logger.warn('service entered crash loop', {
            host: hostName,
            deployment: deploymentId,
        });
    }
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
        const prevStatus = current.rows[0].status;
        // A heartbeat revives offline/degraded hosts to online, but NEVER
        // overwrites an operator-set `draining` state.
        const revived = prevStatus === 'offline' || prevStatus === 'degraded';
        await pool.query(`UPDATE hosts SET
         status = CASE WHEN status = 'draining' THEN 'draining' ELSE 'online' END,
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
        if (revived) {
            await (0, events_1.appendEvent)(pool, {
                type: 'host.online',
                actor_type: 'host',
                actor_id: req.auth.name,
                host_id: hostId,
                payload: { previous_status: prevStatus },
            });
            log_1.logger.info('host came online', { host: req.auth.name, from: prevStatus });
        }
        // Worker-reported issues (e.g. crash loops) -> events, first appearance.
        await emitWorkerIssues(pool, hostId, req.auth.name, body.issues);
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
//
// The claim records a lease (claimed_at + TASK_CLAIM_LEASE_S, default 600s):
// a task that stays in claimed/running past its lease with no progress
// report is requeued by the stuck-task sweeper. Every progress report
// refreshes the lease, so an actively-reporting worker is never swept.
// ---------------------------------------------------------------------------
async function tryClaim(pool, hostId, leaseS) {
    const { rows } = await pool.query(`UPDATE tasks SET
       status = 'claimed',
       claimed_by = $1,
       assigned_to = $1,
       attempts = attempts + 1,
       lease_expires_at = now() + make_interval(secs => $2),
       last_progress_at = now(),
       started_at = COALESCE(started_at, now())
     WHERE id = (
       SELECT id FROM tasks
       WHERE status = 'queued' AND (assigned_to IS NULL OR assigned_to = $1)
       ORDER BY priority DESC, created_at ASC
       LIMIT 1
       FOR UPDATE SKIP LOCKED
     )
     RETURNING *`, [hostId, leaseS]);
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
        const leaseS = (0, taskSweeper_1.readTaskSweeperConfig)().leaseS;
        const deadline = Date.now() + wait * 1000;
        let claimed = null;
        let clientGone = false;
        req.on('close', () => {
            clientGone = true;
        });
        for (;;) {
            claimed = await tryClaim(pool, hostId, leaseS);
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
async function mirrorDeploymentState(pool, task, nextStatus, actorName, reportedResult) {
    const payload = (task.payload ?? {});
    const deploymentId = payload.deployment_id;
    if (typeof deploymentId !== 'string' || !(0, _helpers_1.isUuid)(deploymentId))
        return;
    const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [deploymentId]);
    const deployment = rows[0];
    if (!deployment)
        return;
    const isRollbackTask = task.type === 'rollback' || (task.type === 'deploy' && payload.rollback === true);
    const serviceEvent = task.type === 'restart' ? 'service.restarted' : task.type === 'stop' ? 'service.stopped' : task.type === 'start' ? 'service.started' : null;
    let depStatus = null;
    let depEvent = null;
    if (nextStatus === 'running') {
        if (['requested', 'approved'].includes(deployment.status)) {
            depStatus = task.type === 'deploy' && !isRollbackTask ? 'building' : deployment.status;
            depEvent = task.type === 'deploy' && !isRollbackTask ? 'deployment.started' : null;
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
        else if (task.type === 'remove') {
            depStatus = 'stopped';
            depEvent = 'service.removed';
        }
        else if (serviceEvent) {
            depStatus = task.type === 'stop' ? 'stopped' : 'running';
            depEvent = serviceEvent;
        }
    }
    else if (nextStatus === 'failed') {
        if (task.type === 'rollback') {
            // A failed rollback leaves the deployment where it was; the failure
            // is recorded as an event, not a deployment status.
            depEvent = 'deployment.rollback_failed';
        }
        else {
            depStatus = 'failed';
            depEvent = task.type === 'deploy' && !isRollbackTask ? 'deployment.failed' : serviceEvent ? null : 'deployment.failed';
        }
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
            payload: depEvent === 'deployment.rolled_back'
                ? { version: deployment.version, rolled_back_to: payload.target_deployment_id ?? null }
                : { version: deployment.version },
        });
    }
    // The worker reports the real host->container port mapping in
    // task.result.ports on a completed deploy — persist it on the deployment
    // row (this is what the deployments.ports JSONB column is for).
    if (task.type === 'deploy' &&
        !isRollbackTask &&
        nextStatus === 'completed' &&
        reportedResult &&
        typeof reportedResult === 'object' &&
        reportedResult.ports &&
        typeof reportedResult.ports === 'object') {
        await pool.query('UPDATE deployments SET ports = $2 WHERE id = $1', [
            deploymentId,
            JSON.stringify(reportedResult.ports),
        ]);
    }
    // Port registry lifecycle: release this deployment's reservations when it
    // reaches a terminal state; when a deploy reaches `running`, older
    // deployments of the same project+host are superseded — free their ports.
    if (depStatus && ports_1.TERMINAL_PORT_RELEASE_STATUSES.has(depStatus)) {
        await (0, ports_1.releasePortAllocations)(pool, deploymentId);
    }
    if (task.type === 'deploy' && !isRollbackTask && nextStatus === 'completed') {
        await (0, ports_1.releaseSupersededPortAllocations)(pool, deployment.project_id, deployment.host_id, deploymentId);
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
        // A worker-reported `failed` is not necessarily terminal: the retry
        // policy may park the task in `retrying` (the stuck-task sweeper later
        // returns it to `queued`) when the budget allows and the task type is
        // safe to re-run from where it failed.
        let finalStatus = status;
        if (status === 'failed') {
            const outcome = (0, retryPolicy_1.decideRetryOutcome)({
                type: task.type,
                failedFrom: task.status,
                attempts: task.attempts,
                maxAttempts: task.max_attempts,
            });
            if (outcome === 'retry')
                finalStatus = 'retrying';
        }
        if (!(0, stateMachine_1.canTransitionTask)(task.status, finalStatus)) {
            (0, errors_1.sendError)(res, 409, 'conflict', `cannot transition task from ${task.status} to ${finalStatus}`);
            return;
        }
        if (log_chunk) {
            const dir = logDir();
            await (0, promises_1.mkdir)(dir, { recursive: true });
            await (0, promises_1.appendFile)((0, path_1.join)(dir, `${taskId}.log`), log_chunk, 'utf8');
        }
        const terminal = (0, stateMachine_1.isTerminalTaskStatus)(finalStatus);
        const leaseS = (0, taskSweeper_1.readTaskSweeperConfig)().leaseS;
        const updated = (await pool.query(`UPDATE tasks SET
           status = $2,
           result = COALESCE($3, result),
           error = COALESCE($4, error),
           started_at = CASE WHEN $2 = 'running' AND started_at IS NULL THEN now() ELSE started_at END,
           completed_at = CASE WHEN $5 THEN now() ELSE completed_at END,
           last_progress_at = now(),
           lease_expires_at = CASE WHEN $5 THEN NULL ELSE now() + make_interval(secs => $6) END
         WHERE id = $1 RETURNING *`, [
            taskId,
            finalStatus,
            result !== undefined ? JSON.stringify(result) : null,
            error !== undefined ? error : null,
            terminal,
            leaseS,
        ])).rows[0];
        const eventFor = {
            running: 'task.started',
            awaiting_approval: 'task.awaiting_approval',
            completed: 'task.completed',
            failed: 'task.failed',
            retrying: 'task.retrying',
        };
        if (eventFor[finalStatus]) {
            await (0, events_1.appendEvent)(pool, {
                type: eventFor[finalStatus],
                actor_type: 'host',
                actor_id: req.auth.name,
                task_id: taskId,
                host_id: task.claimed_by,
                payload: finalStatus === 'failed'
                    ? { error: error ?? null }
                    : finalStatus === 'retrying'
                        ? { error: error ?? null, attempts: updated.attempts, max_attempts: updated.max_attempts }
                        : {},
            });
        }
        await mirrorDeploymentState(pool, task, finalStatus, req.auth.name, typeof result === 'object' && result !== null && !Array.isArray(result) ? result : null);
        res.json({ task: updated });
    }
    catch (err) {
        next(err);
    }
});
// ---------------------------------------------------------------------------
// GET /v1/worker/domains — HOST token only.
// Every domain entry on this host's non-terminal deployments, so the worker
// can rebuild its ingress route table (Phase 7):
//   {domains: [{deployment_id, hostname, ingress}]}
// 'ingress' defaults to 'direct' for entries recorded before Phase 7.
// ---------------------------------------------------------------------------
exports.workerRouter.get('/domains', auth_1.requireHost, async (req, res, next) => {
    try {
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT id AS deployment_id, domains FROM deployments
       WHERE host_id = $1 AND status NOT IN ('removed', 'rolled_back', 'failed')`, [req.auth.id]);
        const out = [];
        for (const row of rows) {
            const entries = Array.isArray(row.domains) ? row.domains : [];
            for (const e of entries) {
                if (e && typeof e.hostname === 'string') {
                    out.push({
                        deployment_id: row.deployment_id,
                        hostname: e.hostname,
                        ingress: e.ingress === 'tunnel' ? 'tunnel' : 'direct',
                    });
                }
            }
        }
        res.json({ domains: out });
    }
    catch (err) {
        next(err);
    }
});
// ---------------------------------------------------------------------------
// GET /v1/worker/projects/:project_id/secrets — HOST token only.
// Returns the DECRYPTED project secrets, scoped (2026-10-05): only when
// this host has an active (claimed/running/awaiting_approval) task for the
// project, or a live (requested/approved/building/starting/healthcheck/
// running) deployment of it. Completes the secrets feature: the worker
// pulls these at deploy time and injects them as container env (never
// persisted to state.json). 403 when the host has no live work for the
// project; 404 when the project does not exist.
// ---------------------------------------------------------------------------
async function hostMayReadProjectSecrets(pool, hostId, projectId) {
    const { rows } = await pool.query(`SELECT 1 FROM tasks
      WHERE claimed_by = $1 AND payload->>'project_id' = $2
        AND status IN ('claimed', 'running', 'awaiting_approval')
     UNION
     SELECT 1 FROM deployments
      WHERE host_id = $1 AND project_id = $2
        AND status IN ('requested', 'approved', 'building', 'starting',
                       'healthcheck', 'running')
     LIMIT 1`, [hostId, projectId]);
    return rows.length > 0;
}
exports.workerRouter.get('/projects/:project_id/secrets', auth_1.requireHost, async (req, res, next) => {
    try {
        const projectId = req.params.project_id;
        if (!(0, _helpers_1.isUuid)(projectId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'project_id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const project = await pool.query('SELECT id FROM projects WHERE id = $1', [projectId]);
        if (!project.rows[0]) {
            next(new errors_1.HttpError(404, 'not_found', 'project not found'));
            return;
        }
        const allowed = await hostMayReadProjectSecrets(pool, req.auth.id, projectId);
        if (!allowed) {
            (0, errors_1.sendError)(res, 403, 'forbidden', 'no active task or deployment for this project on this host');
            return;
        }
        const secrets = await (0, secrets_1.getDecryptedProjectSecrets)(pool, projectId);
        res.json({ secrets });
    }
    catch (err) {
        next(err);
    }
});
