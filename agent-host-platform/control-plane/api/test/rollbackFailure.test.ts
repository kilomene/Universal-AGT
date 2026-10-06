// §6 rollback failure durability (real API + real progress route, pg-mem).
//
// A rollback task can report task-status 'completed' while the rollback
// itself failed — the worker signals that with result.status ===
// 'rollback_failed'. The control plane must persist deployments.status =
// 'rollback_failed' and emit deployment.rollback_failed (NOT
// 'rolled_back'), and the domain-failover hooks must treat it like other
// terminal states.
import http from 'http';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { generateHostToken } from '../src/lib/tokens';
import { NON_ROUTABLE_DEPLOYMENT_STATUSES } from '../src/lib/cloudflare-tunnel';

function setupDb() {
  const db = newDb();
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      permissions JSONB, api_key_hash TEXT,
      status TEXT NOT NULL DEFAULT 'active'
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'online',
      token_hash TEXT NOT NULL,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, host_id TEXT,
      version TEXT, status TEXT NOT NULL DEFAULT 'requested',
      health_status TEXT, task_id TEXT, ports JSONB, domains JSONB,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      created_by TEXT, assigned_to TEXT, claimed_by TEXT,
      payload JSONB NOT NULL DEFAULT '{}', result JSONB, error TEXT,
      priority INT DEFAULT 0, attempts INT DEFAULT 0, max_attempts INT DEFAULT 3,
      started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
      last_progress_at TIMESTAMPTZ, lease_expires_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE port_allocations (
      host_id TEXT NOT NULL, port INT NOT NULL, deployment_id TEXT NOT NULL,
      PRIMARY KEY (host_id, port)
    );
    CREATE TABLE domains (
      id TEXT PRIMARY KEY, hostname TEXT NOT NULL,
      idempotency_key TEXT,
      deployment_id TEXT NOT NULL, host_id TEXT NOT NULL,
      ingress TEXT NOT NULL DEFAULT 'tunnel',
      status TEXT NOT NULL DEFAULT 'requested',
      cf_record_id TEXT,
      dns_configured BOOLEAN NOT NULL DEFAULT false,
      tunnel_configured BOOLEAN NOT NULL DEFAULT false,
      https_reachable BOOLEAN NOT NULL DEFAULT false,
      https_status INT,
      https_checked_at TIMESTAMPTZ,
      verified_at TIMESTAMPTZ,
      error TEXT,
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
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server;

async function req(method: string, path: string, token: string, body?: unknown) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json: any = null;
  try {
    json = await res.json();
  } catch {
    /* empty */
  }
  return { status: res.status, body: json };
}

async function seedHost(name: string) {
  const { token, hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status) VALUES ($1, $2, $3, 'online')`,
    [id, name, hash],
  );
  return { id, token };
}

async function seedRollbackScenario(domainStatus = 'active') {
  const host = await seedHost(`w6-rollback-${randomUUID().slice(0, 8)}`);
  const depId = randomUUID();
  const prevDepId = randomUUID();
  await state.pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status)
     VALUES ($1, 'proj-6', $2, 'v2-bad', 'running')`,
    [depId, host.id],
  );
  await state.pool.query(
    `INSERT INTO domains (id, hostname, deployment_id, host_id, ingress, status)
     VALUES ($1, 'app.example.com', $2, $3, 'tunnel', $4)`,
    [randomUUID(), depId, host.id, domainStatus],
  );
  const taskId = randomUUID();
  await state.pool.query(
    `INSERT INTO tasks (id, type, status, claimed_by, payload, max_attempts)
     VALUES ($1, 'rollback', 'claimed', $2, $3, 3)`,
    [
      taskId,
      host.id,
      JSON.stringify({ deployment_id: depId, target_deployment_id: prevDepId }),
    ],
  );
  return { host, depId, taskId };
}

async function eventTypes(deploymentId: string): Promise<string[]> {
  const { rows } = await state.pool.query(
    `SELECT type FROM events WHERE deployment_id = $1 ORDER BY id`,
    [deploymentId],
  );
  return rows.map((r: any) => r.type as string);
}

async function deploymentStatus(depId: string): Promise<string> {
  const { rows } = await state.pool.query(`SELECT status FROM deployments WHERE id = $1`, [
    depId,
  ]);
  return rows[0].status;
}

beforeAll(async () => {
  setupDb();
  const app = createApp();
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  base = `http://127.0.0.1:${(addr as any).port}`;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

describe('§6 rollback failure durability', () => {
  it('result.status=rollback_failed -> deployment rollback_failed + event, domain failed', async () => {
    const { host, depId, taskId } = await seedRollbackScenario();

    // claimed -> running -> completed (the task state machine requires the
    // running hop; the running report must not move the deployment).
    const running = await req('POST', `/v1/worker/tasks/${taskId}/progress`, host.token, {
      status: 'running',
    });
    expect(running.status).toBe(200);
    expect(await deploymentStatus(depId)).toBe('running');

    const res = await req('POST', `/v1/worker/tasks/${taskId}/progress`, host.token, {
      status: 'completed',
      result: { status: 'rollback_failed', detail: 'container still running old image' },
    });
    expect(res.status).toBe(200);
    expect(res.body.task.status).toBe('completed');

    // The deployment row is honest about the failed rollback.
    expect(await deploymentStatus(depId)).toBe('rollback_failed');

    // The failure event fires, not the success event.
    const types = await eventTypes(depId);
    expect(types).toContain('deployment.rollback_failed');
    expect(types).not.toContain('deployment.rolled_back');

    // Domain failover treated it like other terminal states.
    const { rows: doms } = await state.pool.query(
      `SELECT status FROM domains WHERE deployment_id = $1`,
      [depId],
    );
    expect(doms[0].status).toBe('failed');
    const allTypes = await state.pool.query(`SELECT type FROM events ORDER BY id`);
    expect(allTypes.rows.map((r: any) => r.type)).toContain('domain.failed');
  });

  it('a genuinely successful rollback still records rolled_back (positive control)', async () => {
    const { host, depId, taskId } = await seedRollbackScenario();

    const running = await req('POST', `/v1/worker/tasks/${taskId}/progress`, host.token, {
      status: 'running',
    });
    expect(running.status).toBe(200);

    const res = await req('POST', `/v1/worker/tasks/${taskId}/progress`, host.token, {
      status: 'completed',
      result: { status: 'ok' },
    });
    expect(res.status).toBe(200);

    expect(await deploymentStatus(depId)).toBe('rolled_back');
    const types = await eventTypes(depId);
    expect(types).toContain('deployment.rolled_back');
    expect(types).not.toContain('deployment.rollback_failed');
  });

  it('rollback_failed is non-routable for Cloudflare/tunnel failover', () => {
    expect([...NON_ROUTABLE_DEPLOYMENT_STATUSES]).toContain('rollback_failed');
  });
});
