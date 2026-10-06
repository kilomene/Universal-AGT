import { randomUUID } from 'crypto';
import { Router, type Response } from 'express';
import type { Pool } from 'pg';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import {
  decideIdempotency,
  deploymentIdempotencyBody,
  findTaskByIdempotencyKey,
  taskIdempotencyBody,
} from '../lib/idempotency';
import { logger } from '../lib/log';
import { allocatePort, isValidServicePort, settleRollbackPorts } from '../lib/ports';
import { selectRollbackTarget } from '../lib/rollback';
import {
  authorizeDeploymentAccess,
  authorizeHostAccess,
  authorizeProjectAccess,
  inList,
  listAccessibleProjectIds,
} from '../lib/authz';
import { checkHostCapacity, deploymentConstraintsForManifest, deploymentConstraintsForProject, isHostSchedulable, selectHostForDeployment } from '../lib/scheduler';
import { requireAgent, requireHost, requirePermission } from '../middleware/auth';
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

// Resolve an existing deployment row for an idempotency key into the
// replay/conflict response. Returns true when it responded (the caller
// must return), false when no row exists for the key.
//
// Used in two places: the pre-transaction fast path, and the 23505 catch
// after the INSERT — two requests racing with the same key can both miss
// the fast path; the loser then replays the winner's row instead of
// failing with a generic conflict (spec §42: exactly one deployment).
async function resolveIdempotencyReplay(
  pool: Pool,
  res: Response,
  idempotencyKey: string,
  incomingBody: unknown,
): Promise<boolean> {
  const { rows } = await pool.query(
    'SELECT * FROM deployments WHERE idempotency_key = $1',
    [idempotencyKey],
  );
  const existing = rows[0];
  if (!existing) return false;
  // host_port is part of the compared body: it lives in the port
  // registry, not on the deployment row.
  const alloc = (
    await pool.query('SELECT port FROM port_allocations WHERE deployment_id = $1', [existing.id])
  ).rows[0];
  const existingBody = deploymentIdempotencyBody({
    project_id: existing.project_id,
    host_id: existing.host_id,
    version: existing.version,
    artifact_id: existing.artifact_id,
    mode: existing.mode,
    host_port: alloc ? alloc.port : null,
  }) as Record<string, unknown>;
  const incoming = (incomingBody ?? {}) as Record<string, unknown>;
  // §8: host_id:null in the request means "any capable host" — the
  // scheduler's concrete selection is not a requested parameter, so an
  // unpinned retry must replay instead of 409-ing against the selected
  // host. An explicitly pinned host_id is still compared strictly.
  if (incoming.host_id === null || incoming.host_id === undefined) {
    delete existingBody.host_id;
    delete incoming.host_id;
  }
  const decision = decideIdempotency({ body: existingBody }, incoming);
  if (decision === 'replay') {
    const task = await latestTaskForDeployment(pool, existing);
    res.status(200).json({ deployment: existing, task, idempotent_replay: true });
    return true;
  }
  sendError(res, 409, 'conflict', 'idempotency key already used with different deployment parameters');
  return true;
}

// POST /v1/deployments — creates a deployment row (status requested) AND a
// type=deploy task whose payload includes deployment_id. The idempotency_key
// dedupes across BOTH rows.
deploymentsRouter.post('/', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    const { project_id, host_id, version, artifact_id, mode, idempotency_key, host_port } = req.body ?? {};
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
    // Optional fixed host port: reserved in the port registry for this
    // deployment. The worker verifies it is actually free before `docker
    // run` and fails the task with a clear error on collision.
    let requestedHostPort: number | null = null;
    if (host_port !== undefined && host_port !== null) {
      if (!isValidServicePort(host_port)) {
        sendError(res, 400, 'bad_request', 'host_port must be an integer 1-65535');
        return;
      }
      if (!host_id) {
        sendError(res, 422, 'unprocessable', 'host_port requires host_id — a port can only be reserved on a specific host');
        return;
      }
      requestedHostPort = host_port;
    }

    const pool = getPool();
    // §10: the agent must have access to the project (owner or ACL member).
    // This also 404s when the project does not exist.
    await authorizeProjectAccess(pool, req.auth!.id, project_id);
    const project = (await pool.query(
      'SELECT id, name, runtime, configuration FROM projects WHERE id = $1',
      [project_id],
    )).rows[0];
    // §10: an explicitly targeted host needs host-targeting access.
    let requestedHostId: string | null = null;
    if (host_id) {
      await authorizeHostAccess(pool, req.auth!.id, host_id);
      requestedHostId = host_id;
    }
    // The worker refuses to deploy an artifact without artifact_checksum in
    // the task payload (verified before any docker call), so the control
    // plane injects the verified checksum + size from the artifact row.
    let artifactChecksum: string | null = null;
    let artifactSize: number | null = null;
    // Fix #1: the artifact's validated agent.deploy.json manifest, when
    // present, is the authoritative scheduling source — it is the exact
    // definition the worker will deploy.
    let artifactManifest: unknown = null;
    if (artifact_id) {
      const art = await pool.query('SELECT id, project_id, status, checksum, size, manifest FROM artifacts WHERE id = $1', [artifact_id]);
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
      if (typeof art.rows[0].checksum !== 'string' || !art.rows[0].checksum) {
        sendError(res, 422, 'unprocessable', 'artifact has no verified checksum; re-upload it');
        return;
      }
      artifactChecksum = art.rows[0].checksum;
      artifactSize = art.rows[0].size === null || art.rows[0].size === undefined
        ? null : Number(art.rows[0].size);
      artifactManifest = art.rows[0].manifest ?? null;
    }

    const incomingBody = deploymentIdempotencyBody({
      project_id,
      host_id,
      version,
      artifact_id,
      mode: deployMode,
      host_port: requestedHostPort,
    });

    if (typeof idempotency_key === 'string' && idempotency_key) {
      if (await resolveIdempotencyReplay(pool, res, idempotency_key, incomingBody)) {
        return;
      }
    }

    const deploymentId = randomUUID();
    const taskStatus = deployMode === 'manual' ? 'awaiting_approval' : 'queued';
    // Fix #1: the resource reservation must represent the exact resources
    // the worker will deploy. The artifact's validated agent.deploy.json
    // manifest is authoritative when present; projects.configuration is
    // only the fallback for deployments without an artifact or with a
    // legacy (manifest-less) artifact.
    const constraints = artifactManifest !== null && artifactManifest !== undefined
      ? deploymentConstraintsForManifest(artifactManifest, project.runtime)
      : deploymentConstraintsForProject(project);
    const taskPayload: Record<string, unknown> = {
      project_id,
      project_name: project.name, // the worker requires project_name
      host_id: null, // filled with the selected host inside the transaction
      version,
      artifact_id: artifact_id ?? null,
      deployment_id: deploymentId,
      // Fix #1: the canonical reserved contract travels with the task so
      // the worker can enforce "tarball manifest <= reservation" at deploy
      // time (fail loudly instead of silently over-committing the host).
      reserved_cpu: constraints.requiredCpuCores,
      reserved_ram_mb: constraints.requiredRamMb,
    };
    if (requestedHostPort !== null) {
      taskPayload.requested_host_port = requestedHostPort;
    }
    if (artifactChecksum !== null) {
      taskPayload.artifact_checksum = artifactChecksum;
      if (artifactSize !== null) {
        taskPayload.artifact_size = artifactSize;
      }
    }

    const client = await pool.connect();
    let deployment: Record<string, any>;
    let task: Record<string, any>;
    try {
      await client.query('BEGIN');
      // §8: host_id omitted -> the control plane selects an eligible host.
      // §9: the selection runs inside this transaction with the candidate
      // host rows locked FOR UPDATE, so the eligibility check and the
      // reservation below are atomic.
      let selectedHostId = requestedHostId;
      if (!selectedHostId) {
        selectedHostId = await selectHostForDeployment(client, {
          agentId: req.auth!.id,
          requiredCapabilities: constraints.requiredCapabilities,
          requiredCpuCores: constraints.requiredCpuCores,
          requiredRamMb: constraints.requiredRamMb,
        });
        if (!selectedHostId) {
          await client.query('ROLLBACK');
          sendError(
            res,
            503,
            'no_capacity',
            'no eligible host is available for this deployment (no online host matches status, capabilities, resources, and access)',
          );
          return;
        }
      } else {
        // §9: lock the explicitly requested host row too, so the check and
        // the deployment-row reservation are atomic for targeted deploys.
        const h = await client.query(
          'SELECT id, status, worker_draining, total_cpu, total_ram_mb FROM hosts WHERE id = $1 FOR UPDATE',
          [selectedHostId],
        );
        if (!h.rows[0]) {
          await client.query('ROLLBACK');
          sendError(res, 404, 'not_found', 'host not found');
          return;
        }
        // Part 1F/1G: a draining host takes no new deployments — the same
        // rule as auto-select. A deploy task pinned to a draining host
        // could never be claimed (§37), so creating it would only mint a
        // dead deployment. Draining never destroys existing apps or tasks.
        if (!isHostSchedulable(h.rows[0])) {
          await client.query('ROLLBACK');
          sendError(res, 503, 'no_capacity', 'host is draining; it cannot take new deployments');
          return;
        }
        // Part 1D: the reservation is atomic for targeted deploys too —
        // verify capacity against persistent reservations while the host
        // row is locked, in this same transaction.
        if (
          !(await checkHostCapacity(
            client,
            {
              id: selectedHostId,
              total_cpu: h.rows[0].total_cpu,
              total_ram_mb: h.rows[0].total_ram_mb,
            },
            constraints.requiredCpuCores,
            constraints.requiredRamMb,
          ))
        ) {
          await client.query('ROLLBACK');
          sendError(
            res,
            503,
            'no_capacity',
            'host does not have enough reservable CPU/RAM for this deployment',
          );
          return;
        }
      }
      taskPayload.host_id = selectedHostId;
      const depRows = await client.query(
        `INSERT INTO deployments (id, idempotency_key, project_id, host_id, version, artifact_id, mode, status,
                                  reserved_cpu, reserved_ram_mb)
         VALUES ($1,$2,$3,$4,$5,$6,$7,'requested',$8,$9) RETURNING *`,
        [
          deploymentId,
          typeof idempotency_key === 'string' && idempotency_key ? idempotency_key : null,
          project_id,
          selectedHostId,
          version,
          artifact_id ?? null,
          deployMode,
          // Part 1D: the persistent reservation — snapshotted here, inside
          // the same transaction that selected and locked the host.
          constraints.requiredCpuCores,
          constraints.requiredRamMb,
        ],
      );
      deployment = depRows.rows[0];
      if (requestedHostPort !== null) {
        // host_id is guaranteed non-null here (validated above).
        try {
          await allocatePort(client, selectedHostId, requestedHostPort, deploymentId);
        } catch (portErr) {
          await client.query('ROLLBACK');
          if ((portErr as { code?: string }).code === '23505') {
            sendError(res, 409, 'conflict', `host port ${requestedHostPort} is already allocated on this host`);
            return;
          }
          throw portErr;
        }
      }
      const taskRows = await client.query(
        `INSERT INTO tasks (idempotency_key, created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,$3,'deploy',$4,$5) RETURNING *`,
        [
          typeof idempotency_key === 'string' && idempotency_key ? `deploy:${idempotency_key}` : null,
          req.auth!.id,
          selectedHostId,
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
        // Idempotency race: a concurrent request with the same key passed
        // the pre-check and won the INSERT first (same handling POST
        // /v1/tasks already has). Re-read the winner and replay/conflict
        // instead of 409-ing a duplicate request — the deployment still
        // exists exactly once either way.
        if (typeof idempotency_key === 'string' && idempotency_key) {
          try {
            if (await resolveIdempotencyReplay(pool, res, idempotency_key, incomingBody)) {
              return;
            }
          } catch {
            // fall through to the generic conflict below
          }
        }
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
      host_id: deployment.host_id,
      payload: {
        version,
        mode: deployMode,
        host_port: requestedHostPort,
        host_selected: !requestedHostId,
      },
    });
    await appendEvent(pool, {
      type: 'task.created',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: deployment.host_id,
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
    // §10: only deployments of projects the agent may access.
    const accessibleIds = await listAccessibleProjectIds(getPool(), req.auth!.id);
    if (accessibleIds.length === 0) {
      res.json({ deployments: [] });
      return;
    }
    conds.push(inList('project_id', accessibleIds, params) as string);
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
    // §10: via deployment -> project -> owner/ACL.
    await authorizeDeploymentAccess(pool, req.auth!.id, req.params.id);
    const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [req.params.id]);
    const deployment = rows[0];
    const task = await latestTaskForDeployment(pool, deployment);
    res.json({ deployment, task });
  } catch (err) {
    next(err);
  }
});

// POST /v1/deployments/:id/rollback — needs deploy.
//
// Idempotent via an optional `idempotency_key` (§7): rollback is a
// safely-retryable operation, so a retried request (timeout, client retry,
// agent retry) with the same key replays the original rollback task
// instead of queueing a second rollback. Same key + different body -> 409.
deploymentsRouter.post('/:id/rollback', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const { idempotency_key } = req.body ?? {};
    if (idempotency_key !== undefined && typeof idempotency_key !== 'string') {
      sendError(res, 400, 'bad_request', 'idempotency_key must be a string');
      return;
    }
    const idemKey = typeof idempotency_key === 'string' && idempotency_key ? idempotency_key : null;
    const pool = getPool();
    // §10: via deployment -> project -> owner/ACL.
    await authorizeDeploymentAccess(pool, req.auth!.id, req.params.id);
    const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [req.params.id]);
    const current = rows[0];
    const candidates = (
      await pool.query(
        `SELECT * FROM deployments
         WHERE project_id = $1 AND host_id = $2 AND id <> $3 AND status = 'running'`,
        [current.project_id, current.host_id, current.id],
      )
    ).rows;
    const prev = selectRollbackTarget(current, candidates);
    if (!prev) {
      sendError(res, 409, 'conflict', 'no previous healthy deployment of this project+host to roll back to');
      return;
    }

    const taskPayload = {
      deployment_id: current.id,
      target_deployment_id: prev.id,
    };
    const incomingBody = taskIdempotencyBody({ type: 'rollback', payload: taskPayload });

    // Idempotency fast path: same key + equal body -> replay the original
    // task; same key + different body -> 409.
    if (idemKey) {
      const existing = await findTaskByIdempotencyKey(pool, idemKey);
      if (existing) {
        const decision = decideIdempotency(
          { body: taskIdempotencyBody(existing) },
          incomingBody,
        );
        if (decision === 'replay') {
          const { rows: trows } = await pool.query('SELECT * FROM tasks WHERE id = $1', [
            existing.id,
          ]);
          res.status(200).json({ deployment: current, task: trows[0], idempotent_replay: true });
          return;
        }
        sendError(res, 409, 'conflict', 'idempotency key already used with a different payload');
        return;
      }
    }

    let task: Record<string, any>;
    try {
      const taskRows = await pool.query(
        `INSERT INTO tasks (idempotency_key, created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,$3,'rollback','queued',$4) RETURNING *`,
        [idemKey, req.auth!.id, current.host_id, JSON.stringify(taskPayload)],
      );
      task = taskRows.rows[0];
    } catch (err) {
      // Idempotency race: two rollbacks with the same key passed the
      // pre-check concurrently — the unique constraint picked the winner.
      // Re-read and replay/conflict instead of 500.
      if ((err as { code?: string }).code === '23505' && idemKey) {
        const existing = await findTaskByIdempotencyKey(pool, idemKey);
        if (existing) {
          const decision = decideIdempotency(
            { body: taskIdempotencyBody(existing) },
            incomingBody,
          );
          if (decision === 'replay') {
            const { rows: trows } = await pool.query('SELECT * FROM tasks WHERE id = $1', [
              existing.id,
            ]);
            res.status(200).json({ deployment: current, task: trows[0], idempotent_replay: true });
            return;
          }
          sendError(res, 409, 'conflict', 'idempotency key already used with a different payload');
          return;
        }
      }
      throw err;
    }

    await appendEvent(pool, {
      type: 'deployment.rollback_requested',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      deployment_id: current.id,
      host_id: current.host_id,
      payload: {
        version: current.version,
        target_deployment_id: prev.id,
        target_version: prev.version,
      },
    });
    await appendEvent(pool, {
      type: 'task.created',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      task_id: task.id,
      host_id: current.host_id,
      payload: { type: 'rollback' },
    });
    logger.info('rollback task created', { deployment: current.id, task: task.id, target: prev.id });
    res.status(201).json({ deployment: current, task });
  } catch (err) {
    next(err);
  }
});

// POST /v1/deployments/:id/settle-rollback-ports — HOST token only; the
// host must own the deployment. Called by the worker after it completes a
// rollback: releases the rolled-back deployment's port reservations and
// re-reserves the rollback target's host ports for the target deployment
// (its reservation was dropped when the newer deployment superseded it,
// but its container is running again on those same ports). Best-effort
// from the worker's side — it never fails the rollback itself.
deploymentsRouter.post('/:id/settle-rollback-ports', requireHost, async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const { target_deployment_id } = req.body ?? {};
    if (!isUuid(target_deployment_id)) {
      sendError(res, 400, 'bad_request', 'target_deployment_id (UUID) is required');
      return;
    }
    const pool = getPool();
    const { rows } = await pool.query(
      'SELECT id, project_id, host_id FROM deployments WHERE id = $1',
      [req.params.id],
    );
    const deployment = rows[0];
    if (!deployment) {
      next(new HttpError(404, 'not_found', 'deployment not found'));
      return;
    }
    if (deployment.host_id !== req.auth!.id) {
      sendError(res, 403, 'forbidden', 'host token does not own this deployment');
      return;
    }
    const trows = (
      await pool.query('SELECT id, project_id, host_id FROM deployments WHERE id = $1', [
        target_deployment_id,
      ])
    ).rows;
    const target = trows[0];
    if (!target) {
      next(new HttpError(404, 'not_found', 'rollback target deployment not found'));
      return;
    }
    if (target.project_id !== deployment.project_id || target.host_id !== deployment.host_id) {
      sendError(res, 422, 'unprocessable', 'rollback target must belong to the same project and host');
      return;
    }
    const settlement = await settleRollbackPorts(pool, deployment.id, target.id);
    await appendEvent(pool, {
      type: 'deployment.ports_settled',
      actor_type: 'host',
      actor_id: req.auth!.name,
      deployment_id: deployment.id,
      host_id: deployment.host_id,
      payload: { target_deployment_id: target.id, ...settlement },
    });
    logger.info('rollback ports settled', {
      deployment: deployment.id,
      target: target.id,
      ...settlement,
    });
    res.json({
      deployment_id: deployment.id,
      target_deployment_id: target.id,
      ...settlement,
    });
  } catch (err) {
    next(err);
  }
});
