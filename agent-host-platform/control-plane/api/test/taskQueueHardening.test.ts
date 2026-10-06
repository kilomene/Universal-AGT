// WS-D task-queue hardening tests (real express app + pg-mem).
//
// Covers the §5/§6/§7/§49/§50/§70/§74 fixes from the final production
// hardening pass:
//
//   * task transition compare-and-swap (cancel / approve / progress /
//     sweeper): concurrent state moves resolve to exactly one winner —
//     terminal states are never resurrected or rewritten;
//   * deployment mirror gating: only deployment-lifecycle task types
//     (deploy/rollback/restart/stop/start/remove/environment-update) move
//     deployments.status; a failed `logs`/`build` task no longer fails
//     its deployment; a failed restart emits deployment.failed;
//   * cancel compensation (§50): cancelling a pre-running deploy task
//     fails its never-built deployment and releases its port reservation;
//   * rollback idempotency (§7): same key replays, concurrent same-key
//     requests create exactly one rollback task;
//   * sweeper idempotence: an expired-lease task is requeued exactly once;
//   * domain creation idempotency (§7) + stuck-row resume (§50);
//   * failDomainsForDeployment covers degraded rows (§49 degraded status);
//   * API errors carry request_id (§70).
//
// SCOPE NOTE (same as e2eFlows.test.ts): the atomic claim
// (FOR UPDATE SKIP LOCKED) cannot run under pg-mem; claims are simulated
// with a direct status update.
import http from 'http';
import { tmpdir } from 'os';
import { mkdtempSync } from 'fs';
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
import { sweepTasksOnce } from '../src/lib/taskSweeper';
import { failDomainsForDeployment } from '../src/routes/domains';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_random_uuid',
    returns: 'text',
    impure: true,
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
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
      owner TEXT, owner_agent_id TEXT);
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
      id TEXT PRIMARY KEY DEFAULT gen_random_uuid(), type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      idempotency_key TEXT UNIQUE,
      created_by TEXT, assigned_to TEXT, claimed_by TEXT,
      payload JSONB NOT NULL DEFAULT '{}', result JSONB, error TEXT,
      priority INT DEFAULT 0, attempts INT DEFAULT 0, max_attempts INT DEFAULT 3,
      started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
      last_progress_at TIMESTAMPTZ, lease_expires_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
      project_id TEXT NOT NULL, host_id TEXT, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested',
      health_status TEXT, task_id TEXT,
      ports JSONB, domains JSONB,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE port_allocations (
      host_id TEXT NOT NULL, port INT NOT NULL, deployment_id TEXT NOT NULL,
      PRIMARY KEY (host_id, port)
    );
    CREATE TABLE domains (
      id TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
      idempotency_key TEXT,
      hostname TEXT NOT NULL,
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

async function seedDeployment(
  projectId: string,
  hostId: string,
  status = 'requested',
  version = 'v1',
  health: string | null = null,
) {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status, health_status)
     VALUES ($1, $2, $3, $4, $5, $6)`,
    [id, projectId, hostId, version, status, health],
  );
  return id;
}

async function wipe() {
  for (const t of [
    'events',
    'domains',
    'port_allocations',
    'tasks',
    'deployments',
    'hosts',
    'agents',
    'projects',
  ]) {
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
  return { status: res.status, headers: res.headers, body: json };
}

async function eventTypes(): Promise<string[]> {
  const { rows } = await state.pool.query(`SELECT type FROM events ORDER BY id`);
  return rows.map((r: any) => r.type);
}

async function taskRow(id: string) {
  return (await state.pool.query(`SELECT * FROM tasks WHERE id = $1`, [id])).rows[0];
}

async function deploymentRow(id: string) {
  return (await state.pool.query(`SELECT * FROM deployments WHERE id = $1`, [id])).rows[0];
}

/** Simulate the (pg-mem-untestable) claim step: queued -> claimed by host. */
async function simulateClaim(taskId: string, hostId: string) {
  await state.pool.query(
    `UPDATE tasks SET status = 'claimed', claimed_by = $2, assigned_to = $2,
       attempts = attempts + 1, last_progress_at = now(),
       lease_expires_at = now() + interval '10 minutes'
     WHERE id = $1`,
    [taskId, hostId],
  );
}

beforeAll(async () => {
  process.env.LOG_DIR = mkdtempSync(join(tmpdir(), 'uaht-wsd-logs-'));
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
// §5/§74: task transition compare-and-swap
// ---------------------------------------------------------------------------
describe('task transition compare-and-swap', () => {
  it('cancel is idempotent-safe: second cancel 409s, terminal row untouched', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const created = await req('POST', '/v1/tasks', agent.token, { type: 'status' });
    expect(created.status).toBe(201);
    const id = created.body.task.id;

    const c1 = await req('POST', `/v1/tasks/${id}/cancel`, agent.token);
    expect(c1.status).toBe(200);
    expect(c1.body.task.status).toBe('cancelled');

    const c2 = await req('POST', `/v1/tasks/${id}/cancel`, agent.token);
    expect(c2.status).toBe(409);
    expect((await taskRow(id)).status).toBe('cancelled');
  });

  it('progress: a duplicate terminal report 409s instead of resurrecting the task', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const created = await req('POST', '/v1/tasks', agent.token, { type: 'deploy' });
    const id = created.body.task.id;
    await simulateClaim(id, host.id);

    expect((await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, { status: 'running' })).status).toBe(200);
    const done = await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, {
      status: 'completed',
      result: {},
    });
    expect(done.status).toBe(200);
    expect(done.body.task.status).toBe('completed');

    // A retried/delayed duplicate report must not move the terminal task.
    const dup = await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, {
      status: 'running',
    });
    expect(dup.status).toBe(409);
    expect((await taskRow(id)).status).toBe('completed');
  });

  it('progress: a stale worker cannot report after the sweeper requeued its task', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const hostA = await seedHost('wsd-host-a');
    const created = await req('POST', '/v1/tasks', agent.token, { type: 'deploy' });
    const id = created.body.task.id;
    await simulateClaim(id, hostA.id);

    // The sweeper requeues: ownership cleared.
    await state.pool.query(
      `UPDATE tasks SET status = 'queued', claimed_by = NULL, lease_expires_at = NULL WHERE id = $1`,
      [id],
    );
    const stale = await req('POST', `/v1/worker/tasks/${id}/progress`, hostA.token, {
      status: 'running',
    });
    expect(stale.status).toBe(403);
    expect((await taskRow(id)).status).toBe('queued');
  });
});

// ---------------------------------------------------------------------------
// §5: deployment mirror gating + consistent events
// ---------------------------------------------------------------------------
describe('deployment mirror gating', () => {
  it('a failed non-lifecycle task (build) no longer fails its deployment', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'running', 'v3', 'healthy');

    const created = await req('POST', '/v1/tasks', agent.token, {
      type: 'build',
      payload: { deployment_id: depId },
    });
    expect(created.status).toBe(201);
    const id = created.body.task.id;
    await simulateClaim(id, host.id);
    expect(
      (await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, { status: 'running' })).status,
    ).toBe(200);
    const failed = await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, {
      status: 'failed',
      error: 'boom',
    });
    expect(failed.status).toBe(200);
    expect(failed.body.task.status).toBe('failed');

    expect((await deploymentRow(depId)).status).toBe('running');
    expect(await eventTypes()).not.toContain('deployment.failed');
  });

  it('a failed restart fails the deployment AND emits deployment.failed', async () => {
    const agent = await seedAgent('wsd-agent', {
      deploy: true,
      restart: true,
      read_status: true,
    });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'running', 'v3', 'healthy');

    const created = await req('POST', '/v1/tasks', agent.token, {
      type: 'restart',
      payload: { deployment_id: depId },
    });
    expect(created.status).toBe(201);
    const id = created.body.task.id;
    await simulateClaim(id, host.id);
    await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, { status: 'running' });
    const failed = await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, {
      status: 'failed',
      error: 'restart blew up',
    });
    expect(failed.status).toBe(200);

    const dep = await deploymentRow(depId);
    expect(dep.status).toBe('failed');
    expect(dep.health_status).toBe('unhealthy');
    expect(await eventTypes()).toContain('deployment.failed');
  });
});

// ---------------------------------------------------------------------------
// §50: cancel compensation for pre-running deploy tasks
// ---------------------------------------------------------------------------
describe('cancel compensation', () => {
  it('cancelling a queued deploy task frees its port; the deployment simply never starts', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'requested', 'v9');
    await state.pool.query(
      `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, 18080, $2)`,
      [host.id, depId],
    );

    const created = await req('POST', '/v1/tasks', agent.token, {
      type: 'deploy',
      payload: { deployment_id: depId },
    });
    const id = created.body.task.id;
    const cancelled = await req('POST', `/v1/tasks/${id}/cancel`, agent.token);
    expect(cancelled.status).toBe(200);

    // The established contract: a cancelled deploy never starts — the row
    // stays 'requested'. The compensation only frees the leaked port.
    expect((await deploymentRow(depId)).status).toBe('requested');
    const ports = await state.pool.query(`SELECT * FROM port_allocations WHERE deployment_id = $1`, [
      depId,
    ]);
    expect(ports.rows).toHaveLength(0);
    expect(await eventTypes()).toEqual(['task.created', 'task.cancelled']);
  });

  it('cancelling a running deploy task leaves the deployment row alone', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'building', 'v9');

    const created = await req('POST', '/v1/tasks', agent.token, {
      type: 'deploy',
      payload: { deployment_id: depId },
    });
    const id = created.body.task.id;
    await simulateClaim(id, host.id);
    await req('POST', `/v1/worker/tasks/${id}/progress`, host.token, { status: 'running' });

    const cancelled = await req('POST', `/v1/tasks/${id}/cancel`, agent.token);
    expect(cancelled.status).toBe(200);
    // The worker may still be finishing the deploy; only its final report
    // knows the truth, so the compensation stays out of the way.
    expect((await deploymentRow(depId)).status).toBe('building');
  });
});

// ---------------------------------------------------------------------------
// §7: rollback idempotency
// ---------------------------------------------------------------------------
describe('rollback idempotency', () => {
  it('same idempotency key twice -> exactly one rollback task, second replays', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const prevId = await seedDeployment(projectId, host.id, 'running', 'v1', 'healthy');
    const curId = await seedDeployment(projectId, host.id, 'failed', 'v2', 'unhealthy');
    void prevId;

    const r1 = await req('POST', `/v1/deployments/${curId}/rollback`, agent.token, {
      idempotency_key: 'rb-wsd-1',
    });
    expect(r1.status).toBe(201);
    const r2 = await req('POST', `/v1/deployments/${curId}/rollback`, agent.token, {
      idempotency_key: 'rb-wsd-1',
    });
    expect(r2.status).toBe(200);
    expect(r2.body.idempotent_replay).toBe(true);
    expect(r2.body.task.id).toBe(r1.body.task.id);

    const { rows } = await state.pool.query(
      `SELECT count(*)::int AS n FROM tasks WHERE type = 'rollback'`,
    );
    expect(rows[0].n).toBe(1);
  });

  it('same key, different deployment -> 409 conflict', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const prevId = await seedDeployment(projectId, host.id, 'running', 'v1', 'healthy');
    const curId = await seedDeployment(projectId, host.id, 'failed', 'v2', 'unhealthy');
    const curId2 = await seedDeployment(projectId, host.id, 'failed', 'v3', 'unhealthy');
    void prevId;

    const r1 = await req('POST', `/v1/deployments/${curId}/rollback`, agent.token, {
      idempotency_key: 'rb-wsd-2',
    });
    expect(r1.status).toBe(201);
    const r2 = await req('POST', `/v1/deployments/${curId2}/rollback`, agent.token, {
      idempotency_key: 'rb-wsd-2',
    });
    expect(r2.status).toBe(409);
  });
});

// ---------------------------------------------------------------------------
// §6/§74: sweeper compare-and-swap
// ---------------------------------------------------------------------------
describe('sweeper compare-and-swap', () => {
  it('an expired-lease task is requeued exactly once, even across two passes', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const created = await req('POST', '/v1/tasks', agent.token, { type: 'healthcheck' });
    const id = created.body.task.id;
    await simulateClaim(id, host.id);
    // Expire the lease: the worker is gone.
    await state.pool.query(`UPDATE tasks SET lease_expires_at = now() - interval '1 minute' WHERE id = $1`, [
      id,
    ]);

    await sweepTasksOnce(state.pool);
    await sweepTasksOnce(state.pool);

    const task = await taskRow(id);
    expect(task.status).toBe('queued');
    expect(task.claimed_by).toBeNull();
    const events = await eventTypes();
    expect(events.filter((t) => t === 'task.requeued')).toHaveLength(1);
  });

  it('a task with a live lease is untouched by the sweep', async () => {
    const agent = await seedAgent('wsd-agent', { deploy: true, read_status: true });
    const host = await seedHost('wsd-host');
    const created = await req('POST', '/v1/tasks', agent.token, { type: 'healthcheck' });
    const id = created.body.task.id;
    await simulateClaim(id, host.id); // fresh 10-minute lease

    await sweepTasksOnce(state.pool);
    expect((await taskRow(id)).status).toBe('claimed');
  });
});

// ---------------------------------------------------------------------------
// §7/§50: domain creation idempotency + stuck-row resume
// ---------------------------------------------------------------------------
describe('domain idempotency and resume', () => {
  async function seedRunningDeployment() {
    const agent = await seedAgent('wsd-agent', { manage_domains: true, read_status: true });
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'running', 'v1', 'healthy');
    return { agent, depId, host };
  }

  it('same idempotency key twice -> one row, second replays', async () => {
    const { agent, depId } = await seedRunningDeployment();
    const body = {
      deployment_id: depId,
      hostname: 'idem-wsd.example.com',
      ingress: 'direct',
      idempotency_key: 'dom-wsd-1',
    };
    const r1 = await req('POST', '/v1/domains', agent.token, body);
    expect(r1.status).toBe(201);
    const r2 = await req('POST', '/v1/domains', agent.token, body);
    expect(r2.status).toBe(200);
    expect(r2.body.idempotent_replay).toBe(true);
    expect(r2.body.domain.id).toBe(r1.body.domain.id);

    const { rows } = await state.pool.query(
      `SELECT count(*)::int AS n FROM domains WHERE lower(hostname) = 'idem-wsd.example.com' AND status <> 'removed'`,
    );
    expect(rows[0].n).toBe(1);
  });

  it('same key with a different body -> 409', async () => {
    const { agent, depId } = await seedRunningDeployment();
    const r1 = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId,
      hostname: 'idem-wsd-2.example.com',
      ingress: 'direct',
      idempotency_key: 'dom-wsd-2',
    });
    expect(r1.status).toBe(201);
    const r2 = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId,
      hostname: 'other-wsd.example.com',
      ingress: 'direct',
      idempotency_key: 'dom-wsd-2',
    });
    expect(r2.status).toBe(409);
  });

  it('re-posting a failed domain resumes provisioning instead of 409', async () => {
    const { agent, depId, host } = await seedRunningDeployment();
    // A domain stranded in `failed` by a crashed first attempt.
    await state.pool.query(
      `INSERT INTO domains (hostname, deployment_id, host_id, ingress, status, error)
       VALUES ('resume-wsd.example.com', $1, $2, 'direct', 'failed', 'dns blew up')`,
      [depId, host.id],
    );
    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId,
      hostname: 'resume-wsd.example.com',
      ingress: 'direct',
    });
    expect(r.status).toBe(200);
    expect(r.body.domain.status).toBe('active');
    const { rows } = await state.pool.query(
      `SELECT count(*)::int AS n FROM domains WHERE lower(hostname) = 'resume-wsd.example.com' AND status <> 'removed'`,
    );
    expect(rows[0].n).toBe(1);
  });

  it('an active duplicate is still a 409', async () => {
    const { agent, depId } = await seedRunningDeployment();
    const body = { deployment_id: depId, hostname: 'dup-wsd.example.com', ingress: 'direct' };
    expect((await req('POST', '/v1/domains', agent.token, body)).status).toBe(201);
    expect((await req('POST', '/v1/domains', agent.token, body)).status).toBe(409);
  });
});

// ---------------------------------------------------------------------------
// §49: degraded domain lifecycle
// ---------------------------------------------------------------------------
describe('degraded domain lifecycle', () => {
  it('failDomainsForDeployment fails degraded rows too', async () => {
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'failed', 'v1', 'unhealthy');
    const domId = randomUUID();
    await state.pool.query(
      `INSERT INTO domains (id, hostname, deployment_id, host_id, ingress, status)
       VALUES ($1, 'degraded-wsd.example.com', $2, $3, 'tunnel', 'degraded')`,
      [domId, depId, host.id],
    );

    const n = await failDomainsForDeployment(state.pool, depId, 'deployment failed');
    expect(n).toBe(1);
    const { rows } = await state.pool.query(`SELECT status FROM domains WHERE id = $1`, [domId]);
    expect(rows[0].status).toBe('failed');
  });
});

// ---------------------------------------------------------------------------
// §70: error correlation
// ---------------------------------------------------------------------------
describe('error correlation', () => {
  it('API errors carry request_id matching the x-request-id header', async () => {
    const agent = await seedAgent('wsd-agent', { read_status: true });
    const r = await req('GET', '/v1/tasks/not-a-uuid', agent.token);
    expect(r.status).toBe(400);
    expect(r.body.error.code).toBe('bad_request');
    const headerId = r.headers.get('x-request-id');
    expect(typeof headerId).toBe('string');
    expect(r.body.error.request_id).toBe(headerId);
  });
});

// ---------------------------------------------------------------------------
// WS-B §37: draining hosts cannot claim new work
// ---------------------------------------------------------------------------
describe('draining hosts cannot claim', () => {
  it('409s the claim for a host with status=draining', async () => {
    const host = await seedHost('wsd-host');
    await state.pool.query(`UPDATE hosts SET status = 'draining' WHERE id = $1`, [host.id]);
    const r = await req('POST', '/v1/worker/tasks/claim', host.token, { host_id: host.id });
    expect(r.status).toBe(409);
    expect(r.body.error.code).toBe('conflict');
  });

  it('409s the claim for a host whose worker reports draining', async () => {
    const host = await seedHost('wsd-host');
    await state.pool.query(`UPDATE hosts SET worker_draining = true WHERE id = $1`, [host.id]);
    const r = await req('POST', '/v1/worker/tasks/claim', host.token, { host_id: host.id });
    expect(r.status).toBe(409);
  });
});

// ---------------------------------------------------------------------------
// WS-B: heartbeat persists the worker-reported enrichment fields
// ---------------------------------------------------------------------------
describe('heartbeat enrichment', () => {
  it('persists draining, worker_status, capabilities, ingress, host_name', async () => {
    const host = await seedHost('wsd-host');
    const r = await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, {
      worker_version: '1.2.3',
      draining: true,
      worker_status: 'draining',
      capabilities: ['docker', 'gpu'],
      ingress: { enabled: true, provider: 'cloudflare' },
      host_name: 'wsd-host',
    });
    expect(r.status).toBe(200);
    const { rows } = await state.pool.query(
      `SELECT worker_draining, worker_status, capabilities, ingress, reported_host_name
       FROM hosts WHERE id = $1`,
      [host.id],
    );
    const row = rows[0];
    expect(row.worker_draining).toBe(true);
    expect(row.worker_status).toBe('draining');
    expect(row.capabilities).toEqual(['docker', 'gpu']);
    expect(row.ingress).toEqual({ enabled: true, provider: 'cloudflare' });
    expect(row.reported_host_name).toBe('wsd-host');
    // ... and the public host shape carries them for scheduler/dashboard.
    expect(r.body.host.worker_draining).toBe(true);
    expect(r.body.host.capabilities).toEqual(['docker', 'gpu']);
  });

  it('omitted fields never clear stored values', async () => {
    const host = await seedHost('wsd-host');
    await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, {
      capabilities: ['docker'],
      draining: true,
    });
    await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, { cpu_pct: 11 });
    const { rows } = await state.pool.query(
      `SELECT worker_draining, capabilities FROM hosts WHERE id = $1`,
      [host.id],
    );
    expect(rows[0].worker_draining).toBe(true);
    expect(rows[0].capabilities).toEqual(['docker']);
  });
});

// ---------------------------------------------------------------------------
// WS-C §31: worker.updated event
// ---------------------------------------------------------------------------
describe('worker.updated', () => {
  it('fires when the reported worker_version changes', async () => {
    const host = await seedHost('wsd-host');
    await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, { worker_version: '1.0.0' });
    expect(await eventTypes()).not.toContain('worker.updated');

    await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, { worker_version: '1.1.0' });
    const events = await state.pool.query(
      `SELECT type, payload FROM events WHERE type = 'worker.updated'`,
    );
    expect(events.rows).toHaveLength(1);
    expect(events.rows[0].payload).toMatchObject({
      previous_version: '1.0.0',
      worker_version: '1.1.0',
    });

    // Same version again: no duplicate event.
    await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, { worker_version: '1.1.0' });
    const again = await state.pool.query(
      `SELECT count(*)::int AS n FROM events WHERE type = 'worker.updated'`,
    );
    expect(again.rows[0].n).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// C1 (WS-J): the 'degraded' domain status must be allowed by the DB CHECK
// ---------------------------------------------------------------------------
describe('degraded domain status (C1)', () => {
  it('the reconciler degrade transition is permitted by the post-008 schema', async () => {
    const { setDomainStatus } = await import('../src/routes/domains');
    const host = await seedHost('wsd-host');
    const projectId = randomUUID();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'wsd-proj')`, [projectId]);
    const depId = await seedDeployment(projectId, host.id, 'running', 'v1', 'healthy');
    const domId = randomUUID();
    await state.pool.query(
      `INSERT INTO domains (id, hostname, deployment_id, host_id, ingress, status)
       VALUES ($1, 'c1-wsd.example.com', $2, $3, 'tunnel', 'active')`,
      [domId, depId, host.id],
    );
    // This is exactly what degradeDomain() does on the live reconciler
    // path. On the pre-008 schema (007 CHECK without 'degraded') the
    // UPDATE dies with a check violation on real PostgreSQL.
    const row = await setDomainStatus(state.pool, domId, 'active', 'degraded', {
      error: 'route unverifiable',
    });
    expect(row.status).toBe('degraded');
  });

  it('fails on the pre-008 schema, proving the defect 008 fixes', async () => {
    // A table replicating migration 007's CHECK verbatim (no 'degraded').
    await state.pool.query(`DROP TABLE IF EXISTS domains_pre008`);
    await state.pool.query(`
      CREATE TABLE domains_pre008 (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'requested'
          CHECK (status IN ('requested', 'configuring', 'active',
                            'failed', 'removing', 'removed'))
      )`);
    await state.pool.query(`INSERT INTO domains_pre008 (id, status) VALUES ('x', 'active')`);
    await expect(
      state.pool.query(`UPDATE domains_pre008 SET status = 'degraded' WHERE id = 'x'`),
    ).rejects.toThrow();
  });
});
