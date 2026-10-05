"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.secretsRouter = void 0;
exports.getDecryptedProjectSecrets = getDecryptedProjectSecrets;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const secrets_1 = require("../lib/secrets");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.secretsRouter = (0, express_1.Router)({ mergeParams: true });
// All three routes require the manage_secrets permission.
// Plaintext values are encrypted before INSERT and never returned by the API.
async function getProject(pool, id) {
    const { rows } = await pool.query('SELECT id FROM projects WHERE id = $1', [id]);
    return rows[0] ?? null;
}
// POST /v1/projects/:id/secrets {name, value} — upsert, encrypted at rest.
exports.secretsRouter.post('/', auth_1.requireAgent, (0, auth_1.requirePermission)('manage_secrets'), async (req, res, next) => {
    try {
        const projectId = req.params.id;
        if (!(0, _helpers_1.isUuid)(projectId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'project id must be a UUID');
            return;
        }
        const { name, value } = req.body ?? {};
        if (typeof name !== 'string' || !name.trim() || name.length > 128) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'name is required (max 128 chars)');
            return;
        }
        if (typeof value !== 'string' || !value) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'value is required');
            return;
        }
        const pool = (0, pool_1.getPool)();
        if (!(await getProject(pool, projectId))) {
            next(new errors_1.HttpError(404, 'not_found', 'project not found'));
            return;
        }
        let encrypted;
        try {
            encrypted = (0, secrets_1.encryptSecret)(value);
        }
        catch (err) {
            next(new errors_1.HttpError(500, 'internal', `secret encryption failed: ${String(err)}`));
            return;
        }
        const { rows } = await pool.query(`INSERT INTO secrets (project_id, name, encrypted_value)
       VALUES ($1,$2,$3)
       ON CONFLICT (project_id, name) DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value
       RETURNING id, project_id, name, created_at, updated_at`, [projectId, name.trim(), encrypted]);
        await (0, events_1.appendEvent)(pool, {
            type: 'secret.updated',
            actor_type: 'agent',
            actor_id: req.auth.name,
            payload: { project_id: projectId, name: name.trim() },
        });
        log_1.logger.info('project secret stored', { project: projectId, name: name.trim() });
        res.status(201).json({ secret: rows[0] });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/projects/:id/secrets — names only, never values.
exports.secretsRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('manage_secrets'), async (req, res, next) => {
    try {
        const projectId = req.params.id;
        if (!(0, _helpers_1.isUuid)(projectId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'project id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        if (!(await getProject(pool, projectId))) {
            next(new errors_1.HttpError(404, 'not_found', 'project not found'));
            return;
        }
        const { rows } = await pool.query('SELECT name, created_at, updated_at FROM secrets WHERE project_id = $1 ORDER BY name ASC', [projectId]);
        res.json({ secrets: rows });
    }
    catch (err) {
        next(err);
    }
});
// DELETE /v1/projects/:id/secrets/:name
exports.secretsRouter.delete('/:name', auth_1.requireAgent, (0, auth_1.requirePermission)('manage_secrets'), async (req, res, next) => {
    try {
        const projectId = req.params.id;
        if (!(0, _helpers_1.isUuid)(projectId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'project id must be a UUID');
            return;
        }
        const pool = (0, pool_1.getPool)();
        if (!(await getProject(pool, projectId))) {
            next(new errors_1.HttpError(404, 'not_found', 'project not found'));
            return;
        }
        const { rowCount } = await pool.query('DELETE FROM secrets WHERE project_id = $1 AND name = $2', [
            projectId,
            req.params.name,
        ]);
        if (!rowCount) {
            next(new errors_1.HttpError(404, 'not_found', 'secret not found'));
            return;
        }
        await (0, events_1.appendEvent)(pool, {
            type: 'secret.updated',
            actor_type: 'agent',
            actor_id: req.auth.name,
            payload: { project_id: projectId, name: req.params.name, deleted: true },
        });
        res.status(204).end();
    }
    catch (err) {
        next(err);
    }
});
// Internal helper for the worker: decrypted values for a deployment's project.
// Used server-side only — never exposed over HTTP.
async function getDecryptedProjectSecrets(pool, projectId) {
    const { rows } = await pool.query('SELECT name, encrypted_value FROM secrets WHERE project_id = $1', [projectId]);
    const out = {};
    for (const row of rows) {
        out[row.name] = (0, secrets_1.decryptSecret)(row.encrypted_value);
    }
    return out;
}
