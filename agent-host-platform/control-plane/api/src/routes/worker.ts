import { Router } from 'express';
import { appendFile, mkdir } from 'fs/promises';
import { join, resolve } from 'path';
import type { Pool } from 'pg';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import {
  TERMINAL_PORT_RELEASE_STATUSES,
  releasePortAllocations,
  releaseSupersededPortAllocations,
} from '../lib/ports';
import { decideRetryOutcome } from '../lib/retryPolicy';
import { canTransitionTask, isTaskStatus, isTerminalTaskStatus } from '../lib/stateMachine';
import { readTaskSweeperConfig } from '../lib/taskSweeper';
import {
  failDomainsForDeployment,
  reprovisionDomainsForDeployment,
} from './domains';
import { requireHost } from '../middleware/auth';
import { getDecryptedProjectSecrets } from './secrets';
import { isUuid, publicHost } from './_helpers';
import {
  API_VERSION,
  MIN_WORKER_VERSION,
  isOutdatedWorkerVersion,
} from '../lib/versions';

export const workerRouter = Router();

function repoRoot(): string {
  return resolve(__dirname, '..', '..', '..', '..');
}

function logDir(): string {
  const configured = process.env.LOG_DIR;
  return configured ? resolve(configured) : join(repoRoot(), 'data', 'logs');
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// First-appearance dedupe for worker-reported crash loops: the heartbeat
// payload keeps carrying the issue while the flag stands, but the
// service.crash_loop event fires once per (host, deployment) per process.
const crashLoopSeen = new Set<string>();

// Same pattern for outdated workers (§61): the heartbeat keeps reporting
// the old version, but host.worker_outdated fires once per host per process.
const workerOutdatedSeen = new Set<string>();

async function emitWorkerOutdated(
  pool: Pool,
  hostId: string,
  hostName: string,
  workerVersion: string,
): Promise<void> {
  if (workerOutdatedSeen.has(hostId)) return;
  workerOutdatedSeen.add(hostId);
  await appendEvent(pool, {
    type: 'host.worker_outdated',
    actor_type: 'host',
    actor_id: hostName,
    host_id: hostId,
    payload: {
      worker_version: workerVersion,
      min_worker_version: MIN_WORKER_VERSION,
      api_version: API_VERSION,
    },
  });
  logger.warn('host worker is outdated', {
    host: hostName,
    worker_version: workerVersion,
    min_worker_version: MIN_WORKER_VERSION,
  });
}

async function emitWorkerIssues(
  pool: Pool,
  hostId: string,
  hostName: string,
  issues: unknown,
): Promise<void> {
  if (!Array.isArray(issues)) return;
  for (const issue of issues) {
    if (typeof issue !== 'object' || issue === null) continue;
    const rec = issue as Record<string, unknown>;
    if (rec.issue !== 'crash_loop') continue; // unknown issue kinds: ignored
    const deploymentId = typeof rec.deployment_id === 'string' ? rec.deployment_id : null;
    if (!deploymentId) continue;
    const key = `${hostId}:${deploymentId}`;
    if (crashLoopSeen.has(key)) continue;
    crashLoopSeen.add(key);
    await appendEvent(pool, {
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
    logger.warn('service entered crash loop', {
      host: hostName,
      deployment: deploymentId,
    });
  }
}

// ---------------------------------------------------------------------------
// POST /v1/hosts/:id/heartbeat — HOST token only; the token must belong to
// the host identified in the path.
// ---------------------------------------------------------------------------
workerRouter.post('/:id/heartbeat', requireHost, async (req, res, next) => {
  try {
    const hostId = req.params.id;
    if (hostId !== req.auth!.id) {
      sendError(res, 403, 'forbidden', 'host token does not belong to this host');
      return;
    }
    const body = req.body ?? {};
    const numeric = (v: unknown): number | null =>
      typeof v === 'number' && Number.isFinite(v) ? v : null;

    const pool = getPool();

    const current = await pool.query('SELECT status, worker_version FROM hosts WHERE id = $1', [hostId]);
    if (!current.rows[0]) {
      next(new HttpError(404, 'not_found', 'host not found'));
      return;
    }
    const prevStatus: string = current.rows[0].status;
    const prevWorkerVersion: string | null =
      typeof current.rows[0].worker_version === 'string' ? current.rows[0].worker_version : null;
    // A heartbeat revives offline/degraded hosts to online, but NEVER
    // overwrites an operator-set `draining` state.
    const revived = prevStatus === 'offline' || prevStatus === 'degraded';

    // §61: the worker's reported version, for outdated-version detection
    // below. Stored via the COALESCE update like every other heartbeat field.
    const reportedWorkerVersion =
      typeof body.worker_version === 'string' ? body.worker_version : null;

    // WS-B heartbeat enrichment: persist the worker-reported identity
    // fields the old UPDATE dropped — drain state, worker status,
    // capabilities, ingress descriptor, and configured host name — so the
    // scheduler and dashboard can use them. All are COALESCE-guarded: a
    // heartbeat that omits a field never clears the stored value.
    const reportedCapabilities = Array.isArray(body.capabilities) ? body.capabilities : null;
    const reportedDraining = typeof body.draining === 'boolean' ? body.draining : null;
    const reportedWorkerStatus =
      typeof body.worker_status === 'string' && body.worker_status ? body.worker_status : null;
    const reportedIngress =
      body.ingress !== null && typeof body.ingress === 'object' && !Array.isArray(body.ingress)
        ? body.ingress
        : null;
    const reportedHostName =
      typeof body.host_name === 'string' && body.host_name.trim() ? body.host_name.trim() : null;

    await pool.query(
      `UPDATE hosts SET
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
         capabilities = COALESCE($11, capabilities),
         worker_draining = COALESCE($12, worker_draining),
         worker_status = COALESCE($13, worker_status),
         ingress = COALESCE($14, ingress),
         reported_host_name = COALESCE($15, reported_host_name),
         last_seen = now()
       WHERE id = $1`,
      [
        hostId,
        numeric(body.cpu_pct),
        numeric(body.ram_pct),
        numeric(body.disk_pct),
        typeof body.docker_status === 'string' ? body.docker_status : null,
        Array.isArray(body.running_apps) ? JSON.stringify(body.running_apps) : null,
        reportedWorkerVersion,
        numeric(body.total_cpu),
        numeric(body.total_ram_mb),
        numeric(body.total_disk_gb),
        reportedCapabilities ? JSON.stringify(reportedCapabilities) : null,
        reportedDraining,
        reportedWorkerStatus,
        reportedIngress ? JSON.stringify(reportedIngress) : null,
        reportedHostName,
      ],
    );

    // §31 worker.updated: the worker self-updated (WS-B self-update flow
    // restarts it after fetching a new release). Fires on an actual
    // version *change*; the very first report (no previous version) is a
    // registration, not an update.
    if (reportedWorkerVersion && prevWorkerVersion && reportedWorkerVersion !== prevWorkerVersion) {
      await appendEvent(pool, {
        type: 'worker.updated',
        actor_type: 'host',
        actor_id: req.auth!.name,
        host_id: hostId,
        payload: {
          previous_version: prevWorkerVersion,
          worker_version: reportedWorkerVersion,
        },
      });
      logger.info('host worker updated', {
        host: req.auth!.name,
        from: prevWorkerVersion,
        to: reportedWorkerVersion,
      });
    }

    if (revived) {
      await appendEvent(pool, {
        type: 'host.online',
        actor_type: 'host',
        actor_id: req.auth!.name,
        host_id: hostId,
        payload: { previous_status: prevStatus },
      });
      logger.info('host came online', { host: req.auth!.name, from: prevStatus });
    }

    // Worker-reported issues (e.g. crash loops) -> events, first appearance.
    await emitWorkerIssues(pool, hostId, req.auth!.name, body.issues);

    // pending_tasks: queued work this host may claim.
    const pending = await pool.query(
      `SELECT count(*)::int AS n FROM tasks
       WHERE status = 'queued' AND (assigned_to IS NULL OR assigned_to = $1)`,
      [hostId],
    );

    const hostRow = (
      await pool.query(
        `SELECT id, name, host_type, status, capabilities, worker_version,
                worker_draining, worker_status, ingress, reported_host_name,
                cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
                total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
                created_at, updated_at FROM hosts WHERE id = $1`,
        [hostId],
      )
    ).rows[0];

    // §61: outdated-worker detection. The effective version is what the
    // worker just reported, falling back to the stored row (a heartbeat
    // that omits worker_version must not clear a previously seen one).
    // The response carries the verdict every time so the worker itself can
    // see it; the host.worker_outdated event fires once per host per
    // process so operators get a single durable signal.
    const effectiveWorkerVersion =
      reportedWorkerVersion ??
      (typeof hostRow.worker_version === 'string' ? hostRow.worker_version : null);
    const workerOutdated =
      effectiveWorkerVersion !== null && isOutdatedWorkerVersion(effectiveWorkerVersion);
    if (workerOutdated && effectiveWorkerVersion !== null) {
      await emitWorkerOutdated(pool, hostId, req.auth!.name, effectiveWorkerVersion);
    }

    res.json({
      host: publicHost(hostRow),
      pending_tasks: pending.rows[0].n,
      api_version: API_VERSION,
      min_worker_version: MIN_WORKER_VERSION,
      worker_outdated: workerOutdated,
    });
  } catch (err) {
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
async function tryClaim(pool: Pool, hostId: string, leaseS: number) {
  const { rows } = await pool.query(
    `UPDATE tasks SET
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
         -- §37 defense in depth: even if a draining host slips past the
         -- handler-level gate (e.g. it started draining mid-wait), the
         -- atomic claim itself refuses it. The worker pauses its own
         -- claim loop on drain; this is the server-side backstop.
         AND EXISTS (
           SELECT 1 FROM hosts h
           WHERE h.id = $1 AND h.status <> 'draining' AND NOT COALESCE(h.worker_draining, false)
         )
       ORDER BY priority DESC, created_at ASC
       LIMIT 1
       FOR UPDATE SKIP LOCKED
     )
     RETURNING *`,
    [hostId, leaseS],
  );
  return rows[0] ?? null;
}

workerRouter.post('/tasks/claim', requireHost, async (req, res, next) => {
  try {
    const hostId = (req.body ?? {}).host_id;
    if (typeof hostId !== 'string' || !isUuid(hostId)) {
      sendError(res, 400, 'bad_request', 'host_id (UUID) is required in the body');
      return;
    }
    if (hostId !== req.auth!.id) {
      sendError(res, 403, 'forbidden', 'host token does not match host_id');
      return;
    }
    let wait = Number(req.query.wait ?? 0);
    if (!Number.isFinite(wait) || wait < 0) wait = 0;
    wait = Math.min(Math.floor(wait), 30);

    const pool = getPool();
    // §37: a draining host must not claim new work. Server-side gate —
    // the worker pauses its own claim loop on drain (WS-B), but an
    // old/buggy worker might not. Refuse immediately with a clear 409
    // rather than long-polling a wait the host may never use.
    const hostState = (
      await pool.query('SELECT status, worker_draining FROM hosts WHERE id = $1', [hostId])
    ).rows[0];
    if (hostState && (hostState.status === 'draining' || hostState.worker_draining === true)) {
      sendError(res, 409, 'conflict', 'host is draining; it cannot claim new tasks');
      return;
    }
    const leaseS = readTaskSweeperConfig().leaseS;
    const deadline = Date.now() + wait * 1000;
    let claimed: Record<string, unknown> | null = null;
    let clientGone = false;
    req.on('close', () => {
      clientGone = true;
    });
    for (;;) {
      claimed = await tryClaim(pool, hostId, leaseS);
      if (claimed || clientGone) break;
      if (Date.now() >= deadline) break;
      await sleep(1000);
    }
    if (!claimed) {
      res.status(204).end();
      return;
    }
    await appendEvent(pool, {
      type: 'task.claimed',
      actor_type: 'host',
      actor_id: req.auth!.name,
      task_id: claimed.id as string,
      host_id: hostId,
      payload: { type: claimed.type, attempts: claimed.attempts },
    });
    logger.info('task claimed', { task: claimed.id, host: req.auth!.name });
    res.json({ task: claimed });
  } catch (err) {
    next(err);
  }
});

// ---------------------------------------------------------------------------
// POST /v1/worker/tasks/:id/progress — HOST token only; only the claiming
// host may report. Validates the transition via the state machine, appends
// log_chunk to LOG_DIR/<task_id>.log, sets completed_at on terminal states,
// mirrors deployment status for deploy/rollback/service tasks, emits events.
// ---------------------------------------------------------------------------
const PROGRESS_STATUSES = ['claimed', 'running', 'awaiting_approval', 'completed', 'failed'] as const;

// Task types that own a deployment's lifecycle status. A failed `logs`
// probe or `ingress-sync` must never mark its deployment failed — only
// these types mirror into deployments.status (§5).
const DEPLOYMENT_OWNING_TASK_TYPES: ReadonlySet<string> = new Set([
  'deploy',
  'rollback',
  'restart',
  'stop',
  'start',
  'remove',
  'environment-update',
]);

async function mirrorDeploymentState(
  pool: Pool,
  task: Record<string, any>,
  nextStatus: string,
  actorName: string,
  reportedResult?: Record<string, any> | null,
): Promise<void> {
  // §5: read-only / auxiliary task types (logs, status, healthcheck,
  // system-info, artifact-*, build, docker-*, ingress-sync) must not move
  // deployments.status. Before this guard, a failed `logs` task flipped
  // its deployment to `failed`.
  if (!DEPLOYMENT_OWNING_TASK_TYPES.has(task.type)) return;

  const payload = (task.payload ?? {}) as Record<string, any>;
  const deploymentId = payload.deployment_id;
  if (typeof deploymentId !== 'string' || !isUuid(deploymentId)) return;

  const isRollbackTask = task.type === 'rollback' || (task.type === 'deploy' && payload.rollback === true);
  const serviceEvent =
    task.type === 'restart' ? 'service.restarted' : task.type === 'stop' ? 'service.stopped' : task.type === 'start' ? 'service.started' : null;

  // The deployment row is locked (FOR UPDATE) for the read-decide-write
  // below: two concurrent progress reports for different tasks of the
  // same deployment serialize here instead of tearing each other's
  // read-modify-write (§74). Pure-DB writes happen inside the
  // transaction; Cloudflare-touching hooks run after commit (they are
  // already failure-isolated) so no row lock is held across the network.
  const client = await pool.connect();
  let depStatus: string | null = null;
  let depEvent: { type: string; payload: Record<string, unknown> } | null = null;
  let reportedPorts: Record<string, unknown> | null = null;
  try {
    await client.query('BEGIN');
    const { rows } = await client.query('SELECT * FROM deployments WHERE id = $1 FOR UPDATE', [
      deploymentId,
    ]);
    const deployment = rows[0];
    if (!deployment) {
      await client.query('ROLLBACK');
      return;
    }

    if (nextStatus === 'running') {
      if (['requested', 'approved'].includes(deployment.status)) {
        depStatus = task.type === 'deploy' && !isRollbackTask ? 'building' : deployment.status;
        depEvent =
          task.type === 'deploy' && !isRollbackTask
            ? { type: 'deployment.started', payload: { version: deployment.version } }
            : null;
      }
    } else if (nextStatus === 'completed') {
      if (isRollbackTask) {
        depStatus = 'rolled_back';
        depEvent = {
          type: 'deployment.rolled_back',
          payload: {
            version: deployment.version,
            rolled_back_to: payload.target_deployment_id ?? null,
          },
        };
      } else if (task.type === 'deploy') {
        depStatus = 'running';
        depEvent = { type: 'deployment.completed', payload: { version: deployment.version } };
      } else if (task.type === 'remove') {
        depStatus = 'stopped';
        depEvent = { type: 'service.removed', payload: { version: deployment.version } };
      } else if (serviceEvent) {
        depStatus = task.type === 'stop' ? 'stopped' : 'running';
        depEvent = { type: serviceEvent, payload: { version: deployment.version } };
      }
      // environment-update completed: the deployment was already running
      // and still is — no status change, no event (unchanged behavior).
    } else if (nextStatus === 'failed') {
      if (task.type === 'rollback') {
        // A failed rollback leaves the deployment where it was; the failure
        // is recorded as an event, not a deployment status.
        depEvent = { type: 'deployment.rollback_failed', payload: { version: deployment.version } };
      } else {
        depStatus = 'failed';
        // §5: a deployment moving to `failed` always gets its event. (A
        // failed restart/stop used to flip the status silently.)
        depEvent = { type: 'deployment.failed', payload: { version: deployment.version } };
      }
    }

    // Only write when the status actually changes: a duplicate or stale
    // report for an already-resolved deployment is a no-op instead of a
    // duplicate event + duplicate hook run.
    const changed = depStatus !== null && depStatus !== deployment.status;
    if (changed) {
      await client.query(
        `UPDATE deployments SET status = $2, health_status = CASE WHEN $2 = 'running' THEN 'healthy' WHEN $2 = 'failed' THEN 'unhealthy' ELSE health_status END WHERE id = $1`,
        [deploymentId, depStatus],
      );
    }

    // The worker reports the real host->container port mapping in
    // task.result.ports on a completed deploy — persist it on the deployment
    // row (this is what the deployments.ports JSONB column is for).
    if (
      changed &&
      task.type === 'deploy' &&
      !isRollbackTask &&
      nextStatus === 'completed' &&
      reportedResult &&
      typeof reportedResult === 'object' &&
      reportedResult.ports &&
      typeof reportedResult.ports === 'object'
    ) {
      reportedPorts = reportedResult.ports as Record<string, unknown>;
      await client.query('UPDATE deployments SET ports = $2 WHERE id = $1', [
        deploymentId,
        JSON.stringify(reportedPorts),
      ]);
    }

    // Port registry lifecycle, inside the same transaction as the status
    // write so the registry can never disagree with the deployment row.
    if (changed && depStatus && TERMINAL_PORT_RELEASE_STATUSES.has(depStatus)) {
      await releasePortAllocations(client, deploymentId);
    }
    if (changed && task.type === 'deploy' && !isRollbackTask && nextStatus === 'completed') {
      await releaseSupersededPortAllocations(client, deployment.project_id, deployment.host_id, deploymentId);
    }
    await client.query('COMMIT');

    if (depEvent && (changed || depStatus === null)) {
      await appendEvent(pool, {
        type: depEvent.type,
        actor_type: 'host',
        actor_id: actorName,
        task_id: task.id,
        deployment_id: deploymentId,
        host_id: task.claimed_by,
        payload: depEvent.payload,
      });
    }

    // Domain lifecycle hooks run after commit (§50: a single PG transaction
    // cannot cover the Cloudflare calls inside these hooks, and they must
    // never take down progress reporting — failures are logged, not
    // raised). They only run when the deployment status actually changed.
    if (changed && depStatus && ['failed', 'stopped', 'rolled_back'].includes(depStatus)) {
      try {
        const failed = await failDomainsForDeployment(pool, deploymentId, `deployment ${depStatus}`);
        if (failed > 0) {
          logger.info('domains failed with deployment', { deployment: deploymentId, depStatus, failed });
        }
      } catch (err) {
        logger.warn('failDomainsForDeployment hook failed', {
          deployment: deploymentId,
          err: err instanceof Error ? err.message : String(err),
        });
      }
    }
    if (changed && depStatus === 'running' && (task.type === 'deploy' || task.type === 'start')) {
      try {
        const healed = await reprovisionDomainsForDeployment(pool, deploymentId);
        if (healed > 0) {
          logger.info('domains reprovisioned with deployment', { deployment: deploymentId, healed });
        }
      } catch (err) {
        logger.warn('reprovisionDomainsForDeployment hook failed', {
          deployment: deploymentId,
          err: err instanceof Error ? err.message : String(err),
        });
      }
    }
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

workerRouter.post('/tasks/:id/progress', requireHost, async (req, res, next) => {
  try {
    const taskId = req.params.id;
    if (!isUuid(taskId)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const { status, log_chunk, result, error } = req.body ?? {};
    if (typeof status !== 'string' || !(PROGRESS_STATUSES as readonly string[]).includes(status)) {
      sendError(res, 400, 'bad_request', `status must be one of: ${PROGRESS_STATUSES.join(', ')}`);
      return;
    }
    if (log_chunk !== undefined && typeof log_chunk !== 'string') {
      sendError(res, 400, 'bad_request', 'log_chunk must be a string');
      return;
    }
    if (result !== undefined && (typeof result !== 'object' || result === null || Array.isArray(result))) {
      sendError(res, 400, 'bad_request', 'result must be an object');
      return;
    }
    if (error !== undefined && typeof error !== 'string') {
      sendError(res, 400, 'bad_request', 'error must be a string');
      return;
    }

    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM tasks WHERE id = $1', [taskId]);
    const task = rows[0];
    if (!task) {
      next(new HttpError(404, 'not_found', 'task not found'));
      return;
    }
    if (task.claimed_by !== req.auth!.id) {
      sendError(res, 403, 'forbidden', 'only the claiming host may report progress on this task');
      return;
    }

    // A worker-reported `failed` is not necessarily terminal: the retry
    // policy may park the task in `retrying` (the stuck-task sweeper later
    // returns it to `queued`) when the budget allows and the task type is
    // safe to re-run from where it failed.
    let finalStatus = status;
    if (status === 'failed') {
      const outcome = decideRetryOutcome({
        type: task.type,
        failedFrom: task.status,
        attempts: task.attempts,
        maxAttempts: task.max_attempts,
      });
      if (outcome === 'retry') finalStatus = 'retrying';
    }

    if (!canTransitionTask(task.status, finalStatus)) {
      sendError(res, 409, 'conflict', `cannot transition task from ${task.status} to ${finalStatus}`);
      return;
    }

    if (log_chunk) {
      const dir = logDir();
      await mkdir(dir, { recursive: true });
      await appendFile(join(dir, `${taskId}.log`), log_chunk, 'utf8');
    }

    const terminal = isTerminalTaskStatus(finalStatus);
    const leaseS = readTaskSweeperConfig().leaseS;
    // Compare-and-swap (§5/§74): the task may have moved between the read
    // above and this UPDATE (sweeper requeue, concurrent progress report,
    // cancel). Without the status guard, a late/duplicate report could
    // resurrect a terminal task (e.g. completed -> running) or clobber a
    // requeue. Zero rows updated => the state moved on; the worker must
    // re-read the task instead of assuming its report landed.
    const updated = (
      await pool.query(
        `UPDATE tasks SET
           status = $2,
           result = COALESCE($3, result),
           error = COALESCE($4, error),
           started_at = CASE WHEN $2 = 'running' AND started_at IS NULL THEN now() ELSE started_at END,
           completed_at = CASE WHEN $5 THEN now() ELSE completed_at END,
           last_progress_at = now(),
           lease_expires_at = CASE WHEN $5 THEN NULL ELSE now() + ($6 || ' seconds')::interval END
         WHERE id = $1 AND status = $7 RETURNING *`,
        [
          taskId,
          finalStatus,
          result !== undefined ? JSON.stringify(result) : null,
          error !== undefined ? error : null,
          terminal,
          leaseS,
          task.status,
        ],
      )
    ).rows[0];
    if (!updated) {
      sendError(
        res,
        409,
        'conflict',
        `task changed state concurrently (was ${task.status}); re-read the task before reporting again`,
      );
      return;
    }

    const eventFor: Record<string, string> = {
      running: 'task.started',
      awaiting_approval: 'task.awaiting_approval',
      completed: 'task.completed',
      failed: 'task.failed',
      retrying: 'task.retrying',
    };
    if (eventFor[finalStatus]) {
      await appendEvent(pool, {
        type: eventFor[finalStatus],
        actor_type: 'host',
        actor_id: req.auth!.name,
        task_id: taskId,
        host_id: task.claimed_by,
        payload:
          finalStatus === 'failed'
            ? { error: error ?? null }
            : finalStatus === 'retrying'
              ? { error: error ?? null, attempts: updated.attempts, max_attempts: updated.max_attempts }
              : {},
      });
    }

    await mirrorDeploymentState(
      pool,
      task,
      finalStatus,
      req.auth!.name,
      typeof result === 'object' && result !== null && !Array.isArray(result) ? result : null,
    );

    res.json({ task: updated });
  } catch (err) {
    next(err);
  }
});

// ---------------------------------------------------------------------------
// GET /v1/worker/domains — HOST token only.
// Every ACTIVE tunnel-mode domain on this host's routable deployments, so
// the worker can rebuild its LOCAL ingress route mirror (Phase 7):
//   {domains: [{deployment_id, hostname, ingress}]}
// Source of truth is the `domains` table (migration 007); only 'active'
// rows are returned, so the worker never routes a failed/removed domain.
// NOTE: this mirror does not change routing for token-based tunnels — the
// authoritative route table is the remote tunnel configuration, which the
// control plane writes via the Cloudflare API. See docs/cloudflare.md.
// ---------------------------------------------------------------------------
workerRouter.get('/domains', requireHost, async (req, res, next) => {
  try {
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT d.deployment_id, d.hostname, d.ingress
       FROM domains d
       JOIN deployments dep ON dep.id = d.deployment_id
       WHERE dep.host_id = $1
         AND d.status = 'active'
         AND d.ingress = 'tunnel'
         AND NOT (dep.status = ANY($2))`,
      [req.auth!.id, ['failed', 'rolled_back', 'stopped', 'stopping']],
    );
    res.json({
      domains: rows.map((r) => ({
        deployment_id: r.deployment_id,
        hostname: r.hostname,
        ingress: r.ingress,
      })),
    });
  } catch (err) {
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
export async function hostMayReadProjectSecrets(
  pool: Pool,
  hostId: string,
  projectId: string,
): Promise<boolean> {
  const { rows } = await pool.query(
    `SELECT 1 FROM tasks
      WHERE claimed_by = $1 AND payload->>'project_id' = $2
        AND status IN ('claimed', 'running', 'awaiting_approval')
     UNION
     SELECT 1 FROM deployments
      WHERE host_id = $1 AND project_id = $2
        AND status IN ('requested', 'approved', 'building', 'starting',
                       'healthcheck', 'running')
     LIMIT 1`,
    [hostId, projectId],
  );
  return rows.length > 0;
}

workerRouter.get('/projects/:project_id/secrets', requireHost, async (req, res, next) => {
  try {
    const projectId = req.params.project_id;
    if (!isUuid(projectId)) {
      sendError(res, 400, 'bad_request', 'project_id must be a UUID');
      return;
    }
    const pool = getPool();
    const project = await pool.query('SELECT id FROM projects WHERE id = $1', [projectId]);
    if (!project.rows[0]) {
      next(new HttpError(404, 'not_found', 'project not found'));
      return;
    }
    const allowed = await hostMayReadProjectSecrets(pool, req.auth!.id, projectId);
    if (!allowed) {
      sendError(res, 403, 'forbidden', 'no active task or deployment for this project on this host');
      return;
    }
    const secrets = await getDecryptedProjectSecrets(pool, projectId);
    res.json({ secrets });
  } catch (err) {
    next(err);
  }
});
