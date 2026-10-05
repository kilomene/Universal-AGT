import type { Pool, PoolClient } from 'pg';
import { logger } from './log';

type Db = Pool | PoolClient;

// Port registry (table `port_allocations`): reserves a host port for a
// deployment so two deployments on the same host never claim the same port.
//
// Lifecycle:
//   * allocated in the POST /v1/deployments transaction when the request
//     carries an explicit `host_port`;
//   * released when the deployment reaches a terminal state
//     (failed | rolled_back | stopped);
//   * superseded deployments release their ports when a newer deployment of
//     the same project+host reaches `running` (the worker stops the old
//     container, so its port is free again).

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
