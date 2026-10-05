"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.servicesRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.servicesRouter = (0, express_1.Router)();
// Service = a deployment in a lifecycle status, presented in a friendly shape.
const SERVICE_STATUSES = ['running', 'failed', 'building', 'starting', 'healthcheck', 'stopped', 'stopping'];
function toServiceView(row) {
    return {
        id: row.id,
        project_id: row.project_id,
        project_name: row.project_name,
        host_id: row.host_id,
        host_name: row.host_name,
        version: row.version,
        status: row.status,
        health_status: row.health_status,
        container_ids: row.container_ids,
        ports: row.ports,
        domains: row.domains,
        updated_at: row.updated_at,
    };
}
// GET /v1/services?host_id= — agent, read_status.
exports.servicesRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        const { host_id } = req.query;
        const limit = (0, _helpers_1.parseLimit)(req.query.limit);
        const conds = [`d.status = ANY($1)`];
        const params = [SERVICE_STATUSES];
        if (typeof host_id === 'string') {
            if (!(0, _helpers_1.isUuid)(host_id)) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'host_id must be a UUID');
                return;
            }
            params.push(host_id);
            conds.push(`d.host_id = $${params.length}`);
        }
        params.push(limit);
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT d.*, p.name AS project_name, h.name AS host_name
       FROM deployments d
       JOIN projects p ON p.id = d.project_id
       JOIN hosts h ON h.id = d.host_id
       WHERE ${conds.join(' AND ')}
       ORDER BY d.updated_at DESC LIMIT $${params.length}`, params);
        res.json({ services: rows.map(toServiceView) });
    }
    catch (err) {
        next(err);
    }
});
// Creates a control task (restart|stop|start) targeting the deployment's host.
async function controlTask(req, res, next, taskType) {
    try {
        const deploymentId = req.params.id;
        if (!(0, _helpers_1.isUuid)(deploymentId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM deployments WHERE id = $1', [deploymentId]);
        const deployment = rows[0];
        if (!deployment) {
            next(new errors_1.HttpError(404, 'not_found', 'service (deployment) not found'));
            return;
        }
        const taskRows = (await pool.query(`INSERT INTO tasks (created_by, assigned_to, type, status, payload)
         VALUES ($1,$2,$3,'queued',$4) RETURNING *`, [req.auth.id, deployment.host_id, taskType, JSON.stringify({ deployment_id: deploymentId })])).rows;
        const task = taskRows[0];
        await (0, events_1.appendEvent)(pool, {
            type: 'task.created',
            actor_type: 'agent',
            actor_id: req.auth.name,
            task_id: task.id,
            host_id: deployment.host_id,
            payload: { type: taskType, deployment_id: deploymentId },
        });
        log_1.logger.info('service control task created', { type: taskType, deployment: deploymentId });
        res.status(201).json({ task });
    }
    catch (err) {
        next(err);
    }
}
// POST /v1/services/:id/restart — needs restart.
exports.servicesRouter.post('/:id/restart', auth_1.requireAgent, (0, auth_1.requirePermission)('restart'), (req, res, next) => controlTask(req, res, next, 'restart'));
// POST /v1/services/:id/stop — needs stop.
exports.servicesRouter.post('/:id/stop', auth_1.requireAgent, (0, auth_1.requirePermission)('stop'), (req, res, next) => controlTask(req, res, next, 'stop'));
// POST /v1/services/:id/start — needs restart (per PROTOCOL §3.6).
exports.servicesRouter.post('/:id/start', auth_1.requireAgent, (0, auth_1.requirePermission)('restart'), (req, res, next) => controlTask(req, res, next, 'start'));
