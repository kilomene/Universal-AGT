"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.hostsRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const tokens_1 = require("../lib/tokens");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.hostsRouter = (0, express_1.Router)();
// POST /v1/hosts/register — provisioning token OR agent with deploy permission
// (see provisioningOrDeploy; choice documented in README).
exports.hostsRouter.post('/register', auth_1.provisioningOrDeploy, async (req, res, next) => {
    try {
        const { name, host_type, capabilities, worker_version } = req.body ?? {};
        if (typeof name !== 'string' || !name.trim()) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'name is required');
            return;
        }
        if (capabilities !== undefined && !Array.isArray(capabilities)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'capabilities must be an array');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { token, hash } = (0, tokens_1.generateHostToken)();
        try {
            const { rows } = await pool.query(`INSERT INTO hosts (name, host_type, capabilities, worker_version, token_hash)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING id, name, host_type, status, capabilities, worker_version,
                   cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
                   total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
                   created_at, updated_at`, [
                name.trim(),
                typeof host_type === 'string' && host_type ? host_type : 'persistent-linux-host',
                JSON.stringify(capabilities ?? []),
                typeof worker_version === 'string' ? worker_version : null,
                hash,
            ]);
            const host = (0, _helpers_1.publicHost)(rows[0]);
            await (0, events_1.appendEvent)(pool, {
                type: 'host.registered',
                actor_type: req.auth?.kind ?? 'system',
                actor_id: req.auth?.name ?? null,
                host_id: host.id,
                payload: { host_name: host.name },
            });
            log_1.logger.info('host registered', { name: host.name });
            res.status(201).json({ host, host_token: token });
        }
        catch (err) {
            if (err.code === '23505') {
                (0, errors_1.sendError)(res, 409, 'conflict', `host name '${name.trim()}' already exists`);
                return;
            }
            throw err;
        }
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/hosts — agent, read_status.
exports.hostsRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (_req, res, next) => {
    try {
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT id, name, host_type, status, capabilities, worker_version,
              cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
              total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
              created_at, updated_at
       FROM hosts ORDER BY created_at ASC`);
        res.json({ hosts: rows.map(_helpers_1.publicHost) });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/hosts/:id — agent, read_status.
exports.hostsRouter.get('/:id', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT id, name, host_type, status, capabilities, worker_version,
              cpu_pct, ram_pct, disk_pct, docker_status, running_apps,
              total_cpu, total_ram_mb, total_disk_gb, last_seen, metadata,
              created_at, updated_at
       FROM hosts WHERE id = $1`, [req.params.id]);
        if (!rows[0]) {
            next(new errors_1.HttpError(404, 'not_found', 'host not found'));
            return;
        }
        res.json({ host: (0, _helpers_1.publicHost)(rows[0]) });
    }
    catch (err) {
        next(err);
    }
});
