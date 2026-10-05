"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TERMINAL_PORT_RELEASE_STATUSES = void 0;
exports.isValidServicePort = isValidServicePort;
exports.allocatePort = allocatePort;
exports.releasePortAllocations = releasePortAllocations;
exports.releaseSupersededPortAllocations = releaseSupersededPortAllocations;
const log_1 = require("./log");
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
exports.TERMINAL_PORT_RELEASE_STATUSES = new Set([
    'failed',
    'rolled_back',
    'stopped',
]);
function isValidServicePort(port) {
    return typeof port === 'number' && Number.isInteger(port) && port >= 1 && port <= 65535;
}
/** Reserve (host_id, port) for a deployment. Unique violation -> 23505. */
async function allocatePort(db, hostId, port, deploymentId) {
    await db.query(`INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, $2, $3)`, [hostId, port, deploymentId]);
    log_1.logger.info('port allocated', { host: hostId, port, deployment: deploymentId });
}
/** Release every reservation held by a deployment. Returns rows deleted. */
async function releasePortAllocations(db, deploymentId) {
    const { rowCount } = await db.query(`DELETE FROM port_allocations WHERE deployment_id = $1`, [
        deploymentId,
    ]);
    const n = rowCount ?? 0;
    if (n > 0)
        log_1.logger.info('port allocations released', { deployment: deploymentId, count: n });
    return n;
}
/**
 * A deployment of project+host reached `running`: every other deployment of
 * the same project+host has been superseded (its container was stopped by
 * the worker), so its port reservations are stale — release them.
 */
async function releaseSupersededPortAllocations(db, projectId, hostId, keepDeploymentId) {
    if (!hostId)
        return 0;
    const { rowCount } = await db.query(`DELETE FROM port_allocations
     WHERE host_id = $1
       AND deployment_id <> $2
       AND deployment_id IN (
         SELECT id FROM deployments WHERE project_id = $3 AND host_id = $1
       )`, [hostId, keepDeploymentId, projectId]);
    const n = rowCount ?? 0;
    if (n > 0)
        log_1.logger.info('superseded port allocations released', { project: projectId, count: n });
    return n;
}
