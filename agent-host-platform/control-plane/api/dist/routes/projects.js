"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.projectsRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
const secrets_1 = require("./secrets");
exports.projectsRouter = (0, express_1.Router)();
// Nested secrets routes: /v1/projects/:id/secrets
exports.projectsRouter.use('/:id/secrets', secrets_1.secretsRouter);
const RUNTIMES = ['docker', 'docker-compose', 'static'];
// Minimal agent.deploy.json manifest validation (PROTOCOL §4).
function validateConfiguration(configuration, projectName) {
    if (typeof configuration !== 'object' || configuration === null || Array.isArray(configuration)) {
        return 'configuration must be an object';
    }
    const cfg = configuration;
    if (cfg.name !== undefined && cfg.name !== projectName) {
        return 'configuration.name must match the project name';
    }
    if (cfg.runtime !== undefined && !RUNTIMES.includes(cfg.runtime)) {
        return `configuration.runtime must be one of: ${RUNTIMES.join(', ')}`;
    }
    const service = cfg.service;
    if (service !== undefined) {
        const port = service.port;
        if (port !== undefined && (!Number.isInteger(port) || port < 1 || port > 65535)) {
            return 'configuration.service.port must be 1-65535';
        }
    }
    const resources = cfg.resources;
    if (resources !== undefined) {
        if (resources.memory !== undefined && !/^(\d+)(m|g)$/i.test(String(resources.memory))) {
            return "configuration.resources.memory must look like '256m' or '1g'";
        }
        if (resources.cpu !== undefined && !(typeof resources.cpu === 'number' && resources.cpu > 0)) {
            return 'configuration.resources.cpu must be a positive number';
        }
    }
    if (cfg.restart !== undefined && !['no', 'always', 'unless-stopped', 'on-failure'].includes(cfg.restart)) {
        return "configuration.restart must be one of: no, always, unless-stopped, on-failure";
    }
    return null;
}
// POST /v1/projects
exports.projectsRouter.post('/', auth_1.requireAgent, (0, auth_1.requirePermission)('deploy'), async (req, res, next) => {
    try {
        const { name, owner, repository, runtime, configuration } = req.body ?? {};
        if (typeof name !== 'string' || !name.trim()) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'name is required');
            return;
        }
        if (runtime !== undefined && !RUNTIMES.includes(runtime)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', `runtime must be one of: ${RUNTIMES.join(', ')}`);
            return;
        }
        if (configuration !== undefined) {
            const err = validateConfiguration(configuration, name.trim());
            if (err) {
                (0, errors_1.sendError)(res, 422, 'unprocessable', err);
                return;
            }
        }
        const pool = (0, pool_1.getPool)();
        try {
            const { rows } = await pool.query(`INSERT INTO projects (name, owner, repository, runtime, configuration)
         VALUES ($1,$2,$3,$4,$5) RETURNING *`, [
                name.trim(),
                typeof owner === 'string' ? owner : req.auth.name,
                typeof repository === 'string' ? repository : null,
                runtime ?? 'docker',
                JSON.stringify(configuration ?? {}),
            ]);
            res.status(201).json({ project: rows[0] });
        }
        catch (err) {
            if (err.code === '23505') {
                (0, errors_1.sendError)(res, 409, 'conflict', `project name '${name.trim()}' already exists`);
                return;
            }
            throw err;
        }
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/projects
exports.projectsRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (_req, res, next) => {
    try {
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM projects ORDER BY created_at ASC');
        res.json({ projects: rows });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/projects/:id
exports.projectsRouter.get('/:id', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM projects WHERE id = $1', [req.params.id]);
        if (!rows[0]) {
            next(new errors_1.HttpError(404, 'not_found', 'project not found'));
            return;
        }
        res.json({ project: rows[0] });
    }
    catch (err) {
        next(err);
    }
});
// PUT /v1/projects/:id — update configuration (validated manifest).
exports.projectsRouter.put('/:id', auth_1.requireAgent, (0, auth_1.requirePermission)('deploy'), async (req, res, next) => {
    try {
        if (!(0, _helpers_1.isUuid)(req.params.id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query('SELECT * FROM projects WHERE id = $1', [req.params.id]);
        const project = rows[0];
        if (!project) {
            next(new errors_1.HttpError(404, 'not_found', 'project not found'));
            return;
        }
        const { configuration, repository, runtime } = req.body ?? {};
        if (configuration !== undefined) {
            const err = validateConfiguration(configuration, project.name);
            if (err) {
                (0, errors_1.sendError)(res, 422, 'unprocessable', err);
                return;
            }
        }
        if (runtime !== undefined && !RUNTIMES.includes(runtime)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', `runtime must be one of: ${RUNTIMES.join(', ')}`);
            return;
        }
        const updated = (await pool.query(`UPDATE projects SET
           configuration = COALESCE($2, configuration),
           repository = COALESCE($3, repository),
           runtime = COALESCE($4, runtime)
         WHERE id = $1 RETURNING *`, [
            project.id,
            configuration !== undefined ? JSON.stringify(configuration) : null,
            typeof repository === 'string' ? repository : null,
            typeof runtime === 'string' ? runtime : null,
        ])).rows[0];
        res.json({ project: updated });
    }
    catch (err) {
        next(err);
    }
});
