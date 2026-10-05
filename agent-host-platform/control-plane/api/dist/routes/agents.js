"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.agentsRouter = void 0;
exports.checkRegistrationAccess = checkRegistrationAccess;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const tokens_1 = require("../lib/tokens");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.agentsRouter = (0, express_1.Router)();
// Registration access decision, extracted pure for tests.
// - UAHT_PROVISIONING_TOKEN set: the X-Provisioning-Token header must match
//   (constant-time). Missing/wrong -> 401.
// - Unset (non-production): bootstrap mode — open registration ONLY while no
//   agents exist yet; afterwards registration is closed until the operator
//   sets UAHT_PROVISIONING_TOKEN -> 403.
function checkRegistrationAccess(presented, agentCount) {
    const expected = process.env.UAHT_PROVISIONING_TOKEN;
    if (expected) {
        if (presented && (0, tokens_1.verifyToken)(presented, (0, tokens_1.sha256Hex)(expected))) {
            return { ok: true };
        }
        return {
            ok: false,
            status: 401,
            message: 'agent registration requires the X-Provisioning-Token header',
        };
    }
    if (agentCount === 0)
        return { ok: true }; // bootstrap: first agent
    return {
        ok: false,
        status: 403,
        message: 'agent registration is closed: set UAHT_PROVISIONING_TOKEN on the control plane to provision more agents',
    };
}
// POST /v1/agents/register — gated (2026-10-05): a provisioning token, or
// bootstrap mode for the very first agent. The unauthenticated rate-limit
// bucket (10/min per IP) also applies. Permissions remain caller-chosen:
// whoever holds the provisioning token is trusted to scope the key.
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
        const { rows: countRows } = await pool.query('SELECT count(*)::int AS n FROM agents');
        const access = checkRegistrationAccess(req.header('x-provisioning-token') ?? undefined, Number(countRows[0]?.n ?? 0));
        if (!access.ok) {
            (0, errors_1.sendError)(res, access.status, access.status === 401 ? 'unauthorized' : 'forbidden', access.message);
            return;
        }
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
// POST /v1/agents/me/rotate — rotate the caller's own API key. Returns the
// NEW key (shown once); the old key stops working immediately. Emits
// agent.key_rotated. Backwards compatible: keys that never rotate keep
// working exactly as before.
exports.agentsRouter.post('/me/rotate', auth_1.requireAgent, async (req, res, next) => {
    try {
        const pool = (0, pool_1.getPool)();
        const { token, hash } = (0, tokens_1.generateAgentKey)();
        const { rowCount } = await pool.query(`UPDATE agents SET api_key_hash = $2, updated_at = now() WHERE id = $1`, [req.auth.id, hash]);
        if (!rowCount) {
            next(new errors_1.HttpError(404, 'not_found', 'agent not found'));
            return;
        }
        await (0, events_1.appendEvent)(pool, {
            type: 'agent.key_rotated',
            actor_type: 'agent',
            actor_id: req.auth.name,
            payload: { agent_id: req.auth.id },
        });
        log_1.logger.info('agent key rotated', { agent: req.auth.name });
        res.json({ api_key: token });
    }
    catch (err) {
        next(err);
    }
});
