// W13b §42 — API-side concurrency: port registry atomicity and
// deployment idempotency under simultaneous requests.
//
// Boots the REAL express app (createApp) against pg-mem via a vi.mock'd
// db/pool, following the e2eFlows.test.ts pattern. The schema mirrors the
// real one where the race mechanics live: UNIQUE(idempotency_key) on
// deployments and tasks, PRIMARY KEY (host_id, port) on port_allocations.
//
// SCOPE NOTE: pg-mem is single-threaded — it cannot reproduce true
// multi-backend MVCC interleavings. What IS honestly exercised here:
//   * the exact production SQL under concurrent callers (Promise.all),
//     proving the unique constraints decide exactly one winner;
//   * the route's 23505 catch path, proving a same-key race loser replays
//     the winner (200 + idempotent_replay) instead of failing;
//   * the port-conflict path, proving a same-port race loser gets 409.
// The worker-side claim race (FOR UPDATE SKIP LOCKED) cannot run under
// pg-mem and is covered in host-worker/tests/test_concurrent_deploy.py
// against a lock-serialized faithful claim.
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
import { allocatePort } from '../src/lib/ports';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_uuid',
    args: [],
    returns: 'text',
    impure: true, // must be re-evaluated per row: pg-mem otherwise folds
    // the DEFAULT once per prepared plan and reuses it (real PostgreSQL
    // evaluates gen_random_uuid() per row)
    implementation: () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`,
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      permissions JSONB, api_key_hash TEXT,
      idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'online',
      token_hash TEXT NOT NULL,
      idempotency_key TEXT,
      created_at TIMESTAMPTZ DEFAULT now(),
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION
    );
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
      owner TEXT, owner_agent_id TEXT, runtime TEXT, configuration JSONB);
    CREATE TABLE project_members (
      project_id TEXT NOT NULL, agent_id TEXT NOT NULL,
      PRIMARY KEY (project_id, agent_id)
    );
    CREATE TABLE agent_host_access (
      agent_id TEXT NOT NULL, host_id TEXT NOT NULL,
      permissions JSONB NOT NULL DEFAULT '{}',
      PRIMARY KEY (agent_id, host_id)
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY DEFAULT gen_uuid(), type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      idempotency_key TEXT UNIQUE,
      created_by TEXT, assigned_to TEXT, claimed_by TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      priority INT DEFAULT 0, attempts INT DEFAULT 0,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
      project_id TEXT NOT NULL, host_id TEXT, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested',
      task_id TEXT, ports JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION
    );
    CREATE TABLE port_allocations (
      host_id TEXT NOT NULL, port INT NOT NULL, deployment_id TEXT NOT NULL,
      PRIMARY KEY (host_id, port)
    );
    CREATE TABLE events (
      id SERIAL PRIMARY KEY, type TEXT NOT NULL,
      actor_type TEXT, actor_id TEXT, task_id TEXT,
      deployment_id TEXT, host_id TEXT,
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

async function seedAgent(permissions: Record<string, boolean>) {
  const { token, hash } = generateAgentKey();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO agents (id, name, permissions, api_key_hash, status)
     VALUES ($1, $2, $3, $4, 'active')`,
    [id, 'racer', JSON.stringify(permissions), hash],
  );
  return { id, token };
}

async function seedHost() {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status)
     VALUES ($1, $2, 'x', 'online')`,
    [id, 'race-host'],
  );
  return id;
}

async function seedProject(name: string) {
  const id = randomUUID();
  await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, $2)`, [id, name]);
  return id;
}

async function wipe() {
  for (const t of ['events', 'port_allocations', 'tasks', 'deployments', 'hosts', 'agents', 'projects']) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
}

async function postDeploy(token: string, body: unknown) {
  const res = await fetch(`${base}/v1/deployments`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  let json: any = null;
  try {
    json = await res.json();
  } catch { /* empty */ }
  return { status: res.status, body: json };
}

beforeAll(async () => {
  setupDb();
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

beforeEach(async () => {
  await wipe();
});

// ---------------------------------------------------------------------------
// Port registry: concurrent allocatePort of the same (host, port)
// ---------------------------------------------------------------------------
describe('allocatePort under concurrency', () => {
  it('exactly one winner; the rest get 23505', async () => {
    const hostId = randomUUID();
    const port = 8080;
    const attempts = await Promise.all(
      Array.from({ length: 8 }, (_, i) =>
        allocatePort(state.pool, hostId, port, `dep-${i}`)
          .then(() => 'ok')
          .catch((e: any) => e.code ?? String(e)),
      ),
    );
    expect(attempts.filter((a) => a === 'ok')).toHaveLength(1);
    expect(attempts.filter((a) => a === '23505')).toHaveLength(7);
    const { rows } = await state.pool.query(
      `SELECT deployment_id FROM port_allocations WHERE host_id = $1 AND port = $2`,
      [hostId, port],
    );
    expect(rows).toHaveLength(1);
  });

  it('different ports on the same host do not interfere', async () => {
    const hostId = randomUUID();
    const attempts = await Promise.all(
      Array.from({ length: 8 }, (_, i) =>
        allocatePort(state.pool, hostId, 9000 + i, `dep-${i}`)
          .then(() => 'ok')
          .catch((e: any) => e.code ?? String(e)),
      ),
    );
    expect(attempts.every((a) => a === 'ok')).toBe(true);
    const { rows } = await state.pool.query(
      `SELECT count(*)::int AS n FROM port_allocations WHERE host_id = $1`,
      [hostId],
    );
    expect(rows[0].n).toBe(8);
  });
});

// ---------------------------------------------------------------------------
// POST /v1/deployments idempotency under concurrency
// ---------------------------------------------------------------------------
describe('POST /v1/deployments idempotency under concurrency', () => {
  it('same key + same body, 6 simultaneous -> exactly one deployment; losers replay', async () => {
    const agent = await seedAgent({ deploy: true });
    const hostId = await seedHost();
    const projectId = await seedProject('race-proj');
    const key = `race-key-${randomUUID()}`;
    const body = {
      project_id: projectId,
      host_id: hostId,
      version: '1.0.0',
      idempotency_key: key,
    };

    const responses = await Promise.all(
      Array.from({ length: 6 }, () => postDeploy(agent.token, body)),
    );

    // Every response is a success: the first 201, the race losers 200
    // replays of the winner (the route's 23505 catch path).
    for (const r of responses) {
      expect([200, 201]).toContain(r.status);
      if (r.status === 200) {
        expect(r.body.idempotent_replay).toBe(true);
      }
    }
    expect(responses.filter((r) => r.status === 201)).toHaveLength(1);

    // Exactly one deployment and one deploy task exist.
    const { rows: deps } = await state.pool.query(
      `SELECT id FROM deployments WHERE idempotency_key = $1`, [key]);
    expect(deps).toHaveLength(1);
    const { rows: tasks } = await state.pool.query(
      `SELECT id FROM tasks WHERE idempotency_key = $1`, [`deploy:${key}`]);
    expect(tasks).toHaveLength(1);
    // All responses (201 and replays) name the same deployment.
    const ids = new Set(responses.map((r) => r.body.deployment.id));
    expect(ids.size).toBe(1);
    expect([...ids][0]).toBe(deps[0].id);
  });

  // Sanity check that dedup does not over-fire. (pg-mem folds an
  // unmarked mock DEFAULT once per prepared plan; the gen_uuid mock is
  // registered impure so it is re-evaluated per row, as PostgreSQL does.)
  it('different keys concurrently -> independent deployments', async () => {
    const agent = await seedAgent({ deploy: true });
    const hostId = await seedHost();
    const projectId = await seedProject('race-proj-2');

    const responses = await Promise.all(
      ['k1', 'k2'].map((k) =>
        postDeploy(agent.token, {
          project_id: projectId,
          host_id: hostId,
          version: '1.0.0',
          idempotency_key: `${k}-${randomUUID()}`,
        }),
      ),
    );
    expect(responses.map((r) => r.status)).toEqual([201, 201]);
    const { rows } = await state.pool.query(
      `SELECT count(*)::int AS n FROM deployments WHERE project_id = $1`,
      [projectId],
    );
    expect(rows[0].n).toBe(2);
  });

  it('same host_port, different keys, concurrently -> one 201, one 409', async () => {
    const agent = await seedAgent({ deploy: true });
    const hostId = await seedHost();
    const projectId = await seedProject('race-proj-3');
    const mk = (k: string) => ({
      project_id: projectId,
      host_id: hostId,
      version: '1.0.0',
      idempotency_key: `${k}-${randomUUID()}`,
      host_port: 8123,
    });

    const responses = await Promise.all(
      [postDeploy(agent.token, mk('a')), postDeploy(agent.token, mk('b'))],
    );
    const statuses = responses.map((r) => r.status).sort();
    expect(statuses).toEqual([201, 409]);
    const { rows } = await state.pool.query(
      `SELECT deployment_id FROM port_allocations WHERE host_id = $1 AND port = 8123`,
      [hostId],
    );
    expect(rows).toHaveLength(1);
  });
});
