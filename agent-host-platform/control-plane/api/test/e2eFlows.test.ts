// Phase 9 API-level flow tests (control-plane side of the E2E story).
//
// Boots the REAL express app (createApp) against a pg-mem database via a
// vi.mock'd db/pool — genuine route-level assertions (status codes, row
// state, event order), not handler unit tests. These cover the flows the
// Python E2E suite (host-worker/tests/test_e2e.py) drives end-to-end:
//
//   agent creates deployment (auto/manual) -> approve/reject gate ->
//   worker progress (running -> completed/failed) -> deployment mirrored ->
//   cancel semantics
//
// SCOPE NOTE: the worker claim route (POST /v1/worker/tasks/claim) cannot
// run under pg-mem — its atomic-claim SQL uses FOR UPDATE SKIP LOCKED,
// which pg-mem's planner does not support. The claim step is therefore
// simulated here with a direct status update, and is covered for real by
// host-worker/tests/test_e2e.py against a faithful in-memory control plane
// (real worker HTTP client + real dispatcher + real pipeline).
import http from 'http';
import { tmpdir } from 'os';
import { mkdtempSync, existsSync, readFileSync } from 'fs';
import { join } from 'path';
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

// ---------------------------------------------------------------------------
// Test database (superset of the security.test.ts schema: adds the
// deployments columns the progress mirror touches, plus port_allocations)
// ---------------------------------------------------------------------------
function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_uuid',
    args: [],
    returns: 'text',
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
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
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
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY, project_id TEXT, filename TEXT,
      storage_path TEXT, checksum TEXT, size BIGINT,
      version TEXT, status TEXT NOT NULL DEFAULT 'pending',
      manifest JSONB
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
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT,
      project_id TEXT NOT NULL, host_id TEXT, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested',
      health_status TEXT, task_id TEXT,
      ports JSONB, domains JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION
    );
    CREATE TABLE port_allocations (
      host_id TEXT NOT NULL, port INT NOT NULL, deployment_id TEXT NOT NULL,
      PRIMARY KEY (host_id, port)
    );
    CREATE TABLE domains (
      id TEXT PRIMARY KEY, hostname TEXT NOT NULL,
      idempotency_key TEXT,
      deployment_id TEXT, host_id TEXT,
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
let logDir = '';

async function seedAgent(name: string, permissions: Record<string, boolean>) {
  const { token, hash } = generateAgentKey();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO agents (id, name, permissions, api_key_hash, status)
     VALUES ($1, $2, $3, $4, 'active')`,
    [id, name, JSON.stringify(permissions), hash],
  );
  return { id, token };
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

async function seedProject(name: string) {
  const id = randomUUID();
  await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, $2)`, [id, name]);
  return id;
}

async function wipe() {
  for (const t of ['events', 'domains', 'port_allocations', 'tasks', 'deployments', 'artifacts', 'hosts', 'agents', 'projects']) {
    await state.pool.query(`DELETE FROM ${t}`);
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
    /* 204 / empty */
  }
  return { status: res.status, body: json };
}

async function eventTypes(): Promise<string[]> {
  const { rows } = await state.pool.query(`SELECT type FROM events ORDER BY id`);
  return rows.map((r: any) => r.type);
}

/** Simulate the (pg-mem-untestable) claim step: queued -> claimed by host. */
async function simulateClaim(taskId: string, hostId: string) {
  await state.pool.query(
    `UPDATE tasks SET status = 'claimed', claimed_by = $2, assigned_to = $2,
     attempts = attempts + 1, last_progress_at = now() WHERE id = $1`,
    [taskId, hostId],
  );
  await state.pool.query(
    `INSERT INTO events (type, actor_type, actor_id, task_id, host_id, payload)
     VALUES ('task.claimed', 'host', 'e2e-host', $1, $2, '{}')`,
    [taskId, hostId],
  );
}

beforeAll(async () => {
  logDir = mkdtempSync(join(tmpdir(), 'uaht-e2e-logs-'));
  process.env.LOG_DIR = logDir;
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
// Deployment creation: auto vs manual mode
// ---------------------------------------------------------------------------
describe('POST /v1/deployments (mode routing)', () => {
  it('automatic mode: deployment requested + task queued', async () => {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');

    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'automatic',
    });
    expect(status).toBe(201);
    expect(body.deployment.status).toBe('requested');
    expect(body.deployment.mode).toBe('automatic');
    expect(body.task.type).toBe('deploy');
    expect(body.task.status).toBe('queued');
    expect(body.task.payload.deployment_id).toBe(body.deployment.id);
    expect(body.task.payload.project_id).toBe(project);
    expect(await eventTypes()).toEqual(['deployment.requested', 'task.created']);
  });

  it('injects the verified artifact checksum+size into the deploy task payload', async () => {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const artifactId = randomUUID();
    await state.pool.query(
      `INSERT INTO artifacts (id, project_id, filename, checksum, size, status)
       VALUES ($1, $2, 'demo-app.tar.gz', $3, $4, 'ready')`,
      [artifactId, project, 'sha256:' + 'ab'.repeat(32), 12345],
    );

    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0',
      artifact_id: artifactId, mode: 'automatic',
    });
    expect(status).toBe(201);
    // The worker refuses artifact deploys without artifact_checksum in the
    // payload (verified before any docker call) — the control plane must
    // inject it from the verified artifact row.
    expect(body.task.payload.artifact_checksum).toBe('sha256:' + 'ab'.repeat(32));
    expect(body.task.payload.artifact_size).toBe(12345);
  });

  it('422s a deployment whose artifact has no verified checksum', async () => {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const artifactId = randomUUID();
    await state.pool.query(
      `INSERT INTO artifacts (id, project_id, filename, checksum, size, status)
       VALUES ($1, $2, 'demo-app.tar.gz', NULL, 10, 'ready')`,
      [artifactId, project],
    );
    const { status } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0',
      artifact_id: artifactId, mode: 'automatic',
    });
    expect(status).toBe(422);
  });

  it('manual mode: deployment requested + task awaiting_approval (not claimable)', async () => {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');

    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'manual',
    });
    expect(status).toBe(201);
    expect(body.task.status).toBe('awaiting_approval');
    expect(body.task.payload.deployment_id).toBe(body.deployment.id);
  });
});

// ---------------------------------------------------------------------------
// Approval gate
// ---------------------------------------------------------------------------
describe('POST /v1/tasks/:id/approve + /reject', () => {
  async function manualTask() {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const { body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'manual',
    });
    return { agent, host, task: body.task, deployment: body.deployment };
  }

  it('403s approve by an agent WITHOUT approve_deployments', async () => {
    const { task } = await manualTask();
    const weak = await seedAgent('weak', { deploy: true }); // no approve_deployments
    const { status } = await req('POST', `/v1/tasks/${task.id}/approve`, weak.token);
    expect(status).toBe(403);
  });

  it('403s reject by an agent WITHOUT approve_deployments', async () => {
    const { task } = await manualTask();
    const weak = await seedAgent('weak', { deploy: true });
    const { status } = await req('POST', `/v1/tasks/${task.id}/reject`, weak.token);
    expect(status).toBe(403);
  });

  it('409s approve on a task that is not awaiting_approval', async () => {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const { body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'automatic',
    });
    const { status } = await req('POST', `/v1/tasks/${body.task.id}/approve`, agent.token);
    expect(status).toBe(409); // queued tasks need no approval
  });

  it('approve: awaiting_approval -> queued, deployment approved, events in order', async () => {
    const { agent, task, deployment } = await manualTask();
    const { status, body } = await req('POST', `/v1/tasks/${task.id}/approve`, agent.token);
    expect(status).toBe(200);
    expect(body.task.status).toBe('queued');
    const dep = await state.pool.query(`SELECT status FROM deployments WHERE id = $1`, [deployment.id]);
    expect(dep.rows[0].status).toBe('approved');
    expect(await eventTypes()).toEqual([
      'deployment.requested', 'task.created',
      'deployment.approved', 'task.approved',
    ]);
  });

  it('second approve 409s (already queued)', async () => {
    const { agent, task } = await manualTask();
    await req('POST', `/v1/tasks/${task.id}/approve`, agent.token);
    const { status } = await req('POST', `/v1/tasks/${task.id}/approve`, agent.token);
    expect(status).toBe(409);
  });

  it('reject: awaiting_approval -> cancelled, deployment never starts', async () => {
    const { agent, task, deployment } = await manualTask();
    const { status, body } = await req('POST', `/v1/tasks/${task.id}/reject`, agent.token);
    expect(status).toBe(200);
    expect(body.task.status).toBe('cancelled');
    const dep = await state.pool.query(`SELECT status FROM deployments WHERE id = $1`, [deployment.id]);
    expect(dep.rows[0].status).toBe('requested'); // never approved, never deployed
    expect(await eventTypes()).toEqual([
      'deployment.requested', 'task.created', 'task.rejected',
    ]);
  });
});

// ---------------------------------------------------------------------------
// Worker progress: the deploy happy path and failure path, incl. deployment
// mirroring, port persistence, and event order.
// ---------------------------------------------------------------------------
describe('POST /v1/worker/tasks/:id/progress (deploy lifecycle)', () => {
  async function autoDeployment() {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const { body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'automatic',
    });
    await simulateClaim(body.task.id, host.id);
    return { agent, host, task: body.task, deployment: body.deployment };
  }

  it('running -> completed: deployment mirrored, ports persisted, events in order, logs written', async () => {
    const { host, task, deployment } = await autoDeployment();

    const r1 = await req('POST', `/v1/worker/tasks/${task.id}/progress`, host.token, {
      status: 'running', log_chunk: 'build starting\n',
    });
    expect(r1.status).toBe(200);
    expect(r1.body.task.status).toBe('running');

    const r2 = await req('POST', `/v1/worker/tasks/${task.id}/progress`, host.token, {
      status: 'completed',
      log_chunk: 'deploy OK\n',
      result: { deployment_id: deployment.id, status: 'running', ports: { '18001': 3000 } },
    });
    expect(r2.status).toBe(200);
    expect(r2.body.task.status).toBe('completed');

    const dep = await state.pool.query(`SELECT status, health_status, ports FROM deployments WHERE id = $1`, [deployment.id]);
    expect(dep.rows[0].status).toBe('running');
    expect(dep.rows[0].health_status).toBe('healthy');
    expect(dep.rows[0].ports).toEqual({ '18001': 3000 });

    expect(await eventTypes()).toEqual([
      'deployment.requested', 'task.created', 'task.claimed',
      'task.started', 'deployment.started',
      'task.completed', 'deployment.completed',
    ]);

    const logPath = join(logDir, `${task.id}.log`);
    expect(existsSync(logPath)).toBe(true);
    const content = readFileSync(logPath, 'utf8');
    expect(content).toContain('build starting');
    expect(content).toContain('deploy OK');
  });

  it('running -> failed: deployment failed, events recorded', async () => {
    const { host, task, deployment } = await autoDeployment();
    await req('POST', `/v1/worker/tasks/${task.id}/progress`, host.token, { status: 'running' });
    const r = await req('POST', `/v1/worker/tasks/${task.id}/progress`, host.token, {
      status: 'failed', error: 'docker build exploded',
    });
    expect(r.status).toBe(200);
    expect(r.body.task.status).toBe('failed');
    const dep = await state.pool.query(`SELECT status, health_status FROM deployments WHERE id = $1`, [deployment.id]);
    expect(dep.rows[0].status).toBe('failed');
    expect(dep.rows[0].health_status).toBe('unhealthy');
    expect(await eventTypes()).toEqual([
      'deployment.requested', 'task.created', 'task.claimed',
      'task.started', 'deployment.started',
      'task.failed', 'deployment.failed',
    ]);
  });

  it('failed from claimed with retry budget left: parked in retrying, not terminal', async () => {
    const { host, task } = await autoDeployment(); // attempts = 1, max_attempts = 3
    const r = await req('POST', `/v1/worker/tasks/${task.id}/progress`, host.token, {
      status: 'failed', error: 'died before running',
    });
    expect(r.status).toBe(200);
    expect(r.body.task.status).toBe('retrying');
    expect(await eventTypes()).toContain('task.retrying');
  });

  it('409s an illegal transition (claimed -> completed)', async () => {
    const { host, task } = await autoDeployment();
    const r = await req('POST', `/v1/worker/tasks/${task.id}/progress`, host.token, { status: 'completed' });
    expect(r.status).toBe(409);
  });

  it('403s progress reported by a host that did not claim the task', async () => {
    const { task } = await autoDeployment();
    const other = await seedHost('h2');
    const r = await req('POST', `/v1/worker/tasks/${task.id}/progress`, other.token, { status: 'running' });
    expect(r.status).toBe(403);
  });

  it('401s progress with a bogus token', async () => {
    const { task } = await autoDeployment();
    const r = await req('POST', `/v1/worker/tasks/${task.id}/progress`, 'bogus', { status: 'running' });
    expect(r.status).toBe(401);
  });
});

// ---------------------------------------------------------------------------
// Cancel semantics
// ---------------------------------------------------------------------------
describe('POST /v1/tasks/:id/cancel', () => {
  async function autoTask(perm: Record<string, boolean> = { deploy: true, approve_deployments: true }) {
    const agent = await seedAgent('deployer', perm);
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const { body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'automatic',
    });
    return { agent, host, task: body.task };
  }

  it('creator with only read_status can cancel their own queued task', async () => {
    const { agent, task } = await autoTask({ read_status: true, deploy: true });
    // deploy:true needed to create the deployment; drop to creator-only for cancel:
    await state.pool.query(`UPDATE agents SET permissions = '{}' WHERE id = $1`, [agent.id]);
    const { status, body } = await req('POST', `/v1/tasks/${task.id}/cancel`, agent.token);
    expect(status).toBe(200);
    expect(body.task.status).toBe('cancelled');
    expect(await eventTypes()).toContain('task.cancelled');
  });

  it('403s cancel by a non-owner without deploy', async () => {
    // §10: the project is OWNED here (not legacy-open), so the stranger has
    // no project access and cannot cancel the owner's task.
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    await state.pool.query(`UPDATE projects SET owner_agent_id = $1 WHERE id = $2`, [
      agent.id,
      project,
    ]);
    const { body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '1.0.0', mode: 'automatic',
    });
    expect(body.deployment).toBeDefined();
    const stranger = await seedAgent('stranger', { read_status: true });
    const { status } = await req('POST', `/v1/tasks/${body.task.id}/cancel`, stranger.token);
    expect(status).toBe(403);
  });

  it('cancels a running task (claimed by a host)', async () => {
    const { agent, host, task } = await autoTask();
    await simulateClaim(task.id, host.id);
    await state.pool.query(`UPDATE tasks SET status = 'running' WHERE id = $1`, [task.id]);
    const { status } = await req('POST', `/v1/tasks/${task.id}/cancel`, agent.token);
    expect(status).toBe(200);
  });

  it('409s cancel of a completed task (terminal)', async () => {
    const { agent, host, task } = await autoTask();
    await simulateClaim(task.id, host.id);
    await state.pool.query(`UPDATE tasks SET status = 'completed' WHERE id = $1`, [task.id]);
    const { status } = await req('POST', `/v1/tasks/${task.id}/cancel`, agent.token);
    expect(status).toBe(409);
  });

  it('cancels an awaiting_approval task (manual gate escape hatch)', async () => {
    const agent = await seedAgent('deployer', { deploy: true, approve_deployments: true });
    const host = await seedHost('h1');
    const project = await seedProject('demo-app');
    const { body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: project, host_id: host.id, version: '9.9.9', mode: 'manual',
    });
    const { status } = await req('POST', `/v1/tasks/${body.task.id}/cancel`, agent.token);
    expect(status).toBe(200);
    const t = await state.pool.query(`SELECT status FROM tasks WHERE id = $1`, [body.task.id]);
    expect(t.rows[0].status).toBe('cancelled');
  });
});
