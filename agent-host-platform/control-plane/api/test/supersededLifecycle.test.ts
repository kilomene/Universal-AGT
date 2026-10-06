// Issue #2 — the superseded lifecycle.
//
// When a new deployment becomes healthy, the worker stops the previous
// deployment's container and marks it superseded locally. The control
// plane must mirror that: the previous deployment row moves to
// `superseded` (releasing its persistent resource reservation) in the
// same transaction that marks the new deployment `running`. Without
// this, every generation keeps counting against the host and the
// scheduler eventually believes the host is full while only the newest
// deployment is actually running.
//
// `superseded` is NOT resource-consuming, but the row is preserved as a
// rollback target: a successful rollback restores it to `running`.
//
// Boots the REAL express app (createApp) against pg-mem — genuine
// route-level assertions on POST /v1/worker/tasks/:id/progress.
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
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { CONSUMING_DEPLOYMENT_STATUSES } from '../src/lib/scheduler';
import { selectRollbackTarget } from '../src/lib/rollback';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_uuid',
    args: [],
    returns: 'text',
    impure: true,
    implementation: () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`,
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      permissions JSONB, api_key_hash TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'offline',
      capabilities JSONB NOT NULL DEFAULT '[]',
      token_hash TEXT NOT NULL,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      owner_agent_id TEXT,
      runtime TEXT NOT NULL DEFAULT 'docker',
      configuration JSONB NOT NULL DEFAULT '{}'
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT,
      project_id TEXT NOT NULL, host_id TEXT NOT NULL, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested', task_id TEXT,
      health_status TEXT, ports JSONB,
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY DEFAULT gen_uuid(), type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      idempotency_key TEXT,
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
    CREATE TABLE events (
      id SERIAL PRIMARY KEY, type TEXT NOT NULL,
      actor_type TEXT, actor_id TEXT, task_id TEXT,
      deployment_id TEXT, host_id TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE project_members (
      project_id TEXT NOT NULL, agent_id TEXT NOT NULL,
      PRIMARY KEY (project_id, agent_id)
    );
    CREATE TABLE agent_host_access (
      agent_id TEXT NOT NULL, host_id TEXT NOT NULL,
      permissions JSONB NOT NULL DEFAULT '{}',
      PRIMARY KEY (agent_id, host_id)
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server | null = null;

async function startApp() {
  const app = createApp();
  server = http.createServer(app);
  await new Promise<void>((resolve) => server!.listen(0, resolve));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
}

async function stopApp() {
  if (server) {
    await new Promise<void>((resolve) => server!.close(() => resolve()));
    server = null;
  }
}

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

async function seedAgent() {
  const { token, hash } = generateAgentKey();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO agents (id, name, permissions, api_key_hash, status)
     VALUES ($1, 'a1', '{"deploy": true, "read_status": true}', $2, 'active')`,
    [id, hash],
  );
  return { id, token };
}

async function seedHost() {
  const { token, hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, capabilities, total_cpu, total_ram_mb)
     VALUES ($1, 'h1', $2, 'online', '["docker"]', 4, 16384)`,
    [id, hash],
  );
  return { id, token };
}

async function seedProject(name: string, ownerId: string) {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO projects (id, name, owner_agent_id, runtime, configuration)
     VALUES ($1, $2, $3, 'docker', '{"resources": {"cpu": 2, "memory": "8Gi"}}')`,
    [id, name, ownerId],
  );
  return id;
}

async function seedDeployment(projectId: string, hostId: string, version: string, status: string) {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status, health_status, reserved_cpu, reserved_ram_mb)
     VALUES ($1, $2, $3, $4, $5, 'healthy', 2, 8192)`,
    [id, projectId, hostId, version, status],
  );
  return id;
}

async function seedTask(type: string, hostId: string, deploymentId: string, extraPayload: Record<string, unknown> = {}) {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO tasks (id, type, status, claimed_by, payload)
     VALUES ($1, $2, 'claimed', $3, $4)`,
    [id, type, hostId, JSON.stringify({ deployment_id: deploymentId, ...extraPayload })],
  );
  return id;
}

async function deploymentStatus(id: string) {
  const { rows } = await state.pool.query('SELECT status FROM deployments WHERE id = $1', [id]);
  return rows[0].status as string;
}

async function consumingReservation(hostId: string) {
  const { rows } = await state.pool.query(
    `SELECT COALESCE(SUM(reserved_cpu), 0) AS cpu, COALESCE(SUM(reserved_ram_mb), 0) AS ram
     FROM deployments WHERE host_id = $1 AND status = ANY($2)`,
    [hostId, [...CONSUMING_DEPLOYMENT_STATUSES]],
  );
  return { cpu: Number(rows[0].cpu), ram: Number(rows[0].ram) };
}

async function wipe() {
  for (const t of [
    'events', 'port_allocations', 'tasks', 'deployments',
    'agent_host_access', 'project_members', 'hosts', 'agents', 'projects',
  ]) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
}

beforeAll(async () => {
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  setupDb();
  await startApp();
});

afterAll(async () => {
  await stopApp();
});

beforeEach(wipe);

async function completeTask(token: string, taskId: string, result: Record<string, unknown>) {
  const running = await req('POST', `/v1/worker/tasks/${taskId}/progress`, token, {
    status: 'running',
  });
  expect(running.status).toBe(200);
  const done = await req('POST', `/v1/worker/tasks/${taskId}/progress`, token, {
    status: 'completed',
    result,
  });
  expect(done.status).toBe(200);
}

describe('superseded lifecycle (Issue #2)', () => {
  it('completing a deploy marks the previous running deployment superseded', async () => {
    const agent = await seedAgent();
    const host = await seedHost();
    const projectId = await seedProject('p1', agent.id);
    const v1 = await seedDeployment(projectId, host.id, '1.0.0', 'running');
    const v2 = await seedDeployment(projectId, host.id, '2.0.0', 'starting');
    const taskId = await seedTask('deploy', host.id, v2);

    await completeTask(host.token, taskId, {
      status: 'running', health_status: 'healthy', ports: { '8080': 3000 },
    });

    expect(await deploymentStatus(v2)).toBe('running');
    // The leak fix: V1 is no longer `running`.
    expect(await deploymentStatus(v1)).toBe('superseded');
  });

  it('a superseded deployment releases its reservation (host capacity is reusable)', async () => {
    const agent = await seedAgent();
    const host = await seedHost(); // 4 CPU / 16384 MiB
    const projectId = await seedProject('p1', agent.id);
    const v1 = await seedDeployment(projectId, host.id, '1.0.0', 'running');
    const v2 = await seedDeployment(projectId, host.id, '2.0.0', 'starting');
    const taskId = await seedTask('deploy', host.id, v2);

    await completeTask(host.token, taskId, {
      status: 'running', health_status: 'healthy', ports: {},
    });

    // Only V2 consumes now: 2 CPU / 8192 MiB, not 4 / 16384.
    expect(await consumingReservation(host.id)).toEqual({ cpu: 2, ram: 8192 });

    // A third 2-CPU/8Gi deployment fits (before the fix: 2+2+2 > 4 CPU
    // and the scheduler wrongly reported no capacity).
    const dep = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '3.0.0',
    });
    expect(dep.status).toBe(201);
    expect(await deploymentStatus(v1)).toBe('superseded');
  });

  it('a successful rollback restores the superseded target to running', async () => {
    const host = await seedHost();
    const agent = await seedAgent();
    const projectId = await seedProject('p1', agent.id);
    const v1 = await seedDeployment(projectId, host.id, '1.0.0', 'superseded');
    const v2 = await seedDeployment(projectId, host.id, '2.0.0', 'running');
    const taskId = await seedTask('rollback', host.id, v2, { target_deployment_id: v1 });

    await completeTask(host.token, taskId, {
      status: 'rolled_back',
      target_health_status: 'healthy',
      rollback_status: 'succeeded',
      rolled_back_to: v1,
    });

    expect(await deploymentStatus(v2)).toBe('rolled_back');
    // The target serves again: reservation held again, worker/control
    // plane agree.
    expect(await deploymentStatus(v1)).toBe('running');
    expect(await consumingReservation(host.id)).toEqual({ cpu: 2, ram: 8192 });
  });

  it('a confused worker report cannot flip a foreign deployment; the control-plane target wins', async () => {
    const host = await seedHost();
    const agent = await seedAgent();
    const projectId = await seedProject('p1', agent.id);
    const otherProject = await seedProject('p2', agent.id);
    const v1 = await seedDeployment(projectId, host.id, '1.0.0', 'superseded');
    const v2 = await seedDeployment(projectId, host.id, '2.0.0', 'running');
    const foreign = await seedDeployment(otherProject, host.id, '9.9.9', 'superseded');
    const taskId = await seedTask('rollback', host.id, v2, { target_deployment_id: v1 });

    await completeTask(host.token, taskId, {
      status: 'rolled_back',
      target_health_status: 'healthy',
      rollback_status: 'succeeded',
      // Confused/malicious worker names a deployment from another project.
      rolled_back_to: foreign,
    });

    expect(await deploymentStatus(v2)).toBe('rolled_back');
    // The foreign row is untouched (guarded by project+host)...
    expect(await deploymentStatus(foreign)).toBe('superseded');
    // ...and the control plane's own chosen target is the one restored.
    expect(await deploymentStatus(v1)).toBe('running');
  });

  it('only running rows are superseded; other states are untouched', async () => {
    const host = await seedHost();
    await seedAgent();
    const projectId = randomUUID();
    await state.pool.query(
      `INSERT INTO projects (id, name, runtime, configuration) VALUES ($1, 'p1', 'docker', '{}')`,
      [projectId],
    );
    const vOld = await seedDeployment(projectId, host.id, '0.9.0', 'failed');
    const v1 = await seedDeployment(projectId, host.id, '1.0.0', 'running');
    const v2 = await seedDeployment(projectId, host.id, '2.0.0', 'starting');
    const taskId = await seedTask('deploy', host.id, v2);

    await completeTask(host.token, taskId, {
      status: 'running', health_status: 'healthy', ports: {},
    });

    expect(await deploymentStatus(vOld)).toBe('failed');
    expect(await deploymentStatus(v1)).toBe('superseded');
    expect(await deploymentStatus(v2)).toBe('running');
  });
});

describe('superseded is not resource-consuming', () => {
  it('CONSUMING_DEPLOYMENT_STATUSES excludes superseded', () => {
    expect([...CONSUMING_DEPLOYMENT_STATUSES]).not.toContain('superseded');
  });

  it('selectRollbackTarget accepts healthy superseded candidates', () => {
    const current = { id: 'v2', project_id: 'p', host_id: 'h' };
    const target = selectRollbackTarget(current, [
      {
        id: 'v1', project_id: 'p', host_id: 'h', version: '1.0.0',
        status: 'superseded', health_status: 'healthy', created_at: new Date(),
      },
    ]);
    expect(target?.id).toBe('v1');
  });

  it('selectRollbackTarget still rejects unhealthy superseded candidates', () => {
    const current = { id: 'v2', project_id: 'p', host_id: 'h' };
    const target = selectRollbackTarget(current, [
      {
        id: 'v1', project_id: 'p', host_id: 'h', version: '1.0.0',
        status: 'superseded', health_status: 'unhealthy', created_at: new Date(),
      },
    ]);
    expect(target).toBeNull();
  });
});
