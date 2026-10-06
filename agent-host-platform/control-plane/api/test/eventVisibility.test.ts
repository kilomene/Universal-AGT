// W3 Part 3 — event visibility audit.
//
// The event journal is GLOBALLY VISIBLE BY DESIGN: any agent holding the
// `read_status` permission reads the full stream (no per-project /
// per-host / per-agent ACL filtering). This is the shared, tamper-evident
// audit trail (see docs/security.md, "Event visibility: global by design").
//
// The compensating invariant is absolute: event payloads must NEVER carry
// secret material (API keys, host tokens, secret values, provisioning
// tokens, ciphertext). These tests lock both halves of the contract:
//   1. an agent with no project access CAN read events about other
//      agents' projects (global visibility — must keep working);
//   2. a sentinel sweep over every event row asserts no known secret
//      value (or sensitive key name) ever appears in any payload.
//
// Boots the REAL express app (createApp) against pg-mem.
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
      id TEXT PRIMARY KEY DEFAULT test_gen_uuid(), name TEXT NOT NULL,
      type TEXT, capabilities JSONB, permissions JSONB,
      api_key_hash TEXT, idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ, metadata JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY DEFAULT test_gen_uuid(), name TEXT NOT NULL,
      host_type TEXT NOT NULL DEFAULT 'persistent-linux-host',
      status TEXT NOT NULL DEFAULT 'online',
      capabilities JSONB NOT NULL DEFAULT '[]',
      worker_version TEXT,
      token_hash TEXT, previous_token_hash TEXT,
      previous_token_expires_at TIMESTAMPTZ,
      idempotency_key TEXT,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION, disk_pct DOUBLE PRECISION,
      docker_status TEXT, running_apps JSONB NOT NULL DEFAULT '[]',
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION, total_disk_gb DOUBLE PRECISION,
      last_seen TIMESTAMPTZ, metadata JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
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

const READ_ONLY = { read_status: true };
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

async function wipe() {
  for (const t of [
    'events', 'secrets', 'tasks', 'deployments', 'artifacts',
    'agent_host_access', 'project_members', 'domains', 'hosts', 'agents', 'projects',
  ]) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
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

interface ReqOpts {
  token?: string;
  provisioningToken?: string;
  body?: unknown;
  raw?: boolean;
}

async function req(method: string, path: string, opts: ReqOpts = {}) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (opts.token) headers['Authorization'] = `Bearer ${opts.token}`;
  if (opts.provisioningToken) headers['X-Provisioning-Token'] = opts.provisioningToken;
  const res = await fetch(`${base}${path}`, {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  let json: any = null;
  try {
    json = await res.json();
  } catch {
    /* empty */
  }
  return { status: res.status, body: json };
}

const PROV = 'zz9-w3-event-visibility-provisioning';

beforeAll(async () => {
  process.env.UAHT_PROVISIONING_TOKEN = PROV;
  process.env.DATA_ENCRYPTION_KEY = 'cd'.repeat(32);
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
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

describe('Part 3: event visibility is global by design', () => {
  it('an agent with read_status but no project access still reads the full journal', async () => {
    const a = await seedAgent('owner-a');
    const b = await seedAgent('outsider-b', READ_ONLY);
    const host = await seedHost('h1');

    // A builds real history: project, deployment, secret.
    const proj = await req('POST', '/v1/projects', { token: a.token, body: { name: 'proj-a' } });
    expect(proj.status).toBe(201);
    const projectId = proj.body.project.id as string;
    const sec = await req('POST', `/v1/projects/${projectId}/secrets`, {
      token: a.token,
      body: { name: 'DB_PASSWORD', value: 'placeholder-not-a-real-secret' },
    });
    expect(sec.status).toBe(201);
    const dep = await req('POST', '/v1/deployments', {
      token: a.token,
      body: { project_id: projectId, version: '1.0.0', host_id: host.id },
    });
    expect(dep.status).toBe(201);

    // B cannot touch A's project directly (ACL still enforced on resources)...
    expect((await req('GET', `/v1/projects/${projectId}`, { token: b.token })).status).toBe(403);
    expect((await req('GET', `/v1/projects/${projectId}/secrets`, { token: b.token })).status).toBe(403);

    // ...but B CAN read the journal entries about it: global by design.
    const evts = await req('GET', '/v1/events?limit=100', { token: b.token });
    expect(evts.status).toBe(200);
    const types = (evts.body.events as any[]).map((e) => e.type);
    expect(types).toContain('secret.changed');
    expect(types).toContain('deployment.requested');
    expect(types).toContain('task.created');
    // The secret.changed entry is visible but carries the name only.
    const changed = (evts.body.events as any[]).find((e) => e.type === 'secret.changed');
    expect(changed.payload.name).toBe('DB_PASSWORD');
    expect(JSON.stringify(changed.payload)).not.toContain('placeholder-not-a-real-secret');

    // Type filtering works the same for the outsider.
    const filtered = await req('GET', '/v1/events?type=secret.changed&limit=10', { token: b.token });
    expect(filtered.status).toBe(200);
    expect((filtered.body.events as any[]).every((e) => e.type === 'secret.changed')).toBe(true);
  });

  it('an agent without read_status cannot read the journal at all', async () => {
    const a = await seedAgent('owner-a');
    const noperm = await seedAgent('noperm-c', {});
    const proj = await req('POST', '/v1/projects', { token: a.token, body: { name: 'proj-a' } });
    expect(proj.status).toBe(201);
    expect((await req('GET', '/v1/events', { token: noperm.token })).status).toBe(403);
    expect((await req('GET', '/v1/events')).status).toBe(401);
  });
});

describe('Part 3: no secret material ever lands in the event journal', () => {
  it('sentinel sweep: no known secret value or sensitive key appears in any event', async () => {
    const SECRET_SENTINEL = 'zz9-w3-sentinel-secret-value-4f2a';
    const WRONG_PROV = 'zz9-w3-wrong-provisioning-8b1c';

    const op = await seedAgent('operator', FULL);

    // 1. Project secret with a sentinel value.
    const proj = await req('POST', '/v1/projects', { token: op.token, body: { name: 'proj-s' } });
    expect(proj.status).toBe(201);
    const projectId = proj.body.project.id as string;
    expect(
      (
        await req('POST', `/v1/projects/${projectId}/secrets`, {
          token: op.token,
          body: { name: 'API_KEY', value: SECRET_SENTINEL },
        })
      ).status,
    ).toBe(201);
    expect(
      (
        await req('DELETE', `/v1/projects/${projectId}/secrets/API_KEY`, { token: op.token })
      ).status,
    ).toBe(204);

    // 2. Agent registration + key rotation: capture the plaintext keys.
    const reg = await req('POST', '/v1/agents/register', {
      provisioningToken: PROV,
      body: { name: 'rotator', permissions: { read_status: true } },
    });
    expect(reg.status).toBe(201);
    const regKey: string = reg.body.api_key;
    expect(typeof regKey).toBe('string');
    const rot = await req('POST', '/v1/agents/me/rotate', { token: regKey });
    expect(rot.status).toBe(200);
    const rotatedKey: string = rot.body.api_key;
    expect(typeof rotatedKey).toBe('string');
    expect(rotatedKey).not.toBe(regKey);

    // 3. Host registration + token rotation: capture the plaintext tokens.
    const hreg = await req('POST', '/v1/hosts/register', {
      provisioningToken: PROV,
      body: { name: 'host-1' },
    });
    expect(hreg.status).toBe(201);
    const hostToken: string = hreg.body.host_token;
    const hostId: string = hreg.body.host.id;
    const hrot = await req('POST', `/v1/hosts/${hostId}/rotate-token`, { token: hostToken });
    expect(hrot.status).toBe(200);
    const rotatedHostToken: string = hrot.body.host_token;
    expect(typeof rotatedHostToken).toBe('string');

    // 4. Lifecycle transitions (suspend/resume/revoke) on a fresh agent.
    const victim = await seedAgent('victim', READ_ONLY);
    for (const action of ['suspend', 'resume', 'revoke'] as const) {
      const r = await req('POST', `/v1/agents/${victim.id}/${action}`, { provisioningToken: PROV });
      expect(r.status).toBe(200);
    }

    // 5. A failed provisioning attempt (auth.failed audit event).
    expect(
      (await req('POST', '/v1/agents/register', { provisioningToken: WRONG_PROV, body: { name: 'x' } }))
        .status,
    ).toBe(401);

    // Sweep every event row.
    const { rows } = await state.pool.query('SELECT id, type, payload FROM events');
    expect(rows.length).toBeGreaterThan(5);
    const sentinels = [SECRET_SENTINEL, regKey, rotatedKey, hostToken, rotatedHostToken, WRONG_PROV, PROV];
    const forbiddenKeys = [
      'api_key', 'host_token', 'token', 'secret_value', 'value',
      'encrypted_value', 'password', 'provisioning_token',
    ];
    for (const row of rows) {
      const serialized = JSON.stringify(row);
      for (const s of sentinels) {
        expect(serialized).not.toContain(s);
      }
      const payload = row.payload as Record<string, unknown>;
      for (const k of Object.keys(payload ?? {})) {
        expect(forbiddenKeys).not.toContain(k);
      }
    }
  });
});
