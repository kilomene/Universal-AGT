// §61 version-enforcement tests (control-plane API side).
//
// Boots the REAL express app (createApp) against a pg-mem database via a
// vi.mock'd db/pool — genuine route-level assertions (status codes and
// response bodies), not handler unit tests:
//
//   1. POST /v1/hosts/register rejects a worker that positively identifies
//      as older than MIN_WORKER_VERSION (426 + code "worker_outdated") —
//      incompatible versions never silently interoperate.
//   2. Registration still accepts the floor version, newer versions, a
//      missing version (legacy), and an unparseable version (custom builds).
//   3. POST /v1/hosts/:id/heartbeat reports api_version /
//      min_worker_version / worker_outdated, and an outdated heartbeat emits
//      exactly one host.worker_outdated event per process.
//   4. GET /v1/health serves the API version from the single source of
//      truth (lib/versions), plus the worker floor.
import http from 'http';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { API_VERSION, MIN_WORKER_VERSION } from '../src/lib/versions';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'test_gen_id',
    args: [],
    returns: 'text',
    impure: true,
    implementation: () => `row-${++n}`,
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(),
      name TEXT NOT NULL, type TEXT,
      capabilities JSONB, permissions JSONB,
      api_key_hash TEXT,
      idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active', last_seen TIMESTAMPTZ,
      metadata JSONB, created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(),
      name TEXT NOT NULL, host_type TEXT,
      status TEXT NOT NULL DEFAULT 'online',
      capabilities JSONB, worker_version TEXT,
      token_hash TEXT NOT NULL,
      idempotency_key TEXT,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      disk_pct DOUBLE PRECISION, docker_status TEXT,
      running_apps JSONB, total_cpu DOUBLE PRECISION,
      total_ram_mb DOUBLE PRECISION, total_disk_gb DOUBLE PRECISION,
      last_seen TIMESTAMPTZ, metadata JSONB,
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      worker_status TEXT,
      ingress JSONB NOT NULL DEFAULT '{}',
      reported_host_name TEXT,
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
    -- heartbeat reads pending queued tasks; only the queried columns are needed
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'queued',
      assigned_to TEXT
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server;

async function seedAgent(name: string, permissions: Record<string, boolean>) {
  const { token, hash } = generateAgentKey();
  await state.pool.query(
    `INSERT INTO agents (name, permissions, api_key_hash, status)
     VALUES ($1, $2, $3, 'active')`,
    [name, JSON.stringify(permissions), hash],
  );
  return token;
}

async function seedHost(id: string, name: string, workerVersion: string | null = null) {
  const { token, hash } = generateHostToken();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, worker_version)
     VALUES ($1, $2, $3, 'online', $4)`,
    [id, name, hash, workerVersion],
  );
  return token;
}

async function post(path: string, body: unknown, headers: Record<string, string> = {}) {
  const res = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let json: any = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { status: res.status, json, text };
}

let deployToken = '';

beforeAll(async () => {
  process.env.DATA_ENCRYPTION_KEY = 'ab'.repeat(32);
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  setupDb();
  deployToken = await seedAgent('deployer', { deploy: true });
  const app = createApp();
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await state.pool.end();
  await new Promise<void>((r) => server.close(() => r()));
});

describe('host registration worker-version gate (§61)', () => {
  it('rejects a worker older than MIN_WORKER_VERSION with 426 worker_outdated', async () => {
    const { status, json } = await post(
      '/v1/hosts/register',
      { name: 'h-old', worker_version: '0.0.1' },
      { Authorization: `Bearer ${deployToken}` },
    );
    expect(status).toBe(426);
    expect(json?.error?.code).toBe('worker_outdated');
    expect(json?.error?.message).toContain(MIN_WORKER_VERSION);
  });

  it('accepts the floor version, a newer version, and the current release', async () => {
    for (const v of [MIN_WORKER_VERSION, '0.2.0', API_VERSION]) {
      const { status, json } = await post(
        '/v1/hosts/register',
        { name: `h-ok-${v}`, worker_version: v },
        { Authorization: `Bearer ${deployToken}` },
      );
      expect(status).toBe(201);
      expect(json?.host?.worker_version).toBe(v);
    }
  });

  it('still accepts a missing or unparseable worker_version (legacy/custom builds)', async () => {
    for (const [name, v] of [
      ['h-legacy', undefined],
      ['h-custom', 'nightly'],
    ] as const) {
      const body: Record<string, unknown> = { name };
      if (v !== undefined) body.worker_version = v;
      const { status } = await post('/v1/hosts/register', body, {
        Authorization: `Bearer ${deployToken}`,
      });
      expect(status).toBe(201);
    }
  });
});

describe('heartbeat version reporting (§61)', () => {
  const H1 = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

  it('flags an outdated worker and emits exactly one host.worker_outdated event', async () => {
    const hostToken = await seedHost(H1, 'h-beat', MIN_WORKER_VERSION);
    // heartbeat carrying an older worker version than the floor
    let r = await post(
      `/v1/hosts/${H1}/heartbeat`,
      { worker_version: '0.0.7' },
      { Authorization: `Bearer ${hostToken}` },
    );
    expect(r.status).toBe(200);
    expect(r.json?.api_version).toBe(API_VERSION);
    expect(r.json?.min_worker_version).toBe(MIN_WORKER_VERSION);
    expect(r.json?.worker_outdated).toBe(true);
    expect(r.json?.host?.worker_version).toBe('0.0.7');

    const ev1 = await state.pool.query(
      `SELECT count(*)::int AS n FROM events WHERE type = 'host.worker_outdated' AND host_id = $1`,
      [H1],
    );
    expect(ev1.rows[0].n).toBe(1);

    // a second outdated heartbeat still reports the flag but emits no
    // second event (first-appearance dedupe, one durable signal per process)
    r = await post(
      `/v1/hosts/${H1}/heartbeat`,
      { worker_version: '0.0.7' },
      { Authorization: `Bearer ${hostToken}` },
    );
    expect(r.status).toBe(200);
    expect(r.json?.worker_outdated).toBe(true);
    const ev2 = await state.pool.query(
      `SELECT count(*)::int AS n FROM events WHERE type = 'host.worker_outdated' AND host_id = $1`,
      [H1],
    );
    expect(ev2.rows[0].n).toBe(1);
  });

  it('reports worker_outdated=false for a current worker and emits no event', async () => {
    const H2 = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
    const hostToken = await seedHost(H2, 'h-beat-ok', API_VERSION);
    const r = await post(
      `/v1/hosts/${H2}/heartbeat`,
      { worker_version: API_VERSION },
      { Authorization: `Bearer ${hostToken}` },
    );
    expect(r.status).toBe(200);
    expect(r.json?.worker_outdated).toBe(false);
    const ev = await state.pool.query(
      `SELECT count(*)::int AS n FROM events WHERE type = 'host.worker_outdated' AND host_id = $1`,
      [H2],
    );
    expect(ev.rows[0].n).toBe(0);
  });
});

describe('health version surface (§61)', () => {
  it('serves the API version and the worker floor from the single source of truth', async () => {
    const res = await fetch(`${base}/v1/health`);
    expect(res.status).toBe(200);
    const json = await res.json();
    expect(json?.ok).toBe(true);
    expect(json?.version).toBe(API_VERSION);
    expect(json?.min_worker_version).toBe(MIN_WORKER_VERSION);
  });
});
