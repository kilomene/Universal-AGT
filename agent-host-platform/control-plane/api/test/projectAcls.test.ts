// §10 agent project / deployment ACLs.
//
// Boots the REAL express app (createApp) against pg-mem. Agent A owns a
// project; agent B holds the same global permissions but no ownership or
// membership. Every project-scoped route must deny B (403) while allowing A,
// and granting membership must open the project to B. Legacy-open projects
// (owner_agent_id NULL) stay accessible to everyone.
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

function setupDb() {
  const db = newDb();
  // pg-mem has no gen_random_uuid(); emulate the real id default with
  // deterministic UUID-shaped values (routes validate UUID shape).
  let n = 0;
  db.public.registerFunction({
    name: 'test_gen_uuid',
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
      status TEXT NOT NULL DEFAULT 'online',
      capabilities JSONB NOT NULL DEFAULT '[]',
      token_hash TEXT NOT NULL,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION,
      created_at TIMESTAMPTZ DEFAULT now(),
      worker_draining BOOLEAN NOT NULL DEFAULT false
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY DEFAULT test_gen_uuid(), name TEXT NOT NULL,
      owner TEXT, owner_agent_id TEXT, repository TEXT,
      runtime TEXT NOT NULL DEFAULT 'docker',
      configuration JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT,
      project_id TEXT NOT NULL, host_id TEXT NOT NULL, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested', task_id TEXT,
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY DEFAULT test_gen_uuid(), type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      idempotency_key TEXT,
      created_by TEXT, assigned_to TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY, project_id TEXT, filename TEXT,
      storage_path TEXT, checksum TEXT, size BIGINT,
      version TEXT, status TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE TABLE secrets (
      id TEXT PRIMARY KEY DEFAULT 's', project_id TEXT NOT NULL,
      name TEXT NOT NULL, encrypted_value BYTEA NOT NULL
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
    CREATE TABLE domains (
      id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL,
      hostname TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server;

const FULL_PERMS = {
  deploy: true,
  read_status: true,
  restart: true,
  stop: true,
  manage_secrets: true,
  manage_domains: true,
  approve_deployments: true,
};

async function seedAgent(name: string, permissions: Record<string, boolean> = FULL_PERMS) {
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
  const { hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, capabilities, total_cpu, total_ram_mb)
     VALUES ($1,$2,$3,'online','["docker"]',4,8192)`,
    [id, name, hash],
  );
  return { id };
}

async function wipe() {
  for (const t of [
    'events', 'secrets', 'port_allocations', 'tasks', 'deployments',
    'artifacts', 'agent_host_access', 'project_members', 'hosts', 'agents', 'projects',
    'domains',
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
    /* empty */
  }
  return { status: res.status, body: json };
}

// A project owned by A, plus one deployment+task on it.
async function seedOwnedProject(ownerToken: string, ownerId: string, name: string) {
  const create = await req('POST', '/v1/projects', ownerToken, { name });
  expect(create.status).toBe(201);
  expect(create.body.project.owner_agent_id).toBe(ownerId);
  return create.body.project.id as string;
}

beforeAll(async () => {
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  process.env.DATA_ENCRYPTION_KEY = 'cd'.repeat(32);
  setupDb();
  const app = createApp();
  server = http.createServer(app);
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

beforeEach(wipe);

describe('§10 project ACLs', () => {
  it('denies a non-owner agent across every project-scoped route', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('intruder-b');
    const host = await seedHost('h1');
    const projectId = await seedOwnedProject(a.token, a.id, 'proj-a');

    // Project reads/writes.
    expect((await req('GET', `/v1/projects/${projectId}`, b.token)).status).toBe(403);
    expect((await req('PUT', `/v1/projects/${projectId}`, b.token, { repository: 'x' })).status).toBe(403);
    const listB = await req('GET', '/v1/projects', b.token);
    expect(listB.status).toBe(200);
    expect(listB.body.projects.map((p: any) => p.id)).not.toContain(projectId);
    const listA = await req('GET', '/v1/projects', a.token);
    expect(listA.body.projects.map((p: any) => p.id)).toContain(projectId);

    // Deployments.
    expect(
      (await req('POST', '/v1/deployments', b.token, { project_id: projectId, version: '1.0.0' })).status,
    ).toBe(403);

    // Artifacts.
    expect(
      (
        await req('POST', '/v1/artifacts/init', b.token, {
          project_id: projectId,
          filename: 'x.zip',
          size: 10,
          checksum: 'sha256:' + 'ab'.repeat(32),
          version: '1.0.0',
        })
      ).status,
    ).toBe(403);

    // Secrets.
    expect(
      (await req('POST', `/v1/projects/${projectId}/secrets`, b.token, { name: 'K', value: 'v' })).status,
    ).toBe(403);
    expect((await req('GET', `/v1/projects/${projectId}/secrets`, b.token)).status).toBe(403);

    // A deploys; B must not touch the deployment, its task, or its service.
    const dep = await req('POST', '/v1/deployments', a.token, {
      project_id: projectId,
      version: '1.0.0',
      host_id: host.id,
    });
    expect(dep.status).toBe(201);
    const depId = dep.body.deployment.id as string;
    const taskId = dep.body.task.id as string;
    expect((await req('GET', `/v1/deployments/${depId}`, b.token)).status).toBe(403);
    expect((await req('POST', `/v1/deployments/${depId}/rollback`, b.token)).status).toBe(403);
    expect((await req('GET', `/v1/tasks/${taskId}`, b.token)).status).toBe(403);
    expect(
      (await req('POST', '/v1/tasks', b.token, { type: 'logs', payload: { deployment_id: depId } })).status,
    ).toBe(403);
    expect((await req('POST', `/v1/services/${depId}/restart`, b.token)).status).toBe(403);
    const tasksB = await req('GET', '/v1/tasks', b.token);
    expect(tasksB.body.tasks.map((t: any) => t.id)).not.toContain(taskId);
  });

  it('grants access to project members', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('member-b');
    const host = await seedHost('h1');
    const projectId = await seedOwnedProject(a.token, a.id, 'proj-a');

    const add = await req('POST', `/v1/projects/${projectId}/members`, a.token, { agent_id: b.id });
    expect(add.status).toBe(201);

    expect((await req('GET', `/v1/projects/${projectId}`, b.token)).status).toBe(200);
    const dep = await req('POST', '/v1/deployments', b.token, {
      project_id: projectId,
      version: '2.0.0',
      host_id: host.id,
    });
    expect(dep.status).toBe(201);
    expect((await req('GET', `/v1/deployments/${dep.body.deployment.id}`, b.token)).status).toBe(200);
  });

  it('refuses membership changes from non-owners', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('intruder-b');
    const c = await seedAgent('other-c');
    const projectId = await seedOwnedProject(a.token, a.id, 'proj-a');
    const add = await req('POST', `/v1/projects/${projectId}/members`, b.token, { agent_id: c.id });
    expect(add.status).toBe(403);
  });

  it('keeps legacy ownerless projects accessible (backwards compatibility)', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('other-b');
    const legacyId = randomUUID();
    await state.pool.query(
      `INSERT INTO projects (id, name, owner, owner_agent_id) VALUES ($1,'legacy','ghost',NULL)`,
      [legacyId],
    );
    expect((await req('GET', `/v1/projects/${legacyId}`, b.token)).status).toBe(200);
    expect((await req('GET', `/v1/projects/${legacyId}`, a.token)).status).toBe(200);
  });

  it('still 404s on genuinely missing projects', async () => {
    const b = await seedAgent('other-b');
    const missing = randomUUID();
    expect((await req('GET', `/v1/projects/${missing}`, b.token)).status).toBe(404);
    expect(
      (await req('POST', '/v1/deployments', b.token, { project_id: missing, version: '1.0.0' })).status,
    ).toBe(404);
  });

  it('denies domain operations to non-owners (§10: domain -> deployment -> project)', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('intruder-b');
    const host = await seedHost('h1');
    const projectId = await seedOwnedProject(a.token, a.id, 'proj-a');
    const dep = await req('POST', '/v1/deployments', a.token, {
      project_id: projectId,
      version: '1.0.0',
      host_id: host.id,
    });
    expect(dep.status).toBe(201);
    const depId = dep.body.deployment.id as string;

    // B cannot attach or list domains on A's deployment.
    expect(
      (await req('POST', '/v1/domains', b.token, { deployment_id: depId, hostname: 'app.example.com' }))
        .status,
    ).toBe(403);
    expect((await req('GET', `/v1/domains?deployment_id=${depId}`, b.token)).status).toBe(403);

    // Seed a live domain row directly; B still cannot inspect or detach it.
    await state.pool.query(
      `INSERT INTO domains (id, deployment_id, hostname, status) VALUES ($1,$2,$3,'active')`,
      [randomUUID(), depId, 'app.example.com'],
    );
    expect((await req('GET', '/v1/domains/app.example.com', b.token)).status).toBe(403);
    expect(
      (await req('DELETE', '/v1/domains', b.token, { deployment_id: depId, hostname: 'app.example.com' }))
        .status,
    ).toBe(403);

    // A (owner) can list and inspect.
    expect((await req('GET', `/v1/domains?deployment_id=${depId}`, a.token)).status).toBe(200);
    expect((await req('GET', '/v1/domains/app.example.com', a.token)).status).toBe(200);
  });
});
