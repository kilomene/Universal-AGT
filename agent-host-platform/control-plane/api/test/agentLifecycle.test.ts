// §12 agent lifecycle API.
//
// Boots the REAL express app (createApp) against pg-mem:
//   POST /v1/agents/:id/suspend | /resume | /revoke
// Operator authentication only (provisioning token, or an agent with the
// admin permission). Suspend takes effect immediately; revoke invalidates
// the bearer token at once; lifecycle events carry no secret material.
import http from 'http';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { generateAgentKey } from '../src/lib/tokens';

function setupDb() {
  const db = newDb();
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      type TEXT, capabilities JSONB, permissions JSONB,
      api_key_hash TEXT, idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ, metadata JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE events (
      id SERIAL PRIMARY KEY, type TEXT NOT NULL,
      actor_type TEXT, actor_id TEXT, task_id TEXT,
      deployment_id TEXT, host_id TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    -- attachAuth falls through to the hosts lookup on an unknown token.
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      token_hash TEXT, previous_token_hash TEXT,
      previous_token_expires_at TIMESTAMPTZ,
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server;

async function seedAgent(name: string, permissions: Record<string, boolean> = {}) {
  const { token, hash } = generateAgentKey();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO agents (id, name, permissions, api_key_hash, status)
     VALUES ($1, $2, $3, $4, 'active')`,
    [id, name, JSON.stringify(permissions), hash],
  );
  return { id, token };
}

async function wipe() {
  await state.pool.query('DELETE FROM events');
  await state.pool.query('DELETE FROM hosts');
  await state.pool.query('DELETE FROM agents');
}

interface ReqOpts {
  token?: string;
  provisioningToken?: string;
  body?: unknown;
}

async function req(method: string, path: string, opts: ReqOpts = {}) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (opts.token) headers['Authorization'] = `Bearer ${opts.token}`;
  if (opts.provisioningToken) headers['X-Provisioning-Token'] = opts.provisioningToken;
  const res = await fetch(`${base}${path}`, {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  let json: any = null;
  try {
    json = await res.json();
  } catch {
    /* empty */
  }
  return { status: res.status, body: json };
}

async function eventsOf(type: string) {
  const { rows } = await state.pool.query('SELECT * FROM events WHERE type = $1', [type]);
  return rows;
}

const PROV = 'test-provisioning-token-lifecycle';

beforeAll(async () => {
  process.env.UAHT_PROVISIONING_TOKEN = PROV;
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  setupDb();
  const app = createApp();
  server = http.createServer(app);
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  delete process.env.UAHT_PROVISIONING_TOKEN;
  delete process.env.RATE_LIMIT_UNAUTH_PER_MIN;
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

beforeEach(wipe);

describe('§12 agent lifecycle', () => {
  it('suspend takes effect immediately and resume restores the agent', async () => {
    const target = await seedAgent('worker-1', { read_status: true });

    const suspend = await req('POST', `/v1/agents/${target.id}/suspend`, {
      provisioningToken: PROV,
    });
    expect(suspend.status).toBe(200);
    expect(suspend.body.agent.status).toBe('suspended');
    expect(suspend.body.agent).not.toHaveProperty('api_key_hash');

    // The suspended agent's token no longer authorizes normal operations.
    const me = await req('GET', '/v1/agents/me', { token: target.token });
    expect(me.status).toBe(403);

    // Existing rows are untouched (tasks stay durable — nothing deleted).
    const { rows } = await state.pool.query('SELECT status FROM agents WHERE id = $1', [target.id]);
    expect(rows[0].status).toBe('suspended');

    const evts = await eventsOf('agent.suspended');
    expect(evts.length).toBe(1);
    expect(JSON.stringify(evts[0])).not.toContain(target.token);

    const resume = await req('POST', `/v1/agents/${target.id}/resume`, {
      provisioningToken: PROV,
    });
    expect(resume.status).toBe(200);
    expect(resume.body.agent.status).toBe('active');
    const me2 = await req('GET', '/v1/agents/me', { token: target.token });
    expect(me2.status).toBe(200);
    expect((await eventsOf('agent.resumed')).length).toBe(1);
  });

  it('revoke invalidates the bearer token immediately and is permanent', async () => {
    const target = await seedAgent('worker-2', { read_status: true });

    const revoke = await req('POST', `/v1/agents/${target.id}/revoke`, {
      provisioningToken: PROV,
    });
    expect(revoke.status).toBe(200);
    expect(revoke.body.agent.status).toBe('revoked');

    // The old token is dead at once: no agent row matches the hash anymore.
    const me = await req('GET', '/v1/agents/me', { token: target.token });
    expect(me.status).toBe(401);

    // The stored hash was replaced — the old credential cannot be reused.
    const { rows } = await state.pool.query('SELECT api_key_hash FROM agents WHERE id = $1', [
      target.id,
    ]);
    expect(rows[0].api_key_hash).toBeTruthy();

    // Revocation is permanent: resume is rejected, double revoke is rejected.
    expect((await req('POST', `/v1/agents/${target.id}/resume`, { provisioningToken: PROV })).status).toBe(409);
    expect((await req('POST', `/v1/agents/${target.id}/revoke`, { provisioningToken: PROV })).status).toBe(409);

    const evts = await eventsOf('agent.revoked');
    expect(evts.length).toBe(1);
    expect(JSON.stringify(evts[0])).not.toContain(target.token);
    expect(evts[0].payload.token_invalidated).toBe(true);
  });

  it('rejects lifecycle transitions that do not apply', async () => {
    const target = await seedAgent('worker-3', {});
    // Resume an active agent -> 409.
    expect((await req('POST', `/v1/agents/${target.id}/resume`, { provisioningToken: PROV })).status).toBe(409);
    // Suspend twice -> second is 409.
    expect((await req('POST', `/v1/agents/${target.id}/suspend`, { provisioningToken: PROV })).status).toBe(200);
    expect((await req('POST', `/v1/agents/${target.id}/suspend`, { provisioningToken: PROV })).status).toBe(409);
    // Unknown agent -> 404.
    expect(
      (await req('POST', `/v1/agents/${randomUUID()}/suspend`, { provisioningToken: PROV })).status,
    ).toBe(404);
  });

  it('does not allow an agent to suspend or revoke itself', async () => {
    const self = await seedAgent('selfie', { admin: true });
    expect((await req('POST', `/v1/agents/${self.id}/suspend`, { token: self.token })).status).toBe(403);
    expect((await req('POST', `/v1/agents/${self.id}/revoke`, { token: self.token })).status).toBe(403);
    // ...but an admin agent may act on OTHER agents.
    const other = await seedAgent('other', {});
    expect((await req('POST', `/v1/agents/${other.id}/suspend`, { token: self.token })).status).toBe(200);
  });

  it('requires operator authentication', async () => {
    const target = await seedAgent('worker-4', {});
    const plain = await seedAgent('plain', { read_status: true });
    // No credential at all -> 401.
    expect((await req('POST', `/v1/agents/${target.id}/suspend`)).status).toBe(401);
    // Wrong provisioning token -> 401.
    expect(
      (await req('POST', `/v1/agents/${target.id}/suspend`, { provisioningToken: 'wrong' })).status,
    ).toBe(401);
    // Ordinary agent without admin -> 403.
    expect((await req('POST', `/v1/agents/${target.id}/suspend`, { token: plain.token })).status).toBe(403);
    expect((await req('POST', `/v1/agents/${target.id}/resume`, { token: plain.token })).status).toBe(403);
    expect((await req('POST', `/v1/agents/${target.id}/revoke`, { token: plain.token })).status).toBe(403);
  });
});
