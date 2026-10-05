import type { Pool, PoolClient } from 'pg';
import { logger } from './log';

type Db = Pool | PoolClient;

// Port registry (table `port_allocations`): reserves a host port for a
// deployment so two deployments on the same host never claim the same port.
//
// Lifecycle:
//   * allocated in the POST /v1/deployments transaction when the request
//     carries an explicit `host_port` — the PRIMARY KEY (host_id, port)
//     makes concurrent allocations of the same port atomic: exactly one
//     INSERT wins, the other gets 23505 -> 409;
//   * the worker verifies the port is really free at OS level (bind test)
//     and Docker level (`docker ps` published-port scan) before `docker
//     run`; a collision fails the task with a clear error, the deployment
//     goes terminal, and the reservation is released (compensation for the
//     window between the DB reservation and the worker's physical check);
//   * released when the deployment reaches a terminal state
//     (failed | rolled_back | stopped);
//   * superseded deployments release their ports when a newer deployment of
//     the same project+host reaches `running` (the worker stops the old
//     container, so its port is free again);
//   * on rollback completion the rolled-back deployment's reservations are
//     released and the rollback TARGET's ports are re-reserved for it via
//     settleRollbackPorts (its reservation was dropped at supersede time,
//     but its container is running again on those same ports).

export const TERMINAL_PORT_RELEASE_STATUSES: ReadonlySet<string> = new Set([
  'failed',
  'rolled_back',
  'stopped',
]);

export function isValidServicePort(port: unknown): port is number {
  return typeof port === 'number' && Number.isInteger(port) && port >= 1 && port <= 65535;
}

/** Reserve (host_id, port) for a deployment. Unique violation -> 23505. */
export async function allocatePort(
  db: Db,
  hostId: string,
  port: number,
  deploymentId: string,
): Promise<void> {
  await db.query(
    `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, $2, $3)`,
    [hostId, port, deploymentId],
  );
  logger.info('port allocated', { host: hostId, port, deployment: deploymentId });
}

/** Release every reservation held by a deployment. Returns rows deleted. */
export async function releasePortAllocations(db: Db, deploymentId: string): Promise<number> {
  const { rowCount } = await db.query(`DELETE FROM port_allocations WHERE deployment_id = $1`, [
    deploymentId,
  ]);
  const n = rowCount ?? 0;
  if (n > 0) logger.info('port allocations released', { deployment: deploymentId, count: n });
  return n;
}

/**
 * A deployment of project+host reached `running`: every other deployment of
 * the same project+host has been superseded (its container was stopped by
 * the worker), so its port reservations are stale — release them.
 */
export async function releaseSupersededPortAllocations(
  db: Db,
  projectId: string,
  hostId: string | null,
  keepDeploymentId: string,
): Promise<number> {
  if (!hostId) return 0;
  const { rowCount } = await db.query(
    `DELETE FROM port_allocations
     WHERE host_id = $1
       AND deployment_id <> $2
       AND deployment_id IN (
         SELECT id FROM deployments WHERE project_id = $3 AND host_id = $1
       )`,
    [hostId, keepDeploymentId, projectId],
  );
  const n = rowCount ?? 0;
  if (n > 0) logger.info('superseded port allocations released', { project: projectId, count: n });
  return n;
}

export interface RollbackPortSettlement {
  released: number; // reservations dropped from the rolled-back deployment
  restored: number; // reservations re-created for the rollback target
  skipped: number; // target ports already held by someone else (conflict)
  ports: number[]; // target host ports that were restored
}

/**
 * Settle the port registry after a completed rollback.
 *
 * The rolled-back (new) deployment's reservations are released — its
 * container is gone — and the rollback TARGET's host ports are re-reserved
 * for it: its reservation was dropped when the newer deployment superseded
 * it, but its container is running again on those same ports. The target's
 * ports come from its `deployments.ports` JSONB (persisted from the
 * worker's deploy result). Runs in one transaction when handed a Pool; a
 * PoolClient is assumed to already be inside the caller's transaction.
 *
 * A target port already re-allocated to someone else is SKIPPED (counted,
 * not an error): the worker's physical port check is the backstop, and the
 * conflict is visible in the returned counts.
 */
export async function settleRollbackPorts(
  db: Db,
  rolledBackDeploymentId: string,
  targetDeploymentId: string,
): Promise<RollbackPortSettlement> {
  const ownsConnection = typeof (db as Pool).connect === 'function';
  const client = ownsConnection ? await (db as Pool).connect() : db;
  try {
    if (ownsConnection) await client.query('BEGIN');
    const target = (
      await client.query('SELECT host_id, ports FROM deployments WHERE id = $1', [
        targetDeploymentId,
      ])
    ).rows[0];
    if (!target) {
      throw new Error(`rollback target deployment ${targetDeploymentId} not found`);
    }
    const hostId: string | null = target.host_id ?? null;
    let targetPorts: number[] = [];
    try {
      const raw = target.ports;
      const obj = typeof raw === 'string' ? JSON.parse(raw) : raw;
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
        targetPorts = Object.keys(obj)
          .map((k) => Number(k))
          .filter((n) => Number.isInteger(n) && n >= 1 && n <= 65535);
      }
    } catch {
      targetPorts = [];
    }
    const released = await releasePortAllocations(client, rolledBackDeploymentId);
    let restored = 0;
    let skipped = 0;
    const ports: number[] = [];
    for (const port of targetPorts) {
      if (!hostId) break;
      await client.query(
        `INSERT INTO port_allocations (host_id, port, deployment_id)
         VALUES ($1, $2, $3) ON CONFLICT (host_id, port) DO NOTHING`,
        [hostId, port, targetDeploymentId],
      );
      // Count by reading back who actually holds the port rather than
      // trusting rowCount: some drivers/doubles report 1 for an
      // ON CONFLICT DO NOTHING no-op (real PostgreSQL reports 0).
      const holder = (
        await client.query(
          `SELECT deployment_id FROM port_allocations
           WHERE host_id = $1 AND port = $2`,
          [hostId, port],
        )
      ).rows[0]?.deployment_id;
      if (holder === targetDeploymentId) {
        restored += 1;
        ports.push(port);
      } else {
        skipped += 1;
      }
    }
    if (ownsConnection) await client.query('COMMIT');
    const settlement = { released, restored, skipped, ports };
    logger.info('rollback ports settled', {
      rolledBack: rolledBackDeploymentId,
      target: targetDeploymentId,
      ...settlement,
    });
    return settlement;
  } catch (err) {
    if (ownsConnection) await client.query('ROLLBACK');
    throw err;
  } finally {
    if (ownsConnection) (client as PoolClient).release();
  }
}
