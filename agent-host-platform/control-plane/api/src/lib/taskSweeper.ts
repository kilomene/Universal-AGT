import type { Pool } from 'pg';
import { appendEvent } from './events';
import { logger } from './log';
import { decideRetryOutcome } from './retryPolicy';

// Stuck-task sweeper: every TASK_SWEEP_INTERVAL_S (default 60s) the control
// plane looks at tasks in `claimed` / `running` whose claim lease has expired
// (claimed_at + TASK_CLAIM_LEASE_S, default 600s, refreshed on every progress
// report — so a task that is actively reporting can never be swept) and tasks
// parked in `retrying`:
//
//   claimed/running + lease expired -> requeue (`queued`, attempts+1,
//     `task.requeued`), unless the retry budget is exhausted or the retry
//     policy forbids it -> terminal `failed` (`task.failed`).
//   retrying -> `queued` (the claim that created the retry already counted
//     the attempt; moving it back to `queued` costs nothing).
//
// The pure core (sweepTaskDecision) is unit-tested without a database.

export interface TaskSweeperConfig {
  intervalS: number;
  leaseS: number;
}

export function readTaskSweeperConfig(env: NodeJS.ProcessEnv = process.env): TaskSweeperConfig {
  const num = (v: string | undefined, def: number): number => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : def;
  };
  return {
    intervalS: num(env.TASK_SWEEP_INTERVAL_S, 60),
    leaseS: num(env.TASK_CLAIM_LEASE_S, 600),
  };
}

export interface SweepTaskRow {
  id: string;
  type: string;
  status: string;
  attempts: number;
  max_attempts: number;
  lease_expires_at: Date | string | null;
  claimed_by: string | null;
  assigned_to: string | null;
}

export type SweepDecision =
  | { action: 'requeue'; incrementAttempts: boolean }
  | { action: 'fail'; incrementAttempts: boolean }
  | { action: 'none' };

/** Pure: what should the sweeper do with this task row right now? */
export function sweepTaskDecision(row: SweepTaskRow, now: Date = new Date()): SweepDecision {
  if (row.status === 'retrying') {
    // The retrying -> queued hop: the failed attempt already counted its
    // claim, so this move does not consume another attempt.
    return { action: 'requeue', incrementAttempts: false };
  }
  if (row.status !== 'claimed' && row.status !== 'running') {
    return { action: 'none' };
  }
  if (!row.lease_expires_at) return { action: 'none' }; // pre-lease rows: untouched
  const leaseExpiry = new Date(row.lease_expires_at);
  if (Number.isNaN(leaseExpiry.getTime())) return { action: 'none' };
  if (leaseExpiry.getTime() > now.getTime()) return { action: 'none' }; // lease alive

  // Lease expired with no recent progress: the worker is gone. Count this
  // lost attempt, then apply the same retry policy as a reported failure.
  const attempts = row.attempts + 1;
  const decision = decideRetryOutcome({
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

async function sweepOnce(pool: Pool, _cfg: TaskSweeperConfig): Promise<void> {
  const now = new Date();
  const { rows } = await pool.query(
    `SELECT id, type, status, attempts, max_attempts, lease_expires_at,
            claimed_by, assigned_to
     FROM tasks
     WHERE status IN ('claimed', 'running', 'retrying')`,
  );
  for (const row of rows as SweepTaskRow[]) {
    const decision = sweepTaskDecision(row, now);
    if (decision.action === 'none') continue;

    const target = decision.action === 'requeue' ? 'queued' : 'failed';
    // Compare-and-swap (§74): re-check the status AND the lease expiry in
    // the UPDATE itself. Between the SELECT above and this UPDATE the
    // owning worker may have reported progress (refreshing the lease),
    // completed the task, or another sweeper replica may have swept it
    // first — in all of those cases the row no longer matches and this
    // UPDATE touches zero rows, so we skip it instead of double-counting
    // an attempt or emitting a duplicate event.
    const updated = (
      await pool.query(
        `UPDATE tasks SET
           status = $2,
           attempts = attempts + $3,
           claimed_by = CASE WHEN $2 = 'queued' THEN NULL ELSE claimed_by END,
           lease_expires_at = CASE WHEN $2 = 'queued' THEN NULL ELSE lease_expires_at END,
           completed_at = CASE WHEN $2 = 'failed' THEN now() ELSE completed_at END
         WHERE id = $1
           AND status = $4
           AND ($4 = 'retrying' OR lease_expires_at <= now())
         RETURNING attempts`,
        [row.id, target, decision.incrementAttempts ? 1 : 0, row.status],
      )
    ).rows[0];
    if (!updated) continue; // lost the race: someone else moved the task

    const attemptsAfter: number = updated?.attempts ?? row.attempts;
    if (decision.action === 'requeue') {
      await appendEvent(pool, {
        type: 'task.requeued',
        actor_type: 'system',
        actor_id: 'task-sweeper',
        task_id: row.id,
        // §13: the host that lost the lease is claimed_by; assigned_to is
        // the agent-requested pin (or null), not the claiming host.
        host_id: row.claimed_by ?? row.assigned_to,
        payload: {
          previous_status: row.status,
          reason: row.status === 'retrying' ? 'retry_approved' : 'claim_lease_expired',
          attempts: attemptsAfter,
        },
      });
      logger.info('task requeued by sweeper', {
        task: row.id,
        from: row.status,
        attempts: attemptsAfter,
      });
    } else {
      const reason =
        attemptsAfter >= row.max_attempts
          ? 'max_attempts_exceeded'
          : `retry_not_allowed:${row.type}:${row.status}`;
      await appendEvent(pool, {
        type: 'task.failed',
        actor_type: 'system',
        actor_id: 'task-sweeper',
        task_id: row.id,
        // §13: see the task.requeued event above — claimed_by is the host
        // that lost the lease.
        host_id: row.claimed_by ?? row.assigned_to,
        payload: { reason, attempts: attemptsAfter },
      });
      logger.warn('task failed by sweeper (lease expired, no retry)', {
        task: row.id,
        reason,
      });
    }
  }
}

/** Start the periodic stuck-task sweep. Returns a stop function. */
export function startTaskSweeper(pool: Pool, cfg: TaskSweeperConfig = readTaskSweeperConfig()): () => void {
  const timer = setInterval(() => {
    sweepOnce(pool, cfg).catch((err) => {
      logger.warn('task sweeper pass failed', { err: String(err) });
    });
  }, cfg.intervalS * 1000);
  timer.unref?.();
  logger.info('task sweeper started', { ...cfg });
  return () => clearInterval(timer);
}

export { sweepOnce as sweepTasksOnce };
