import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { generateAgentKey } from '../lib/tokens';
import { requireAgent } from '../middleware/auth';
import { publicAgent } from './_helpers';

export const agentsRouter = Router();

// POST /v1/agents/register — open bootstrap endpoint (first agent has no key
// yet). Later registrations are a normal API call; operators should front this
// with network policy in production (see README).
agentsRouter.post('/register', async (req, res, next) => {
  try {
    const { name, type, capabilities, permissions } = req.body ?? {};
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
    const { token, hash } = generateAgentKey();
    try {
      const { rows } = await pool.query(
        `INSERT INTO agents (name, type, capabilities, permissions, api_key_hash)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING id, name, type, capabilities, permissions, status, last_seen, metadata, created_at, updated_at`,
        [
          name.trim(),
          typeof type === 'string' && type ? type : 'generic',
          JSON.stringify(capabilities ?? []),
          JSON.stringify(permissions ?? {}),
          hash,
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
