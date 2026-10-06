// Registration idempotency (§7) tests (real express app + pg-mem).
//
// POST /v1/hosts/register and POST /v1/agents/register accept an optional
// idempotency_key:
//   * first send with a key -> 201 with fresh credentials;
//   * retry with the same key + equal parameters -> 200 replay with a NEW
//     credential (the original is unrecoverable — only its hash is stored)
//     and idempotent_replay:true; exactly one row exists;
//   * same key + different parameters -> 409 conflict;
//   * no key -> unchanged behavior (duplicate name -> 409);
//   * malformed key -> 400.
import http from 'http';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { resetRateLimitBucketsForTests } from '../src/middleware/rateLimit';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'test_gen_id',
    returns: 'text',
    impure: true,
    implementation: () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`,
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(), name TEXT NOT NULL UNIQUE, type TEXT,
      capabilities JSONB, permissions JSONB, api_key_hash TEXT,
      idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ, metadata JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(), name TEXT NOT NULL UNIQUE,
      host_type TEXT,
      status TEXT NOT NULL DEFAULT 'online', token_hash TEXT NOT NULL,
      idempotency_key TEXT,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      capabilities JSONB, worker_version TEXT,
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      worker_status TEXT,
      ingress JSONB NOT NULL DEFAULT '{}',
      reported_host_name TEXT,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      disk_pct DOUBLE PRECISION, docker_status TEXT,
      running_apps JSONB, total_cpu DOUBLE PRECISION,
      total_ram_mb DOUBLE PRECISION, total_disk_gb DOUBLE PRECISION,
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
    -- minimal: the heartbeat handler counts queued tasks for the host.
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(), type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      assigned_to TEXT, claimed_by TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server;

async function post(path: string, body: unknown, provHeader = true) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (provHeader) headers['X-Provisioning-Token'] = 'prov-token';
  const res = await fetch(`${base}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  let json: any = null;
  try {
    json = await res.json();
  } catch {
    /* empty */
  }
  return { status: res.status, body: json };
}

async function count(table: string): Promise<number> {
  const { rows } = await state.pool.query(`SELECT count(*)::int AS n FROM ${table}`);
  return rows[0].n;
}

beforeAll(async () => {
  process.env.UAHT_PROVISIONING_TOKEN = 'prov-token';
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  setupDb();
  const app = createApp();
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  base = `http://127.0.0.1:${(addr as any).port}`;
});

afterAll(async () => {
  delete process.env.UAHT_PROVISIONING_TOKEN;
  delete process.env.RATE_LIMIT_UNAUTH_PER_MIN;
  server.close();
});

beforeEach(async () => {
  resetRateLimitBucketsForTests();
  await state.pool.query('DELETE FROM events');
  await state.pool.query('DELETE FROM hosts');
  await state.pool.query('DELETE FROM agents');
});

describe('host registration idempotency (§7)', () => {
  it('registers with a key (201), replays with equal params (200 + fresh token, one row)', async () => {
    const params = { name: 'h-idem', host_type: 'persistent-linux-host', capabilities: ['docker'], idempotency_key: 'key-1' };
    const first = await post('/v1/hosts/register', params);
    expect(first.status).toBe(201);
    expect(first.body.host_token).toMatch(/^uagh_/);
    expect(first.body.idempotent_replay).toBeUndefined();

    const retry = await post('/v1/hosts/register', params);
    expect(retry.status).toBe(200);
    expect(retry.body.idempotent_replay).toBe(true);
    expect(retry.body.host.id).toBe(first.body.host.id);
    // Original token is unrecoverable (hash only): replay mints a fresh one.
    expect(retry.body.host_token).toMatch(/^uagh_/);
    expect(retry.body.host_token).not.toBe(first.body.host_token);
    expect(await count('hosts')).toBe(1);

    // The fresh token authenticates as the host (heartbeat is host-authed).
    const hb = await fetch(`${base}/v1/hosts/${first.body.host.id}/heartbeat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${retry.body.host_token}`,
      },
      body: '{}',
    });
    expect(hb.status).toBe(200);
  });

  it('same key with different params -> 409 conflict', async () => {
    const first = await post('/v1/hosts/register', { name: 'h-a', idempotency_key: 'key-2' });
    expect(first.status).toBe(201);
    const conflict = await post('/v1/hosts/register', { name: 'h-B-DIFFERENT', idempotency_key: 'key-2' });
    expect(conflict.status).toBe(409);
    expect(await count('hosts')).toBe(1);
  });

  it('no key keeps legacy behavior: duplicate name -> 409', async () => {
    expect((await post('/v1/hosts/register', { name: 'h-legacy' })).status).toBe(201);
    const dup = await post('/v1/hosts/register', { name: 'h-legacy' });
    expect(dup.status).toBe(409);
    expect(dup.body.error.code).toBe('conflict');
  });

  it('malformed idempotency_key -> 400', async () => {
    expect((await post('/v1/hosts/register', { name: 'h-bad1', idempotency_key: '' })).status).toBe(400);
    expect((await post('/v1/hosts/register', { name: 'h-bad2', idempotency_key: 'x'.repeat(129) })).status).toBe(
      400,
    );
    expect((await post('/v1/hosts/register', { name: 'h-bad3', idempotency_key: 42 })).status).toBe(400);
  });

  it('emits host.token_rotated on replay', async () => {
    const params = { name: 'h-ev', idempotency_key: 'key-3' };
    await post('/v1/hosts/register', params);
    await post('/v1/hosts/register', params);
    const { rows } = await state.pool.query(`SELECT type FROM events ORDER BY id`);
    const types = rows.map((r: any) => r.type);
    expect(types).toContain('host.registered');
    expect(types).toContain('host.token_rotated');
  });
});

describe('agent registration idempotency (§7)', () => {
  it('registers with a key (201), replays with equal params (200 + fresh key, one row)', async () => {
    const params = {
      name: 'a-idem',
      type: 'claude',
      permissions: { deploy: true },
      idempotency_key: 'akey-1',
    };
    const first = await post('/v1/agents/register', params);
    expect(first.status).toBe(201);
    expect(first.body.api_key).toBeTruthy();

    const retry = await post('/v1/agents/register', params);
    expect(retry.status).toBe(200);
    expect(retry.body.idempotent_replay).toBe(true);
    expect(retry.body.agent.id).toBe(first.body.agent.id);
    expect(retry.body.api_key).not.toBe(first.body.api_key);
    expect(await count('agents')).toBe(1);

    // Fresh key authenticates (/v1/agents/me is agent-authed).
    const me = await fetch(`${base}/v1/agents/me`, {
      headers: { Authorization: `Bearer ${retry.body.api_key}` },
    });
    expect(me.status).toBe(200);
  });

  it('same key with different params -> 409 conflict', async () => {
    expect((await post('/v1/agents/register', { name: 'a-x', idempotency_key: 'akey-2' })).status).toBe(201);
    const conflict = await post('/v1/agents/register', { name: 'a-x', type: 'other-type', idempotency_key: 'akey-2' });
    expect(conflict.status).toBe(409);
    expect(await count('agents')).toBe(1);
  });
});
