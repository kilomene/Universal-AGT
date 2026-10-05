"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.attachAuth = attachAuth;
exports.requireAgent = requireAgent;
exports.requireHost = requireHost;
exports.hasPermission = hasPermission;
exports.requirePermission = requirePermission;
exports.provisioningOrDeploy = provisioningOrDeploy;
exports.apiErrorHandler = apiErrorHandler;
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const tokens_1 = require("../lib/tokens");
function parseBearer(req) {
    const header = req.headers.authorization;
    if (header) {
        const [scheme, token] = header.split(' ');
        if (scheme && scheme.toLowerCase() === 'bearer' && token)
            return token;
    }
    // Fallback for browser EventSource (SSE), which cannot set request headers:
    // the dashboard passes ?api_key=<token> on GET /v1/events/stream ONLY.
    // Honored nowhere else — query-string tokens can end up in access logs
    // (tightened 2026-10-05; was accepted globally before).
    const q = req.query.api_key;
    if (typeof q === 'string' && q.length > 0) {
        const path = String(req.originalUrl || '').split('?')[0];
        if (req.method === 'GET' && path === '/v1/events/stream')
            return q;
    }
    return null;
}
// Attaches req.auth when a valid bearer token is present; never 401s.
// Mount globally on /v1 so rate limiting and guards can rely on it.
async function attachAuth(req, _res, next) {
    try {
        const token = parseBearer(req);
        if (!token)
            return next();
        const hash = (0, tokens_1.sha256Hex)(token);
        const pool = (0, pool_1.getPool)();
        const agentRes = await pool.query(`SELECT id, name, permissions, status FROM agents WHERE api_key_hash = $1`, [hash]);
        if (agentRes.rows[0]) {
            const row = agentRes.rows[0];
            if (row.status !== 'active') {
                return next(new errors_1.HttpError(403, 'forbidden', 'agent is not active'));
            }
            req.auth = {
                kind: 'agent',
                id: row.id,
                name: row.name,
                permissions: (row.permissions ?? {}),
            };
            await pool.query('UPDATE agents SET last_seen = now() WHERE id = $1', [row.id]).catch(() => { });
            return next();
        }
        const hostRes = await pool.query(`SELECT id, name FROM hosts WHERE token_hash = $1`, [hash]);
        if (hostRes.rows[0]) {
            const row = hostRes.rows[0];
            req.auth = { kind: 'host', id: row.id, name: row.name, permissions: {} };
            return next();
        }
        return next();
    }
    catch (err) {
        return next(err);
    }
}
function requireAgent(req, _res, next) {
    if (!req.auth)
        return next(new errors_1.HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
    if (req.auth.kind !== 'agent')
        return next(new errors_1.HttpError(403, 'forbidden', 'agent token required'));
    return next();
}
function requireHost(req, _res, next) {
    if (!req.auth)
        return next(new errors_1.HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
    if (req.auth.kind !== 'host')
        return next(new errors_1.HttpError(403, 'forbidden', 'host token required'));
    return next();
}
function hasPermission(auth, name) {
    return auth.permissions?.[name] === true;
}
function requirePermission(name) {
    return (req, _res, next) => {
        if (!req.auth || req.auth.kind !== 'agent') {
            return next(new errors_1.HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
        }
        if (!hasPermission(req.auth, name)) {
            return next(new errors_1.HttpError(403, 'forbidden', `missing permission: ${name}`));
        }
        return next();
    };
}
// Provisioning bootstrap for POST /v1/hosts/register: either a matching
// PROVISIONING_TOKEN bearer, or an agent token with the deploy permission.
// (Choice documented in README: env token for hands-off bootstrap.)
function provisioningOrDeploy(req, _res, next) {
    const expected = process.env.PROVISIONING_TOKEN;
    const token = parseBearer(req);
    if (expected && token && (0, tokens_1.verifyToken)(token, (0, tokens_1.sha256Hex)(expected))) {
        return next();
    }
    if (req.auth?.kind === 'agent' && hasPermission(req.auth, 'deploy')) {
        return next();
    }
    if (!req.auth && !token) {
        return next(new errors_1.HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
    }
    return next(new errors_1.HttpError(403, 'forbidden', 'host registration requires a provisioning token or the deploy permission'));
}
function apiErrorHandler(err, _req, res, 
// eslint-disable-next-line @typescript-eslint/no-unused-vars
_next) {
    if (err instanceof errors_1.HttpError) {
        (0, errors_1.sendError)(res, err.status, err.code, err.message);
        return;
    }
    // eslint-disable-next-line no-console
    console.error(JSON.stringify({ ts: new Date().toISOString(), level: 'error', msg: 'unhandled error', err: String(err) }));
    (0, errors_1.sendError)(res, 500, 'internal', 'internal server error');
}
