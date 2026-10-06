// W3 Part 4 — ACL non-regression: the cases projectAcls.test.ts does not
// cover at HTTP level.
//
//   * Agent B deploying/targeting a strict-mode host without host permission
//     (POST /v1/tasks with host_id, POST /v1/deployments with host_id) —
//     403 via the central authorizeHostAccess helper; legacy-open hosts
//     stay accessible (backwards compatibility).
//   * Agent B writing artifact CONTENT to agent A's artifact
//     (PUT /v1/artifacts/:id/content) — 403 via authorizeArtifactAccess.
//     This was a demonstrated IDOR before the W3 fix (the handler only
//     required the global `deploy` permission); the fix is one central-
//     helper call in routes/artifacts.ts.
//
// The other four "Agent A cannot..." cases (GET /projects/B, operating
// Deployment B, reading Project B secrets, manipulating Project B
// artifacts/domains metadata) are covered by test/projectAcls.test.ts.
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

async function putBytes(path: string, token: string, bytes: Buffer) {
  const res = await fetch(`${base}${path}`, {
    method: 'PUT',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/octet-stream',
      'Content-Length': String(bytes.length),
    },
    body: bytes as any,
    duplex: 'half',
  } as any);
  let json: any = null;
  try {
    json = await res.json();
  } catch {
    /* empty */
  }
  return { status: res.status, body: json };
}

beforeAll(async () => {
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  process.env.DATA_ENCRYPTION_KEY = 'cd'.repeat(32);
  process.env.ARTIFACT_DIR = mkdtempSync(join(tmpdir(), 'uaht-w3-artifacts-'));
  setupDb();
  const app = createApp();
  server = http.createServer(app);
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  delete process.env.RATE_LIMIT_UNAUTH_PER_MIN;
  delete process.env.DATA_ENCRYPTION_KEY;
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

beforeEach(wipe);

describe('Part 4: host targeting respects agent_host_access (strict mode)', () => {
  it('denies an agent without host permission; allows the granted agent; keeps legacy-open hosts open', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('intruder-b');
    const strictHost = await seedHost('strict-h1');
    const openHost = await seedHost('open-h2');
    // Strict mode: only A may target strict-h1.
    await state.pool.query('INSERT INTO agent_host_access (agent_id, host_id) VALUES ($1,$2)', [
      a.id,
      strictHost.id,
    ]);

    // B has full global permissions but no host grant: explicit targeting 403s.
    expect(
      (await req('POST', '/v1/tasks', b.token, { type: 'system-info', host_id: strictHost.id }))
        .status,
    ).toBe(403);
    const bProj = await req('POST', '/v1/projects', b.token, { name: 'proj-b' });
    expect(bProj.status).toBe(201);
    expect(
      (
        await req('POST', '/v1/deployments', b.token, {
          project_id: bProj.body.project.id,
          version: '1.0.0',
          host_id: strictHost.id,
        })
      ).status,
    ).toBe(403);

    // A holds the grant: targeting works (task + deployment).
    expect(
      (await req('POST', '/v1/tasks', a.token, { type: 'system-info', host_id: strictHost.id }))
        .status,
    ).toBe(201);
    const aProj = await req('POST', '/v1/projects', a.token, { name: 'proj-a' });
    expect(aProj.status).toBe(201);
    expect(
      (
        await req('POST', '/v1/deployments', a.token, {
          project_id: aProj.body.project.id,
          version: '1.0.0',
          host_id: strictHost.id,
        })
      ).status,
    ).toBe(201);

    // Legacy-open host (no agent_host_access rows at all): everyone may target.
    expect(
      (await req('POST', '/v1/tasks', b.token, { type: 'system-info', host_id: openHost.id }))
        .status,
    ).toBe(201);
  });

  it('still 404s when the targeted host does not exist', async () => {
    const b = await seedAgent('intruder-b');
    expect(
      (await req('POST', '/v1/tasks', b.token, { type: 'system-info', host_id: randomUUID() }))
        .status,
    ).toBe(404);
  });
});

describe('Part 4: artifact content upload is project-scoped (IDOR fix)', () => {
  it('denies cross-project content upload; the owner path still works', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('intruder-b');
    const proj = await req('POST', '/v1/projects', a.token, { name: 'proj-a' });
    expect(proj.status).toBe(201);
    const projectId = proj.body.project.id as string;

    const bytes = Buffer.from('w3-artifact-bytes');
    const checksum = 'sha256:' + createHash('sha256').update(bytes).digest('hex');
    const init = await req('POST', '/v1/artifacts/init', a.token, {
      project_id: projectId,
      filename: 'app.zip',
      size: bytes.length,
      checksum,
      version: '1.0.0',
    });
    expect(init.status).toBe(201);
    const artifactId = init.body.artifact.id as string;

    // B holds the same global permissions but no project access: 403.
    expect((await putBytes(`/v1/artifacts/${artifactId}/content`, b.token, bytes)).status).toBe(403);

    // B's denied attempt left the artifact untouched.
    const after = await state.pool.query('SELECT status FROM artifacts WHERE id = $1', [artifactId]);
    expect(after.rows[0].status).toBe('pending');

    // Read side was already gated; re-assert it here for completeness.
    expect((await req('GET', `/v1/artifacts/${artifactId}`, b.token)).status).toBe(403);
    expect((await req('GET', `/v1/artifacts/${artifactId}/download`, b.token)).status).toBe(403);

    // The owner upload path still works end to end.
    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, a.token, bytes);
    expect(up.status).toBe(200);
    expect(up.body.artifact.status).toBe('ready');
  });
});
