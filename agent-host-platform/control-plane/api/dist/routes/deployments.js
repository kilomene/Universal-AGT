"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.deploymentsRouter = void 0;
const crypto_1 = require("crypto");
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const idempotency_1 = require("../lib/idempotency");
const log_1 = require("../lib/log");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.deploymentsRouter = (0, express_1.Router)();
const MODES = ['automatic', 'manual'];
async function latestTaskForDeployment(pool, deployment) {
    const { rows } = await pool.query(`SELECT * FROM tasks
     WHERE id = $1 OR payload->>'deployment_id' = $2
     ORDER BY created_at DESC LIMIT 1`, [deployment.task_id, deployment.id]);
    return rows[0] ?? null;
}
// POST /v1/deployments — creates a deployment row (status requested) AND a
// type=deploy task whose payload includes deployment_id. The idempotency_key
// dedupes across BOTH rows.
exports.deploymentsRouter.post('/', auth_1.requireAgent, (0, auth_1.requirePermission)('deploy'), async (req, res, next) => {
    try {
        const { project_id, host_id, version, artifact_id, mode, idempotency_key } = req.body ?? {};
        if (!(0, _helpers_1.isUuid)(project_id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'project_id (UUID) is required');
            return;
        }
        if (host_id !== undefined && host_id !== null && !(0, _helpers_1.isUuid)(host_id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'host_id must be a UUID');
            return;
        }
        if (typeof version !== 'string' || !version) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'version is required');
            return;
        }
        if (artifact_id !== undefined && artifact_id !== null && !(0, _helpers_1.isUuid)(artifact_id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'artifact_id must be a UUID');
            return;
        }
        const deployMode = mode === undefined ? 'automatic' : mode;
        if (!MODES.includes(deployMode)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', "mode must be 'automatic' or 'manual'");
            return;
        }
        if (idempotency_key !== undefined && typeof idempotency_key !== 'string') {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'idempotency_key must be a string');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const project = await pool.query('SELECT id FROM projects WHERE id = $1', [project_id]);
        if (!project.rows[0]) {
            (0, errors_1.sendError)(res, 404, 'not_found', 'project not found');
            return;
        }
        if (host_id) {
            const host = await pool.query('SELECT id FROM hosts WHERE id = $1', [host_id]);
            if (!host.rows[0]) {
                (0, errors_1.sendError)(res, 404, 'not_found', 'host not found');
                return;
            }
        }
        if (artifact_id) {
            const art = await pool.query('SELECT id, project_id, status FROM artifacts WHERE id = $1', [artifact_id]);
            if (!art.rows[0]) {
                (0, errors_1.sendError)(res, 404, 'not_found', 'artifact not found');
                return;
            }
            if (art.rows[0].project_id !== project_id) {
                (0, errors_1.sendError)(res, 422, 'unprocessable', 'artifact does not belong to this project');
                return;
            }
            if (art.rows[0].status !== 'ready') {
                (0, errors_1.sendError)(res, 422, 'unprocessable', `artifact is ${art.rows[0].status}; only ready artifacts can be deployed`);
                return;
            }
        }
        const incomingBody = (0, idempotency_1.deploymentIdempotencyBody)({ project_id, host_id, version, artifact_id, mode: deployMode });
        if (typeof idempotency_key === 'string' && idempotency_key) {
            const { rows } = await pool.query('SELECT * FROM deployments WHERE idempotency_key = $1', [idempotency_key]);
            const existing = rows[0];
            if (existing) {
                const decision = (0, idempotency_1.decideIdempotency)({ body: (0, idempotency_1.deploymentIdempotencyBody)(existing) }, incomingBody);
                if (decision === 'replay') {
                    const task = await latestTaskForDeployment(pool, existing);
                    res.status(200).json({ deployment: existing, task, idempotent_replay: true });
                    return;
                }
                (0, errors_1.sendError)(res, 409, 'conflict', 'idempotency key already used with different deployment parameters');
                return;
            }
        }
        const deploymentId = (0, crypto_1.randomUUID)();
        const taskStatus = deployMode === 'manual' ? 'awaiting_approval' : 'queued';
        const taskPayload = {
            project_id,
            host_id: host_id ?? null,
            version,
            artifact_id: artifact_id ?? null,
            deployment_id: deploymentId,
        };
        const client = await pool.connect();
        let deployment;
        let task;
        try {
            await client.query('BEGIN');
            const depRows = await client.query(`INSERT INTO deployments (id, idempotency_key, project_id, host_id, version, status)
         VALUES ($1,$2,$3,$4,$5,'requested') RETURNING *`, [
                deploymentId,
                typeof idempotency_key === 'string' && idempotency_key ? idempotency_key : null,
                project_id,
                host_id ?? null,
                version,
            ]);
            deployment = depRows.rows[0];
            const taskRows = await client.query(`INSERT INTO tasks (idempotency_key, created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,$3,'deploy',$4,$5) RETURNING *`, [
                typeof idempotency_key === 'string' && idempotency_key ? `deploy:${idempotency_key}` : null,
                req.auth.id,
                host_id ?? null,
                taskStatus,
                JSON.stringify(taskPayload),
            ]);
            task = taskRows.rows[0];
            await client.query('UPDATE deployments SET task_id = $2 WHERE id = $1', [deploymentId, task.id]);
            deployment.task_id = task.id;
            await client.query('COMMIT');
        }
        catch (err) {
            await client.query('ROLLBACK');
            if (err.code === '23505') {
                (0, errors_1.sendError)(res, 409, 'conflict', 'a deployment of this project+host+version already exists');
                return;
            }
            throw err;
        }
        finally {
            client.release();
        }
        await (0, events_1.appendEvent)(pool, {
            type: 'deployment.requested',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            deployment_id: deploymentId,
            host_id: host_id ?? null,
            payload: { version, mode: deployMode },
        });
        await (0, events_1.appendEvent)(pool, {
            type: 'task.created',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            host_id: host_id ?? null,
            payload: { type: 'deploy', mode: deployMode },
        });
        log_1.logger.info('deployment requested', { deployment: deploymentId, version });
        res.status(201).json({ deployment, task });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/deployments?project_id=&host_id=&status=
exports.deploymentsRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        const { project_id, host_id, status } = req.query;
        const limit = (0, _helpers_1.parseLimit)(req.query.limit);
        const conds = [];
        const params = [];
        if (typeof project_id === 'string') {
            if (!(0, _helpers_1.isUuid)(project_id)) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'project_id must be a UUID');
                return;
            }
            params.push(project_id);
            conds.push(`project_id = $${params.length}`);
        }
        if (typeof host_id === 'string') {
            if (!(0, _helpers_1.isUuid)(host_id)) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'host_id must be a UUID');
                return;
            }
            params.push(host_id);
            conds.push(`host_id = $${params.length}`);
        }
        if (typeof status === 'string') {
            params.push(status);
            conds.push(`status = $${params.length}`);
        }
        const where = conds.length ? 'WHERE ' + conds.join(' AND ') : '';
        params.push(limit);
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT * FROM deployments ${where} ORDER BY created_at DESC LIMIT $${params.length}`, params);
        res.json({ deployments: rows });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/deployments/:id — includes the latest task.
exports.deploymentsRouter.get('/:id', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [req.params.id]);
        const deployment = rows[0];
        if (!deployment) {
            next(new errors_1.HttpError(404, 'not_found', 'deployment not found'));
            return;
        }
        const task = await latestTaskForDeployment(pool, deployment);
        res.json({ deployment, task });
    }
    catch (err) {
        next(err);
    }
});
// POST /v1/deployments/:id/rollback — needs deploy; deploys the previous
// healthy version of the same project+host as a new deployment row linked
// via rollback_of. The worker's completion report emits deployment.rolled_back.
exports.deploymentsRouter.post('/:id/rollback', auth_1.requireAgent, (0, auth_1.requirePermission)('deploy'), async (req, res, next) => {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [req.params.id]);
        const current = rows[0];
        if (!current) {
            next(new errors_1.HttpError(404, 'not_found', 'deployment not found'));
            return;
        }
        const prev = (await pool.query(`SELECT * FROM deployments
         WHERE project_id = $1 AND host_id = $2 AND id <> $3
           AND status = 'running' AND health_status = 'healthy'
         ORDER BY created_at DESC LIMIT 1`, [current.project_id, current.host_id, current.id])).rows[0];
        if (!prev) {
            (0, errors_1.sendError)(res, 409, 'conflict', 'no previous healthy deployment of this project+host to roll back to');
            return;
        }
        const artForPrev = (await pool.query(`SELECT id FROM artifacts WHERE project_id = $1 AND version = $2 ORDER BY created_at DESC LIMIT 1`, [current.project_id, prev.version])).rows[0];
        const deploymentId = (0, crypto_1.randomUUID)();
        const taskPayload = {
            project_id: current.project_id,
            host_id: current.host_id,
            version: prev.version,
            artifact_id: artForPrev ? artForPrev.id : null,
            deployment_id: deploymentId,
            rollback: true,
            rollback_of: current.id,
            rollback_to_deployment_id: prev.id,
        };
        const client = await pool.connect();
        let deployment;
        let task;
        try {
            await client.query('BEGIN');
            const depRows = await client.query(`INSERT INTO deployments (id, idempotency_key, project_id, host_id, version, status, rollback_of)
         VALUES ($1,$2,$3,$4,$5,'requested',$6) RETURNING *`, [deploymentId, `rollback:${(0, crypto_1.randomUUID)()}`, current.project_id, current.host_id, prev.version, current.id]);
            deployment = depRows.rows[0];
            const taskRows = await client.query(`INSERT INTO tasks (created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,'deploy','queued',$3) RETURNING *`, [req.auth.id, current.host_id, JSON.stringify(taskPayload)]);
            task = taskRows.rows[0];
            await client.query('UPDATE deployments SET task_id = $2 WHERE id = $1', [deploymentId, task.id]);
            deployment.task_id = task.id;
            await client.query('COMMIT');
        }
        catch (err) {
            await client.query('ROLLBACK');
            throw err;
        }
        finally {
            client.release();
        }
        await (0, events_1.appendEvent)(pool, {
            type: 'deployment.requested',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            deployment_id: deploymentId,
            host_id: current.host_id,
            payload: { version: prev.version, rollback: true, rollback_of: current.id },
        });
        await (0, events_1.appendEvent)(pool, {
            type: 'task.created',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            host_id: current.host_id,
            payload: { type: 'deploy', rollback: true },
        });
        log_1.logger.info('rollback deployment created', { deployment: deploymentId, from: current.id, to: prev.id });
        res.status(201).json({ deployment, task });
    }
    catch (err) {
        next(err);
    }
});
