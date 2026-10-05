"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.agentsRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const tokens_1 = require("../lib/tokens");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.agentsRouter = (0, express_1.Router)();
// POST /v1/agents/register — open bootstrap endpoint (first agent has no key
// yet). Later registrations are a normal API call; operators should front this
// with network policy in production (see README).
exports.agentsRouter.post('/register', async (req, res, next) => {
    try {
        const { name, type, capabilities, permissions } = req.body ?? {};
        if (typeof name !== 'string' || !name.trim()) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'name is required');
            return;
        }
        if (capabilities !== undefined && !Array.isArray(capabilities)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'capabilities must be an array');
            return;
        }
        if (permissions !== undefined && (typeof permissions !== 'object' || permissions === null || Array.isArray(permissions))) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'permissions must be an object');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const { token, hash } = (0, tokens_1.generateAgentKey)();
        try {
            const { rows } = await pool.query(`INSERT INTO agents (name, type, capabilities, permissions, api_key_hash)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING id, name, type, capabilities, permissions, status, last_seen, metadata, created_at, updated_at`, [
                name.trim(),
                typeof type === 'string' && type ? type : 'generic',
                JSON.stringify(capabilities ?? []),
                JSON.stringify(permissions ?? {}),
                hash,
            ]);
            const agent = (0, _helpers_1.publicAgent)(rows[0]);
            await (0, events_1.appendEvent)(pool, {
                type: 'agent.connected',
                actor_type: 'agent',
                actor_id: agent.name,
                payload: { agent_id: agent.id },
            });
            log_1.logger.info('agent registered', { name: agent.name });
            res.status(201).json({ agent, api_key: token });
        }
        catch (err) {
            if (err.code === '23505') {
                (0, errors_1.sendError)(res, 409, 'conflict', `agent name '${name.trim()}' already exists`);
                return;
            }
            throw err;
        }
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/agents/me — caller's own agent row (no secret fields).
exports.agentsRouter.get('/me', auth_1.requireAgent, async (req, res, next) => {
    try {
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT id, name, type, capabilities, permissions, status, last_seen, metadata, created_at, updated_at
       FROM agents WHERE id = $1`, [req.auth.id]);
        if (!rows[0]) {
            next(new errors_1.HttpError(404, 'not_found', 'agent not found'));
            return;
        }
        res.json({ agent: (0, _helpers_1.publicAgent)(rows[0]) });
    }
    catch (err) {
        next(err);
    }
});
