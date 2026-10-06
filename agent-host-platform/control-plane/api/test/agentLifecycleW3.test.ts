// W3 Part 5 — agent lifecycle end-to-end verification.
//
// active -> works; suspended -> authentication denied (403, immediately);
// resumed -> works again; revoked -> credential permanently invalid (401,
// immediately and forever).
//
// The revoked/suspended credential is exercised against every sensitive
// surface: deploying, uploading artifact bytes, creating tasks, reading
// project secrets, reading protected resources, and reading the event
// journal. Repeated sequential requests after the transition prove there
// is no credential cache (in-memory token cache with TTL, memoized auth
// result) that would let a dead credential keep working — attachAuth
// re-reads the agents row from the database on EVERY request.
//
// Boots the REAL express app (createApp) against pg-mem.
import http from 'http';
import { createHash, randomUUID } from 'crypto';
import { tmpdir } from 'os';
import { mkdtempSync } from 'fs';
import { join } from 'path';
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
      type TEXT, capabilities JSONB, permissions JSONB,
      api_key_hash TEXT, idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ, metadata JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      token_hash TEXT, previous_token_hash TEXT,
      previous_token_expires_at TIMESTAMPTZ,
      status TEXT NOT NULL DEFAULT 'online',
      capabilities JSONB NOT NULL DEFAULT '[]',
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
      priority INTEGER NOT NULL DEFAULT 0,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY, project_id TEXT, filename TEXT,
      storage_path TEXT, checksum TEXT, size BIGINT,
      version TEXT, status TEXT NOT NULL DEFAULT 'pending',
      manifest JSONB
    );
    CREATE TABLE secrets (
      id TEXT PRIMARY KEY DEFAULT test_gen_uuid(), project_id TEXT NOT NULL,
      name TEXT NOT NULL, encrypted_value BYTEA NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
      UNIQUE (project_id, name)
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

const FULL = {
  deploy: true,
  read_status: true,
  restart: true,
  stop: true,
  manage_secrets: true,
  manage_domains: true,
  approve_deployments: true,
  admin: true,
};

async function seedAgent(name: string, permissions: Record<string, boolean> = FULL) {
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
  for (const t of [
    'events', 'secrets', 'tasks', 'deployments', 'artifacts',
    'agent_host_access', 'project_members', 'domains', 'hosts', 'agents', 'projects',
  ]) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
}

async function req(method: string, path: string, token: string | undefined, body?: unknown) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${base}${path}`, {
    method,
    headers,
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

async function putBytes(path: string, token: string | undefined, bytes: Buffer) {
  const headers: Record<string, string> = {
    'Content-Type': 'application/octet-stream',
    'Content-Length': String(bytes.length),
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${base}${path}`, {
    method: 'PUT',
    headers,
    body: bytes as any,
    duplex: 'half',
  } as any);
  return { status: res.status };
}

const PROV = 'zz9-w3-lifecycle-provisioning';

beforeAll(async () => {
  process.env.UAHT_PROVISIONING_TOKEN = PROV;
  process.env.DATA_ENCRYPTION_KEY = 'cd'.repeat(32);
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  process.env.ARTIFACT_DIR = mkdtempSync(join(tmpdir(), 'uaht-w3-lifecycle-artifacts-'));
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
  delete process.env.DATA_ENCRYPTION_KEY;
  delete process.env.RATE_LIMIT_UNAUTH_PER_MIN;
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

beforeEach(wipe);

// One agent owns a project with a secret and a pending artifact; every
// lifecycle state is then probed across the sensitive surfaces.
async function seedWorld() {
  const agent = await seedAgent('lifecycle-agent');
  const { hash } = generateHostToken();
  const hostId = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, capabilities, total_cpu, total_ram_mb)
     VALUES ($1,'lc-host',$2,'online','["docker"]',4,8192)`,
    [hostId, hash],
  );
  const proj = await req('POST', '/v1/projects', agent.token, { name: 'lifecycle-proj' });
  expect(proj.status).toBe(201);
  const projectId = proj.body.project.id as string;
  const sec = await req('POST', `/v1/projects/${projectId}/secrets`, agent.token, {
    name: 'K',
    value: 'placeholder-not-a-real-secret',
  });
  expect(sec.status).toBe(201);
  const bytes = Buffer.from('lifecycle-artifact-bytes');
  const init = await req('POST', '/v1/artifacts/init', agent.token, {
    project_id: projectId,
    filename: 'a.zip',
    size: bytes.length,
    checksum: 'sha256:' + createHash('sha256').update(bytes).digest('hex'),
    version: '1.0.0',
  });
  expect(init.status).toBe(201);
  const artifactId = init.body.artifact.id as string;
  return { agent, projectId, artifactId, bytes, hostId };
}

// Probes the sensitive surfaces; returns the status codes observed.
async function probeSurfaces(token: string | undefined, world: { projectId: string; artifactId: string; bytes: Buffer; hostId: string }) {
  const task = await req('POST', '/v1/tasks', token, { type: 'system-info' });
  const deploy = await req('POST', '/v1/deployments', token, {
    project_id: world.projectId,
    version: '9.9.9',
    host_id: world.hostId,
  });
  const upload = await putBytes(`/v1/artifacts/${world.artifactId}/content`, token, world.bytes);
  const secrets = await req('GET', `/v1/projects/${world.projectId}/secrets`, token);
  const project = await req('GET', `/v1/projects/${world.projectId}`, token);
  const events = await req('GET', '/v1/events?limit=5', token);
  return {
    task: task.status,
    deploy: deploy.status,
    upload: upload.status,
    secrets: secrets.status,
    project: project.status,
    events: events.status,
  };
}

describe('Part 5: agent lifecycle end-to-end', () => {
  it('active -> works; suspended -> denied; resumed -> works; revoked -> dead forever', async () => {
    const world = await seedWorld();
    const { agent, artifactId } = world;

    // ACTIVE: everything works.
    const active = await probeSurfaces(agent.token, world);
    expect(active.task).toBe(201);
    expect(active.deploy).toBe(201);
    expect(active.upload).toBe(200);
    expect(active.secrets).toBe(200);
    expect(active.project).toBe(200);
    expect(active.events).toBe(200);

    // Re-init a fresh pending artifact for the later upload probes (the
    // first one is 'ready' now).
    const bytes2 = Buffer.from('second-artifact-bytes');
    const init2 = await req('POST', '/v1/artifacts/init', agent.token, {
      project_id: world.projectId,
      filename: 'b.zip',
      size: bytes2.length,
      checksum: 'sha256:' + createHash('sha256').update(bytes2).digest('hex'),
      version: '2.0.0',
    });
    expect(init2.status).toBe(201);
    const world2 = { ...world, artifactId: init2.body.artifact.id as string, bytes: bytes2 };

    // SUSPEND: authentication denied immediately (403), on every surface.
    expect(
      (await req('POST', `/v1/agents/${agent.id}/suspend`, undefined, undefined)).status,
    ).toBe(401); // sanity: operator auth still required
    const suspend = await fetch(`${base}/v1/agents/${agent.id}/suspend`, {
      method: 'POST',
      headers: { 'X-Provisioning-Token': PROV, 'Content-Type': 'application/json' },
    });
    expect(suspend.status).toBe(200);
    const suspended = await probeSurfaces(agent.token, world2);
    for (const [surface, status] of Object.entries(suspended)) {
      expect(status, `suspended agent must be denied on ${surface}`).toBe(403);
    }

    // RESUME: works again immediately (no stale denial — no auth cache).
    const resume = await fetch(`${base}/v1/agents/${agent.id}/resume`, {
      method: 'POST',
      headers: { 'X-Provisioning-Token': PROV, 'Content-Type': 'application/json' },
    });
    expect(resume.status).toBe(200);
    const resumed = await probeSurfaces(agent.token, world2);
    expect(resumed.task).toBe(201);
    expect(resumed.events).toBe(200);
    expect(resumed.project).toBe(200);

    // REVOKE: credential permanently invalid — 401 on every surface,
    // repeatedly (no TTL window, no memoized auth result).
    const revoke = await fetch(`${base}/v1/agents/${agent.id}/revoke`, {
      method: 'POST',
      headers: { 'X-Provisioning-Token': PROV, 'Content-Type': 'application/json' },
    });
    expect(revoke.status).toBe(200);
    for (let i = 0; i < 3; i++) {
      const dead = await probeSurfaces(agent.token, world2);
      for (const [surface, status] of Object.entries(dead)) {
        expect(status, `revoked agent attempt ${i} must 401 on ${surface}`).toBe(401);
      }
    }
    // The stored hash was replaced with an unmatchable random value.
    const { rows } = await state.pool.query('SELECT api_key_hash, status FROM agents WHERE id = $1', [
      agent.id,
    ]);
    expect(rows[0].status).toBe('revoked');
    expect(rows[0].api_key_hash).toBeTruthy();

    // Revocation is permanent: resume is rejected.
    const resumeAfter = await fetch(`${base}/v1/agents/${agent.id}/resume`, {
      method: 'POST',
      headers: { 'X-Provisioning-Token': PROV, 'Content-Type': 'application/json' },
    });
    expect(resumeAfter.status).toBe(409);
    expect(artifactId).toBeTruthy();
  });

  it('a revoked credential cannot claim tasks, rotate keys, or act as operator', async () => {
    const world = await seedWorld();
    const { agent } = world;
    const revoke = await fetch(`${base}/v1/agents/${agent.id}/revoke`, {
      method: 'POST',
      headers: { 'X-Provisioning-Token': PROV, 'Content-Type': 'application/json' },
    });
    expect(revoke.status).toBe(200);

    // Worker task claim is host-token-only; the dead agent token 401s before kind checks.
    const claim = await req('POST', '/v1/worker/tasks/claim', agent.token, {});
    expect(claim.status).toBe(401);
    // Key rotation with the dead token: 401, not a new key.
    expect((await req('POST', '/v1/agents/me/rotate', agent.token)).status).toBe(401);
    // Operator action with the dead token: 401 (never treated as operator).
    expect((await req('POST', `/v1/agents/${agent.id}/suspend`, agent.token)).status).toBe(401);
  });
});
