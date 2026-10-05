// Phase 6 security tests (control-plane API side).
//
// Boots the REAL express app (createApp) against a pg-mem database via a
// vi.mock'd db/pool — genuine route-level assertions (status codes), not
// handler unit tests. Pure-function tests cover the permission map, the
// registration gate, the upload cap parsing, and the unauthenticated
// rate-limit bucket.
import http from 'http';
import { tmpdir } from 'os';
import { mkdtempSync } from 'fs';
import { join } from 'path';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, beforeEach, afterEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { checkRegistrationAccess } from '../src/routes/agents';
import { artifactMaxBytes } from '../src/routes/artifacts';
import { permissionForTaskType, TASK_TYPES } from '../src/routes/tasks';
import { rateLimit, resetRateLimitBucketsForTests } from '../src/middleware/rateLimit';
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { encryptSecret } from '../src/lib/secrets';

// ---------------------------------------------------------------------------
// Test database
// ---------------------------------------------------------------------------
function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'test_gen_id',
    args: [],
    returns: 'text',
    implementation: () => `row-${++n}`,
  });
  // pg-mem mangles Buffer query PARAMETERS on the bytea path (bytes do not
  // roundtrip); inserting via a registered function that returns a Buffer
  // keeps ciphertext intact for the secrets tests.
  db.public.registerFunction({
    name: 'hex_to_bytea',
    args: ['text'],
    returns: 'bytea',
    implementation: (hex: string) => Buffer.from(hex, 'hex'),
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(),
      name TEXT NOT NULL, type TEXT,
      capabilities JSONB, permissions JSONB,
      api_key_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ, metadata JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(),
      name TEXT NOT NULL, host_type TEXT,
      status TEXT NOT NULL DEFAULT 'online',
      capabilities JSONB, worker_version TEXT,
      token_hash TEXT NOT NULL,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      disk_pct DOUBLE PRECISION, docker_status TEXT,
      running_apps JSONB, total_cpu DOUBLE PRECISION,
      total_ram_mb DOUBLE PRECISION, total_disk_gb DOUBLE PRECISION,
      last_seen TIMESTAMPTZ, metadata JSONB,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY, project_id TEXT, filename TEXT,
      storage_path TEXT, checksum TEXT, size BIGINT,
      version TEXT, status TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, type TEXT NOT NULL,
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
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
      host_id TEXT NOT NULL, version TEXT,
      status TEXT NOT NULL DEFAULT 'requested'
    );
    CREATE TABLE events (
      id SERIAL PRIMARY KEY, type TEXT NOT NULL,
      actor_type TEXT, actor_id TEXT, task_id TEXT,
      deployment_id TEXT, host_id TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE secrets (
      id TEXT PRIMARY KEY DEFAULT test_gen_id(),
      project_id TEXT NOT NULL, name TEXT NOT NULL,
      encrypted_value BYTEA NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

const A1 = '11111111-1111-1111-1111-111111111111';
const A2 = '22222222-2222-2222-2222-222222222222';
const H1 = '33333333-3333-3333-3333-333333333333';
const H2 = '44444444-4444-4444-4444-444444444444';
const P1 = '55555555-5555-5555-5555-555555555555';
const ART1 = '66666666-6666-6666-6666-666666666666';
const T1 = '77777777-7777-7777-7777-777777777777';

let base = '';

async function seedAgent(id: string, name: string, permissions: Record<string, boolean>) {
  const { token, hash } = generateAgentKey();
  await state.pool.query(
    `INSERT INTO agents (id, name, permissions, api_key_hash, status)
     VALUES ($1, $2, $3, $4, 'active')`,
    [id, name, JSON.stringify(permissions), hash],
  );
  return token;
}

async function seedHost(id: string, name: string) {
  const { token, hash } = generateHostToken();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status) VALUES ($1, $2, $3, 'online')`,
    [id, name, hash],
  );
  return token;
}

let server: http.Server;
let artifactDir = '';

// Env keys individual tests may mutate; saved/restored around each test so
// one test's tuning never leaks into another. ARTIFACT_DIR and
// DATA_ENCRYPTION_KEY are set once in beforeAll and left alone.
const TEST_ENV_KEYS = ['UAHT_PROVISIONING_TOKEN', 'ARTIFACT_MAX_BYTES', 'RATE_LIMIT_UNAUTH_PER_MIN'] as const;
let savedTestEnv: Record<string, string | undefined> = {};

beforeAll(async () => {
  artifactDir = mkdtempSync(join(tmpdir(), 'uaht-artifacts-'));
  process.env.ARTIFACT_DIR = artifactDir;
  process.env.DATA_ENCRYPTION_KEY = 'ab'.repeat(32);
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

beforeEach(() => {
  resetRateLimitBucketsForTests();
  savedTestEnv = {};
  for (const k of TEST_ENV_KEYS) savedTestEnv[k] = process.env[k];
  // Keep the unauthenticated bucket effectively off for route tests (it has
  // its own dedicated unit test below).
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  delete process.env.UAHT_PROVISIONING_TOKEN;
  delete process.env.ARTIFACT_MAX_BYTES;
});

afterEach(() => {
  for (const k of TEST_ENV_KEYS) {
    if (savedTestEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedTestEnv[k];
  }
});

async function wipe() {
  for (const t of ['secrets', 'events', 'tasks', 'deployments', 'artifacts', 'hosts', 'agents', 'projects']) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
}

// ---------------------------------------------------------------------------
// Pure: per-type permission map (Phase 3 fix, verified in Phase 6)
// ---------------------------------------------------------------------------
describe('permissionForTaskType', () => {
  const expected: Record<string, string> = {
    deploy: 'deploy', remove: 'deploy', rollback: 'deploy', build: 'deploy',
    'docker-build': 'deploy', 'docker-run': 'deploy', 'docker-compose': 'deploy',
    'environment-update': 'deploy', 'artifact-upload': 'deploy',
    restart: 'restart', start: 'restart', stop: 'stop',
    logs: 'read_status', status: 'read_status', healthcheck: 'read_status',
    'system-info': 'read_status', 'artifact-download': 'read_status',
  };
  it('covers exactly the 17 protocol task types', () => {
    expect([...TASK_TYPES].sort()).toEqual(Object.keys(expected).sort());
  });
  it('maps every type to its permission', () => {
    for (const [type, perm] of Object.entries(expected)) {
      expect(permissionForTaskType(type), type).toBe(perm);
    }
  });
  it('unknown types stay closed (default to deploy)', () => {
    expect(permissionForTaskType('rm-rf-everything')).toBe('deploy');
    expect(permissionForTaskType('')).toBe('deploy');
  });
});

// ---------------------------------------------------------------------------
// Pure: registration gate
// ---------------------------------------------------------------------------
describe('checkRegistrationAccess', () => {
  let saved: string | undefined;
  beforeEach(() => { saved = process.env.UAHT_PROVISIONING_TOKEN; });
  afterEach(() => {
    if (saved === undefined) delete process.env.UAHT_PROVISIONING_TOKEN;
    else process.env.UAHT_PROVISIONING_TOKEN = saved;
  });
  it('requires the header when the env token is set', () => {
    process.env.UAHT_PROVISIONING_TOKEN = 's3cret';
    expect(checkRegistrationAccess('s3cret', 5)).toEqual({ ok: true });
    expect(checkRegistrationAccess(undefined, 0)).toEqual({
      ok: false, status: 401,
      message: 'agent registration requires the X-Provisioning-Token header',
    });
    expect(checkRegistrationAccess('wrong', 0).ok).toBe(false);
  });
  it('bootstrap mode: open only while no agents exist', () => {
    delete process.env.UAHT_PROVISIONING_TOKEN;
    expect(checkRegistrationAccess(undefined, 0)).toEqual({ ok: true });
    const closed = checkRegistrationAccess(undefined, 1);
    expect(closed.ok).toBe(false);
    if (!closed.ok) expect(closed.status).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// Pure: upload cap parsing
// ---------------------------------------------------------------------------
describe('artifactMaxBytes', () => {
  let saved: string | undefined;
  beforeEach(() => { saved = process.env.ARTIFACT_MAX_BYTES; });
  afterEach(() => {
    if (saved === undefined) delete process.env.ARTIFACT_MAX_BYTES;
    else process.env.ARTIFACT_MAX_BYTES = saved;
  });
  it('defaults to 500MB', () => {
    delete process.env.ARTIFACT_MAX_BYTES;
    expect(artifactMaxBytes()).toBe(500 * 1024 * 1024);
  });
  it('honors the env override, rejects garbage', () => {
    process.env.ARTIFACT_MAX_BYTES = '1048576';
    expect(artifactMaxBytes()).toBe(1048576);
    process.env.ARTIFACT_MAX_BYTES = 'banana';
    expect(artifactMaxBytes()).toBe(500 * 1024 * 1024);
    process.env.ARTIFACT_MAX_BYTES = '-5';
    expect(artifactMaxBytes()).toBe(500 * 1024 * 1024);
  });
});

// ---------------------------------------------------------------------------
// Pure: unauthenticated rate-limit bucket
// ---------------------------------------------------------------------------
describe('rateLimit unauthenticated bucket', () => {
  let saved: string | undefined;
  beforeEach(() => { saved = process.env.RATE_LIMIT_UNAUTH_PER_MIN; });
  afterEach(() => {
    if (saved === undefined) delete process.env.RATE_LIMIT_UNAUTH_PER_MIN;
    else process.env.RATE_LIMIT_UNAUTH_PER_MIN = saved;
  });
  function mockRes() {
    return {
      statusCode: 200 as number,
      body: null as any,
      status(code: number) { this.statusCode = code; return this; },
      json(b: any) { this.body = b; return this; },
    };
  }
  it('throttles unauthenticated requests per IP', () => {
    process.env.RATE_LIMIT_UNAUTH_PER_MIN = '3';
    resetRateLimitBucketsForTests();
    let nexts = 0;
    const next = () => { nexts++; };
    for (let i = 0; i < 3; i++) {
      const res = mockRes();
      rateLimit({ auth: undefined, ip: '10.9.9.9' } as any, res as any, next);
      expect(res.statusCode).toBe(200);
    }
    expect(nexts).toBe(3);
    const res = mockRes();
    rateLimit({ auth: undefined, ip: '10.9.9.9' } as any, res as any, next);
    expect(res.statusCode).toBe(429);
    expect(nexts).toBe(3);
    // a different IP has its own bucket
    const res2 = mockRes();
    rateLimit({ auth: undefined, ip: '10.9.9.10' } as any, res2 as any, next);
    expect(res2.statusCode).toBe(200);
    expect(nexts).toBe(4);
  });
});

// ---------------------------------------------------------------------------
// Route: agent registration gating
// ---------------------------------------------------------------------------
describe('POST /v1/agents/register', () => {
  beforeEach(wipe);
  it('401s without the provisioning token when the env token is set', async () => {
    process.env.UAHT_PROVISIONING_TOKEN = 'prov-token';
    const res = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'agent-x' }),
    });
    expect(res.status).toBe(401);
    const bad = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Provisioning-Token': 'nope' },
      body: JSON.stringify({ name: 'agent-x' }),
    });
    expect(bad.status).toBe(401);
  });
  it('201s with the correct provisioning token', async () => {
    process.env.UAHT_PROVISIONING_TOKEN = 'prov-token';
    const res = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Provisioning-Token': 'prov-token' },
      body: JSON.stringify({ name: 'agent-ok', permissions: { read_status: true } }),
    });
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(body.api_key).toMatch(/^uag_/);
    expect(body.agent.permissions).toEqual({ read_status: true });
  });
  it('bootstrap mode: first registration open, later ones closed', async () => {
    delete process.env.UAHT_PROVISIONING_TOKEN;
    const first = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'first-agent' }),
    });
    expect(first.status).toBe(201);
    const second = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'second-agent' }),
    });
    expect(second.status).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// Route: auth basics
// ---------------------------------------------------------------------------
describe('auth', () => {
  beforeEach(wipe);
  it('401s with no token', async () => {
    const res = await fetch(`${base}/v1/tasks`);
    expect(res.status).toBe(401);
    const body = await res.json();
    expect(body.error.code).toBe('unauthorized');
  });
  it('ignores ?api_key= outside the SSE stream', async () => {
    const token = await seedAgent(A1, 'q-agent', { read_status: true });
    const res = await fetch(`${base}/v1/tasks?api_key=${encodeURIComponent(token)}`);
    expect(res.status).toBe(401);
  });
  it('still honors ?api_key= on GET /v1/events/stream (EventSource)', async () => {
    const token = await seedAgent(A1, 'sse-agent', { read_status: true });
    const ctrl = new AbortController();
    const res = await fetch(`${base}/v1/events/stream?api_key=${encodeURIComponent(token)}&limit=1`, {
      signal: ctrl.signal,
    });
    expect(res.status).toBe(200);
    const reader = res.body!.getReader();
    const { value } = await reader.read();
    const text = new TextDecoder().decode(value);
    expect(text).toContain('connected');
    ctrl.abort();
    await reader.cancel().catch(() => {});
  });
});

// ---------------------------------------------------------------------------
// Route: task permissions + cancel ownership
// ---------------------------------------------------------------------------
describe('tasks', () => {
  beforeEach(wipe);
  it('403s a read-only agent creating a deploy task', async () => {
    const token = await seedAgent(A1, 'reader', { read_status: true });
    const res = await fetch(`${base}/v1/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ type: 'deploy', payload: {} }),
    });
    expect(res.status).toBe(403);
    expect((await res.json()).error.code).toBe('forbidden');
  });
  it('403s cancel by a non-owner without deploy', async () => {
    const ownerToken = await seedAgent(A1, 'owner', { deploy: true, read_status: true });
    const otherToken = await seedAgent(A2, 'other', { read_status: true });
    await state.pool.query(
      `INSERT INTO tasks (id, type, status, created_by, payload) VALUES ($1,'deploy','queued',$2,'{}')`,
      [T1, A1],
    );
    const res = await fetch(`${base}/v1/tasks/${T1}/cancel`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${otherToken}` },
    });
    expect(res.status).toBe(403);
    // positive control: the creator can cancel
    const ok = await fetch(`${base}/v1/tasks/${T1}/cancel`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${ownerToken}` },
    });
    expect(ok.status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// Route: worker endpoints — host token binding
// ---------------------------------------------------------------------------
describe('worker host binding', () => {
  beforeEach(wipe);
  it('403s when the host token does not match the path host', async () => {
    const tokenA = await seedHost(H1, 'host-a');
    await seedHost(H2, 'host-b');
    const res = await fetch(`${base}/v1/hosts/${H2}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tokenA}` },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(403);
  });
  it('200s on the heartbeat for the token’s own host', async () => {
    const tokenA = await seedHost(H1, 'host-a');
    const res = await fetch(`${base}/v1/hosts/${H1}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tokenA}` },
      body: JSON.stringify({ worker_version: '9.9.9' }),
    });
    expect(res.status).toBe(200);
  });
  it('never clobbers an operator-set draining state (Phase 2 fix, verified)', async () => {
    const tokenA = await seedHost(H1, 'host-a');
    await state.pool.query(`UPDATE hosts SET status = 'draining' WHERE id = $1`, [H1]);
    const res = await fetch(`${base}/v1/hosts/${H1}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tokenA}` },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(200);
    const { rows } = await state.pool.query(`SELECT status FROM hosts WHERE id = $1`, [H1]);
    expect(rows[0].status).toBe('draining');
  });
  it('401s worker endpoints with a bogus token', async () => {
    const res = await fetch(`${base}/v1/hosts/${H1}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer bogus' },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(401);
  });
});

// ---------------------------------------------------------------------------
// Route: worker project secrets (scoped)
// ---------------------------------------------------------------------------
describe('GET /v1/worker/projects/:id/secrets', () => {
  beforeEach(async () => {
    await wipe();
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'proj')`, [P1]);
    const encHex = encryptSecret('s3cr3t-value').toString('hex');
    await state.pool.query(
      `INSERT INTO secrets (id, project_id, name, encrypted_value)
       VALUES ('sec-1', $1, 'API_KEY', hex_to_bytea('${encHex}'))`,
      [P1],
    );
  });
  it('403s a host with no live work for the project', async () => {
    const token = await seedHost(H1, 'host-a');
    const res = await fetch(`${base}/v1/worker/projects/${P1}/secrets`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(403);
  });
  it('200s with decrypted secrets for a host holding a claimed task', async () => {
    const token = await seedHost(H1, 'host-a');
    await state.pool.query(
      `INSERT INTO tasks (id, type, status, claimed_by, payload)
       VALUES ($1, 'deploy', 'claimed', $2, $3)`,
      [T1, H1, JSON.stringify({ project_id: P1 })],
    );
    const res = await fetch(`${base}/v1/worker/projects/${P1}/secrets`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(200);
    expect((await res.json()).secrets).toEqual({ API_KEY: 's3cr3t-value' });
  });
  it('404s for an unknown project', async () => {
    const token = await seedHost(H1, 'host-a');
    const res = await fetch(`${base}/v1/worker/projects/99999999-9999-9999-9999-999999999999/secrets`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(404);
  });
});

// ---------------------------------------------------------------------------
// Route: artifact upload caps + download scoping
// ---------------------------------------------------------------------------
describe('artifacts', () => {
  beforeEach(wipe);
  it('413s an oversized upload (honest Content-Length)', async () => {
    process.env.ARTIFACT_MAX_BYTES = '1024';
    const token = await seedAgent(A1, 'uploader', { deploy: true });
    const res = await fetch(`${base}/v1/artifacts/${ART1}/content`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/octet-stream' },
      body: Buffer.alloc(2048, 'x'),
      duplex: 'half',
    } as any);
    expect(res.status).toBe(413);
    expect((await res.json()).error.code).toBe('payload_too_large');
  });
  it('413s a body with no Content-Length past the stream cap (chunked)', async () => {
    process.env.ARTIFACT_MAX_BYTES = '1024';
    const token = await seedAgent(A1, 'uploader', { deploy: true });
    // The artifact row must exist so the handler reaches the streaming
    // phase (the upfront Content-Length check is skipped for chunked
    // bodies — the per-chunk stream cap must fire instead).
    await state.pool.query(
      `INSERT INTO artifacts (id, project_id, filename, storage_path, checksum, size, version, status)
       VALUES ($1, $2, 'c.bin', $3, 'sha256:${'ef'.repeat(32)}', 4096, 'v1', 'pending')`,
      [ART1, P1, `${ART1}/c.bin`],
    );
    const { status, body } = await new Promise<{ status?: number; body: string }>((resolve, reject) => {
      const req = http.request(
        `${base}/v1/artifacts/${ART1}/content`,
        {
          method: 'PUT',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/octet-stream',
            // Deliberately no Content-Length: chunked encoding.
          },
        },
        (res) => {
          let data = '';
          res.on('data', (c) => { data += c; });
          res.on('end', () => resolve({ status: res.statusCode, body: data }));
        },
      );
      req.on('error', reject);
      req.write(Buffer.alloc(4096, 'y'));
      req.end();
    });
    expect(status).toBe(413);
    expect(JSON.parse(body).error.code).toBe('payload_too_large');
  });
  it('413s artifact init when the declared size exceeds the cap', async () => {
    process.env.ARTIFACT_MAX_BYTES = '1024';
    const token = await seedAgent(A1, 'uploader', { deploy: true });
    await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'p')`, [P1]);
    const res = await fetch(`${base}/v1/artifacts/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        project_id: P1, filename: 'big.bin', size: 1 << 30,
        checksum: 'sha256:' + 'ab'.repeat(32), version: 'v1',
      }),
    });
    expect(res.status).toBe(413);
  });
  it('403s a host downloading an artifact claimed by another host', async () => {
    const tokenA = await seedHost(H1, 'host-a');
    await seedHost(H2, 'host-b');
    await state.pool.query(
      `INSERT INTO artifacts (id, project_id, filename, storage_path, checksum, size, version, status)
       VALUES ($1, $2, 'f.bin', $3, 'sha256:${'cd'.repeat(32)}', 10, 'v1', 'ready')`,
      [ART1, P1, `${ART1}/f.bin`],
    );
    await state.pool.query(
      `INSERT INTO tasks (id, type, status, claimed_by, payload) VALUES ($1,'build','running',$2,$3)`,
      [T1, H2, JSON.stringify({ artifact_id: ART1 })],
    );
    const res = await fetch(`${base}/v1/artifacts/${ART1}/download`, {
      headers: { Authorization: `Bearer ${tokenA}` },
    });
    expect(res.status).toBe(403);
  });
  it('lets the claiming host past the scoping check', async () => {
    const tokenB = await seedHost(H2, 'host-b');
    await state.pool.query(
      `INSERT INTO artifacts (id, project_id, filename, storage_path, checksum, size, version, status)
       VALUES ($1, $2, 'f.bin', $3, 'sha256:${'cd'.repeat(32)}', 10, 'v1', 'ready')`,
      [ART1, P1, `${ART1}/f.bin`],
    );
    await state.pool.query(
      `INSERT INTO tasks (id, type, status, claimed_by, payload) VALUES ($1,'build','running',$2,$3)`,
      [T1, H2, JSON.stringify({ artifact_id: ART1 })],
    );
    const res = await fetch(`${base}/v1/artifacts/${ART1}/download`, {
      headers: { Authorization: `Bearer ${tokenB}` },
    });
    // Scoping passes (no 403); the file itself was never uploaded, so 404 —
    // the assertion is that the scope check, not the file, is the gate.
    expect(res.status).toBe(404);
  });
  it('keeps agent read_status downloads working', async () => {
    const token = await seedAgent(A1, 'reader', { read_status: true });
    await state.pool.query(
      `INSERT INTO artifacts (id, project_id, filename, storage_path, checksum, size, version, status)
       VALUES ($1, $2, 'f.bin', $3, 'sha256:${'cd'.repeat(32)}', 10, 'v1', 'ready')`,
      [ART1, P1, `${ART1}/f.bin`],
    );
    const res = await fetch(`${base}/v1/artifacts/${ART1}/download`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(404); // no 403: agent path untouched
  });
});

// ---------------------------------------------------------------------------
// Route: token rotation
// ---------------------------------------------------------------------------
describe('token rotation', () => {
  beforeEach(wipe);
  it('rotates an agent key: new works, old dies', async () => {
    const oldToken = await seedAgent(A1, 'rotator', { read_status: true });
    const res = await fetch(`${base}/v1/agents/me/rotate`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${oldToken}` },
    });
    expect(res.status).toBe(200);
    const { api_key: newToken } = await res.json();
    expect(newToken).toMatch(/^uag_/);
    expect(newToken).not.toBe(oldToken);
    const stale = await fetch(`${base}/v1/agents/me`, {
      headers: { Authorization: `Bearer ${oldToken}` },
    });
    expect(stale.status).toBe(401);
    const fresh = await fetch(`${base}/v1/agents/me`, {
      headers: { Authorization: `Bearer ${newToken}` },
    });
    expect(fresh.status).toBe(200);
  });
  it('rotates a host token only for the matching host', async () => {
    const oldToken = await seedHost(H1, 'host-a');
    await seedHost(H2, 'host-b');
    const mismatch = await fetch(`${base}/v1/hosts/${H2}/rotate-token`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${oldToken}` },
    });
    expect(mismatch.status).toBe(403);
    const res = await fetch(`${base}/v1/hosts/${H1}/rotate-token`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${oldToken}` },
    });
    expect(res.status).toBe(200);
    const { host_token: newToken } = await res.json();
    expect(newToken).toMatch(/^uagh_/);
    const stale = await fetch(`${base}/v1/hosts/${H1}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${oldToken}` },
      body: JSON.stringify({}),
    });
    expect(stale.status).toBe(401);
    const fresh = await fetch(`${base}/v1/hosts/${H1}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${newToken}` },
      body: JSON.stringify({}),
    });
    expect(fresh.status).toBe(200);
  });
  it('401s rotation with no token', async () => {
    const res = await fetch(`${base}/v1/agents/me/rotate`, { method: 'POST' });
    expect(res.status).toBe(401);
  });
});
