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

// Canonical permission model (least privilege, PROTOCOL §1).
//
// Agent permissions are a flat JSONB map on the agent row
// (agents.permissions): {"deploy": true, ...}. Every mutating agent
// endpoint is guarded by requirePermission(<one of these>); read-only
// agent endpoints use 'read_status'. Host-kind callers have NO agent
// permissions (permissions: {}) and are never checked against this list —
// host endpoints enforce host self-identity (requireHost + path-id match)
// instead.
//
// Endpoint -> permission mapping:
//   deploy:                create/update projects, create deployments,
//                          create/rollback tasks (task types deploy, remove,
//                          rollback, build, docker-*, environment-update,
//                          artifact-upload, ingress-sync), artifact init +
//                          content upload, cancel other agents' tasks,
//                          register new hosts (alternative to the
//                          provisioning token)
//   approve_deployments:   approve/reject awaiting_approval tasks
//   read_status:           all read-only agent endpoints (tasks, hosts,
//                          deployments, services, projects, artifacts,
//                          events, domains listing is NOT here — see below)
//   restart:               restart/start services
//   stop:                  stop services
//   manage_secrets:        create/list/delete project secrets (plaintext
//                          values are never returned by the list endpoint)
//   manage_domains:        create/list/delete deployment domain entries
//
// "Host admin" lifecycle (register/rotate) is deliberately NOT a
// permission: host registration requires the UAHT_PROVISIONING_TOKEN
// (or an agent with deploy) via provisioningOrDeploy, and token rotation
// is host self-service only (requireHost + the token must belong to the
// host in the path). No agent permission can mint or rotate host tokens.
export const PERMISSIONS = [
  'deploy',
  'approve_deployments',
  'read_status',
  'restart',
  'stop',
  'manage_secrets',
  'manage_domains',
] as const;
export type PermissionName = (typeof PERMISSIONS)[number];

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

    const hostRes = await pool.query(
      `SELECT id, name FROM hosts
        WHERE token_hash = $1
           OR (previous_token_hash = $1 AND previous_token_expires_at > now())`,
      [hash],
    );
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
// 2026-10-06: accept the token from EITHER Authorization: Bearer OR the
// X-Provisioning-Token header. The documented agent-integration convention is
// the X-Provisioning-Token header (docs/agent-integration.md); the Bearer
// form is kept for backward compatibility. Both carry the same credential.
function provisioningTokenFrom(req: Request): string | null {
  const header = req.header('x-provisioning-token');
  if (header && header.length > 0) return header;
  return parseBearer(req);
}
export function provisioningOrDeploy(req: Request, _res: Response, next: NextFunction): void {
  const expected = process.env.UAHT_PROVISIONING_TOKEN;
  const token = provisioningTokenFrom(req);
  if (expected && token && verifyToken(token, sha256Hex(expected))) {
    return next();
  }
  if (req.auth?.kind === 'agent' && hasPermission(req.auth, 'deploy')) {
    return next();
  }
  if (!req.auth && !token) {
    return next(new HttpError(401, 'unauthorized', 'missing or invalid provisioning credential (X-Provisioning-Token header or bearer token)'));
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
  // W16 audit: express.json() body-parser failures used to fall through to
  // the generic 500 below. A malformed JSON body is a client error (400);
  // a body over the 256kb parser limit is 413 — not an internal failure.
  const bodyErr = err as { type?: string; status?: number };
  if (err instanceof SyntaxError && bodyErr.type === 'entity.parse.failed') {
    sendError(res, 400, 'bad_request', 'malformed JSON body');
    return;
  }
  if (bodyErr.type === 'entity.too.large') {
    sendError(res, 413, 'payload_too_large', 'request body exceeds the 256kb JSON limit');
    return;
  }
  // eslint-disable-next-line no-console
  console.error(JSON.stringify({ ts: new Date().toISOString(), level: 'error', msg: 'unhandled error', err: String(err) }));
  sendError(res, 500, 'internal', 'internal server error');
}
