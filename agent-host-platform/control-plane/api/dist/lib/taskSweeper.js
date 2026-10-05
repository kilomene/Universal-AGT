"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.readTaskSweeperConfig = readTaskSweeperConfig;
exports.sweepTaskDecision = sweepTaskDecision;
exports.startTaskSweeper = startTaskSweeper;
exports.sweepTasksOnce = sweepOnce;
const events_1 = require("./events");
const log_1 = require("./log");
const retryPolicy_1 = require("./retryPolicy");
function readTaskSweeperConfig(env = process.env) {
    const num = (v, def) => {
        const n = Number(v);
        return Number.isFinite(n) && n > 0 ? n : def;
    };
    return {
        intervalS: num(env.TASK_SWEEP_INTERVAL_S, 60),
        leaseS: num(env.TASK_CLAIM_LEASE_S, 600),
    };
}
/** Pure: what should the sweeper do with this task row right now? */
function sweepTaskDecision(row, now = new Date()) {
    if (row.status === 'retrying') {
        // The retrying -> queued hop: the failed attempt already counted its
        // claim, so this move does not consume another attempt.
        return { action: 'requeue', incrementAttempts: false };
    }
    if (row.status !== 'claimed' && row.status !== 'running') {
        return { action: 'none' };
    }
    if (!row.lease_expires_at)
        return { action: 'none' }; // pre-lease rows: untouched
    const leaseExpiry = new Date(row.lease_expires_at);
    if (Number.isNaN(leaseExpiry.getTime()))
        return { action: 'none' };
    if (leaseExpiry.getTime() > now.getTime())
        return { action: 'none' }; // lease alive
    // Lease expired with no recent progress: the worker is gone. Count this
    // lost attempt, then apply the same retry policy as a reported failure.
    const attempts = row.attempts + 1;
    const decision = (0, retryPolicy_1.decideRetryOutcome)({
        type: row.type,
        failedFrom: row.status,
        attempts,
        maxAttempts: row.max_attempts,
    });
    if (decision === 'retry') {
        return { action: 'requeue', incrementAttempts: true };
    }
    return { action: 'fail', incrementAttempts: true };
}
async function sweepOnce(pool, _cfg) {
    const now = new Date();
    const { rows } = await pool.query(`SELECT id, type, status, attempts, max_attempts, lease_expires_at,
            claimed_by, assigned_to
     FROM tasks
     WHERE status IN ('claimed', 'running', 'retrying')`);
    for (const row of rows) {
        const decision = sweepTaskDecision(row, now);
        if (decision.action === 'none')
            continue;
        const target = decision.action === 'requeue' ? 'queued' : 'failed';
        const updated = (await pool.query(`UPDATE tasks SET
           status = $2,
           attempts = attempts + $3,
           claimed_by = CASE WHEN $2 = 'queued' THEN NULL ELSE claimed_by END,
           lease_expires_at = CASE WHEN $2 = 'queued' THEN NULL ELSE lease_expires_at END,
           completed_at = CASE WHEN $2 = 'failed' THEN now() ELSE completed_at END
         WHERE id = $1 RETURNING attempts`, [row.id, target, decision.incrementAttempts ? 1 : 0])).rows[0];
        const attemptsAfter = updated?.attempts ?? row.attempts;
        if (decision.action === 'requeue') {
            await (0, events_1.appendEvent)(pool, {
                type: 'task.requeued',
                actor_type: 'system',
                actor_id: 'task-sweeper',
                task_id: row.id,
                host_id: row.assigned_to,
                payload: {
                    previous_status: row.status,
                    reason: row.status === 'retrying' ? 'retry_approved' : 'claim_lease_expired',
                    attempts: attemptsAfter,
                },
            });
            log_1.logger.info('task requeued by sweeper', {
                task: row.id,
                from: row.status,
                attempts: attemptsAfter,
            });
        }
        else {
            const reason = attemptsAfter >= row.max_attempts
                ? 'max_attempts_exceeded'
                : `retry_not_allowed:${row.type}:${row.status}`;
            await (0, events_1.appendEvent)(pool, {
                type: 'task.failed',
                actor_type: 'system',
                actor_id: 'task-sweeper',
                task_id: row.id,
                host_id: row.assigned_to,
                payload: { reason, attempts: attemptsAfter },
            });
            log_1.logger.warn('task failed by sweeper (lease expired, no retry)', {
                task: row.id,
                reason,
            });
        }
    }
}
/** Start the periodic stuck-task sweep. Returns a stop function. */
function startTaskSweeper(pool, cfg = readTaskSweeperConfig()) {
    const timer = setInterval(() => {
        sweepOnce(pool, cfg).catch((err) => {
            log_1.logger.warn('task sweeper pass failed', { err: String(err) });
        });
    }, cfg.intervalS * 1000);
    timer.unref?.();
    log_1.logger.info('task sweeper started', { ...cfg });
    return () => clearInterval(timer);
}
