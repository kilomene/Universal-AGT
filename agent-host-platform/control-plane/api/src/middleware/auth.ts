import type { NextFunction, Request, Response } from 'express';
import { getPool } from '../db/pool';
import { HttpError, sendError } from '../lib/errors';
import { sha256Hex, verifyToken } from '../lib/tokens';

// PROTOCOL §1: two credential kinds, both `Authorization: Bearer <token>`.
// Agent keys are looked up in agents (by api_key_hash), host tokens in hosts
// (by token_hash). A host token can never call agent endpoints and vice versa.

export interface AuthContext {
  kind: 'agent' | 'host';
  id: string;
  name: string;
  permissions: Record<string, boolean>;
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      auth?: AuthContext;
    }
  }
}

function parseBearer(req: Request): string | null {
  const header = req.headers.authorization;
  if (header) {
    const [scheme, token] = header.split(' ');
    if (scheme && scheme.toLowerCase() === 'bearer' && token) return token;
  }
  // Fallback for browser EventSource (SSE), which cannot set request headers:
  // the dashboard passes ?api_key=<token> on GET /v1/events/stream ONLY.
  // Honored nowhere else — query-string tokens can end up in access logs
  // (tightened 2026-10-05; was accepted globally before).
  const q = req.query.api_key;
  if (typeof q === 'string' && q.length > 0) {
    const path = String(req.originalUrl || '').split('?')[0];
    if (req.method === 'GET' && path === '/v1/events/stream') return q;
  }
  return null;
}

// Attaches req.auth when a valid bearer token is present; never 401s.
// Mount globally on /v1 so rate limiting and guards can rely on it.
export async function attachAuth(req: Request, _res: Response, next: NextFunction): Promise<void> {
  try {
    const token = parseBearer(req);
    if (!token) return next();
    const hash = sha256Hex(token);
    const pool = getPool();

    const agentRes = await pool.query(
      `SELECT id, name, permissions, status FROM agents WHERE api_key_hash = $1`,
      [hash],
    );
    if (agentRes.rows[0]) {
      const row = agentRes.rows[0];
      if (row.status !== 'active') {
        return next(new HttpError(403, 'forbidden', 'agent is not active'));
      }
      req.auth = {
        kind: 'agent',
        id: row.id,
        name: row.name,
        permissions: (row.permissions ?? {}) as Record<string, boolean>,
      };
      await pool.query('UPDATE agents SET last_seen = now() WHERE id = $1', [row.id]).catch(() => {});
      return next();
    }

    const hostRes = await pool.query(`SELECT id, name FROM hosts WHERE token_hash = $1`, [hash]);
    if (hostRes.rows[0]) {
      const row = hostRes.rows[0];
      req.auth = { kind: 'host', id: row.id, name: row.name, permissions: {} };
      return next();
    }
    return next();
  } catch (err) {
    return next(err);
  }
}

export function requireAgent(req: Request, _res: Response, next: NextFunction): void {
  if (!req.auth) return next(new HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
  if (req.auth.kind !== 'agent') return next(new HttpError(403, 'forbidden', 'agent token required'));
  return next();
}

export function requireHost(req: Request, _res: Response, next: NextFunction): void {
  if (!req.auth) return next(new HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
  if (req.auth.kind !== 'host') return next(new HttpError(403, 'forbidden', 'host token required'));
  return next();
}

export function hasPermission(auth: AuthContext, name: string): boolean {
  return auth.permissions?.[name] === true;
}

export function requirePermission(name: string) {
  return (req: Request, _res: Response, next: NextFunction): void => {
    if (!req.auth || req.auth.kind !== 'agent') {
      return next(new HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
    }
    if (!hasPermission(req.auth, name)) {
      return next(new HttpError(403, 'forbidden', `missing permission: ${name}`));
    }
    return next();
  };
}

// Provisioning bootstrap for POST /v1/hosts/register: either a matching
// UAHT_PROVISIONING_TOKEN bearer, or an agent token with the deploy permission.
// (Choice documented in README: env token for hands-off bootstrap.)
// W3 (2026-10-05): ONE canonical provisioning-token name. The same token that
// gates agent registration (X-Provisioning-Token header) also gates host
// bootstrap registration (Authorization: Bearer). The old unprefixed name
// is gone — see docs/CONFIG.md.
export function provisioningOrDeploy(req: Request, _res: Response, next: NextFunction): void {
  const expected = process.env.UAHT_PROVISIONING_TOKEN;
  const token = parseBearer(req);
  if (expected && token && verifyToken(token, sha256Hex(expected))) {
    return next();
  }
  if (req.auth?.kind === 'agent' && hasPermission(req.auth, 'deploy')) {
    return next();
  }
  if (!req.auth && !token) {
    return next(new HttpError(401, 'unauthorized', 'missing or invalid bearer token'));
  }
  return next(new HttpError(403, 'forbidden', 'host registration requires a provisioning token or the deploy permission'));
}

export function apiErrorHandler(
  err: unknown,
  _req: Request,
  res: Response,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _next: NextFunction,
): void {
  if (err instanceof HttpError) {
    sendError(res, err.status, err.code, err.message);
    return;
  }
  // eslint-disable-next-line no-console
  console.error(JSON.stringify({ ts: new Date().toISOString(), level: 'error', msg: 'unhandled error', err: String(err) }));
  sendError(res, 500, 'internal', 'internal server error');
}
