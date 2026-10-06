import { Router, type Request, type Response } from 'express';
import type { Pool } from 'pg';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { generateAgentKey, sha256Hex, verifyToken } from '../lib/tokens';
import { auditAuthFailure, requireAgent } from '../middleware/auth';
import { rotationRateLimit } from '../middleware/rateLimit';
import { decideIdempotency } from '../lib/idempotency';
import { publicAgent } from './_helpers';

export const agentsRouter = Router();

// §7 registration idempotency for POST /v1/agents/register. Same semantics
// as hosts (see routes/hosts.ts): a retry with the same idempotency_key and
// equal parameters replays — the original API key is unrecoverable (hash
// only), so a fresh key is minted and returned, letting the retrying client
// recover working credentials instead of a dead 409. Agent rotation is
// immediate (matching POST /v1/agents/me/rotate), so a spurious retry of an
// already-delivered response invalidates the delivered key — retries must
// only be sent when the original response was not received.
async function replayAgentRegistration(
  req: Request, res: Response, pool: Pool,
  idemKey: string, regBody: { name: string; type: string; capabilities: unknown; permissions: unknown },
): Promise<boolean> {
  const { rows } = await pool.query(
    'SELECT id, name, type, capabilities, permissions FROM agents WHERE idempotency_key = $1',
    [idemKey],
  );
  const row = rows[0];
  if (!row) return false;
  const decision = decideIdempotency(
    {
      body: {
        name: row.name,
        type: row.type,
        capabilities: row.capabilities ?? [],
        permissions: row.permissions ?? {},
      },
    },
    regBody,
  );
  if (decision === 'conflict') {
    sendError(res, 409, 'conflict', 'idempotency_key was already used with different registration parameters');
    return true;
  }
  const { token, hash } = generateAgentKey();
  await pool.query('UPDATE agents SET api_key_hash = $2, updated_at = now() WHERE id = $1', [row.id, hash]);
  await appendEvent(pool, {
    type: 'agent.key_rotated',
    actor_type: 'agent',
    actor_id: row.name as string,
    payload: { agent_id: row.id, reason: 'registration_idempotency_replay' },
  });
  const ar = await pool.query(
    `SELECT id, name, type, capabilities, permissions, status, last_seen, metadata, created_at, updated_at
       FROM agents WHERE id = $1`,
    [row.id],
  );
  logger.info('agent registration replayed via idempotency key', { name: row.name });
  res.status(200).json({ agent: publicAgent(ar.rows[0]), api_key: token, idempotent_replay: true });
  return true;
}

// Registration access decision, extracted pure for tests.
// - UAHT_PROVISIONING_TOKEN set: the X-Provisioning-Token header must match
//   (constant-time). Missing/wrong -> 401.
// - Unset (non-production): bootstrap mode — open registration ONLY while no
//   agents exist yet; afterwards registration is closed until the operator
//   sets UAHT_PROVISIONING_TOKEN -> 403.
export function checkRegistrationAccess(
  presented: string | undefined,
  agentCount: number,
): { ok: true } | { ok: false; status: 401 | 403; message: string } {
  const expected = process.env.UAHT_PROVISIONING_TOKEN;
  if (expected) {
    if (presented && verifyToken(presented, sha256Hex(expected))) {
      return { ok: true };
    }
    return {
      ok: false,
      status: 401,
      message: 'agent registration requires the X-Provisioning-Token header',
    };
  }
  if (agentCount === 0) return { ok: true }; // bootstrap: first agent
  return {
    ok: false,
    status: 403,
    message:
      'agent registration is closed: set UAHT_PROVISIONING_TOKEN on the control plane to provision more agents',
  };
}

// POST /v1/agents/register — gated (2026-10-05): a provisioning token, or
// bootstrap mode for the very first agent. The unauthenticated rate-limit
// bucket (10/min per IP) also applies. Permissions remain caller-chosen:
// whoever holds the provisioning token is trusted to scope the key.
agentsRouter.post('/register', async (req, res, next) => {
  try {
    const { name, type, capabilities, permissions, idempotency_key } = req.body ?? {};
    if (typeof name !== 'string' || !name.trim()) {
      sendError(res, 400, 'bad_request', 'name is required');
      return;
    }
    if (capabilities !== undefined && !Array.isArray(capabilities)) {
      sendError(res, 400, 'bad_request', 'capabilities must be an array');
      return;
    }
    if (permissions !== undefined && (typeof permissions !== 'object' || permissions === null || Array.isArray(permissions))) {
      sendError(res, 400, 'bad_request', 'permissions must be an object');
      return;
    }
    const pool = getPool();
    // §7: optional idempotency key for safe client retries (lost responses).
    let idemKey: string | null = null;
    if (idempotency_key !== undefined) {
      if (typeof idempotency_key !== 'string' || !idempotency_key.trim() || idempotency_key.length > 128) {
        sendError(res, 400, 'bad_request', 'idempotency_key must be a non-empty string of at most 128 characters');
        return;
      }
      idemKey = idempotency_key.trim();
    }
    const regBody = {
      name: name.trim(),
      type: typeof type === 'string' && type ? type : 'generic',
      capabilities: capabilities ?? [],
      permissions: permissions ?? {},
    };
    const { rows: countRows } = await pool.query('SELECT count(*)::int AS n FROM agents');
    const presented = req.header('x-provisioning-token') ?? undefined;
    const access = checkRegistrationAccess(presented, Number(countRows[0]?.n ?? 0));
    if (!access.ok) {
      if (access.status === 401 && presented) {
        // A provisioning token was presented but did not match: auditable
        // auth failure (brute-force signal). Missing-token 401s are not
        // audited — they are ordinary unauthorized requests.
        void auditAuthFailure(req, 'POST /v1/agents/register', 'bad_provisioning_token');
      }
      sendError(res, access.status, access.status === 401 ? 'unauthorized' : 'forbidden', access.message);
      return;
    }
    if (idemKey && (await replayAgentRegistration(req, res, pool, idemKey, regBody))) return;
    const { token, hash } = generateAgentKey();
    try {
      const { rows } = await pool.query(
        `INSERT INTO agents (name, type, capabilities, permissions, api_key_hash, idempotency_key)
         VALUES ($1, $2, $3, $4, $5, $6)
         RETURNING id, name, type, capabilities, permissions, status, last_seen, metadata, created_at, updated_at`,
        [
          regBody.name,
          regBody.type,
          JSON.stringify(regBody.capabilities),
          JSON.stringify(regBody.permissions),
          hash,
          idemKey,
        ],
      );
      const agent = publicAgent(rows[0]);
      await appendEvent(pool, {
        type: 'agent.connected',
        actor_type: 'agent',
        actor_id: agent.name as string,
        payload: { agent_id: agent.id },
      });
      logger.info('agent registered', { name: agent.name });
      res.status(201).json({ agent, api_key: token });
    } catch (err: unknown) {
      if ((err as { code?: string }).code === '23505') {
        if (idemKey && (await replayAgentRegistration(req, res, pool, idemKey, regBody))) return;
        sendError(res, 409, 'conflict', `agent name '${name.trim()}' already exists`);
        return;
      }
      throw err;
    }
  } catch (err) {
    next(err);
  }
});

// GET /v1/agents/me — caller's own agent row (no secret fields).
agentsRouter.get('/me', requireAgent, async (req, res, next) => {
  try {
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT id, name, type, capabilities, permissions, status, last_seen, metadata, created_at, updated_at
       FROM agents WHERE id = $1`,
      [req.auth!.id],
    );
    if (!rows[0]) {
      next(new HttpError(404, 'not_found', 'agent not found'));
      return;
    }
    res.json({ agent: publicAgent(rows[0]) });
  } catch (err) {
    next(err);
  }
});

// POST /v1/agents/me/rotate — rotate the caller's own API key. Returns the
// NEW key (shown once); the old key stops working immediately. Emits
// agent.key_rotated. Backwards compatible: keys that never rotate keep
// working exactly as before. A stricter rotation rate-limit bucket applies
// on top of the general API limit (see rotationRateLimit).
agentsRouter.post('/me/rotate', requireAgent, rotationRateLimit, async (req, res, next) => {
  try {
    const pool = getPool();
    const { token, hash } = generateAgentKey();
    const { rowCount } = await pool.query(
      `UPDATE agents SET api_key_hash = $2, updated_at = now() WHERE id = $1`,
      [req.auth!.id, hash],
    );
    if (!rowCount) {
      next(new HttpError(404, 'not_found', 'agent not found'));
      return;
    }
    await appendEvent(pool, {
      type: 'agent.key_rotated',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      payload: { agent_id: req.auth!.id },
    });
    logger.info('agent key rotated', { agent: req.auth!.name });
    res.json({ api_key: token });
  } catch (err) {
    next(err);
  }
});
