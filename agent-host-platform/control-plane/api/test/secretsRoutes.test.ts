// W11 secrets route tests (spec §27, §32).
//
// Boots the REAL express app (createApp) against a pg-mem database via a
// vi.mock'd db/pool — genuine route-level assertions (status codes, row
// state, emitted events), not handler unit tests.
//
// Covers the full secret path on the control-plane side:
//   * POST /v1/projects/:id/secrets encrypts with AES-256-GCM and never
//     returns the value;
//   * GET /v1/projects/:id/secrets returns names/metadata ONLY;
//   * rotation/change emits `secret.changed`, deletion emits
//     `secret.deleted` — both without values;
//   * manage_secrets permission gating;
//   * the worker pull endpoint refuses agent tokens (host token only).
//
// Placeholder values only (test-key-not-real): nothing here may trip the
// secrets sweep (scripts/secrets-sweep.sh).
import http from 'http';
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
import { encryptSecret } from '../src/lib/secrets';
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { resetRateLimitBucketsForTests } from '../src/middleware/rateLimit';

const FIRST = 'test-key-not-real';
const ROTATED = 'rotated-test-key-not-real';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_uuid',
    args: [],
    returns: 'text',
    // impure: a fresh id on every call, like gen_random_uuid() in production
    // (pg-mem memoizes pure function results, which would hand every
    // default-id insert the same id).
    impure: true,
    implementation: () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`,
  });
  // pg-mem mangles Buffer query PARAMETERS on the bytea path (bytes do not
  // roundtrip); inserting via a registered function that returns a Buffer
  // keeps ciphertext intact for the worker pull test below.
  db.public.registerFunction({
    name: 'hex_to_bytea',
    args: ['text'],
    returns: 'bytea',
    implementation: (hex: string) => Buffer.from(hex, 'hex'),
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
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE secrets (
      id TEXT PRIMARY KEY DEFAULT gen_uuid(),
      project_id TEXT NOT NULL, name TEXT NOT NULL,
      encrypted_value BYTEA NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now(),
      UNIQUE (project_id, name)
    );
    CREATE TABLE events (
      id SERIAL PRIMARY KEY, type TEXT NOT NULL,
      actor_type TEXT, actor_id TEXT, task_id TEXT,
      deployment_id TEXT, host_id TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      claimed_by TEXT, payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
      host_id TEXT NOT NULL, version TEXT,
      status TEXT NOT NULL DEFAULT 'requested'
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

const A1 = '11111111-1111-1111-1111-111111111111';
const A2 = '22222222-2222-2222-2222-222222222222';
const H1 = '33333333-3333-3333-3333-333333333333';
const P1 = '55555555-5555-5555-5555-555555555555';

let base = '';
let server: http.Server;

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
  await state.pool.query(`INSERT INTO hosts (id, name, token_hash, status) VALUES ($1, $2, $3, 'online')`, [
    id,
    name,
    hash,
  ]);
  return token;
}

async function lastEvent() {
  const { rows } = await state.pool.query('SELECT type, payload FROM events ORDER BY id DESC LIMIT 1');
  return rows[0] as { type: string; payload: Record<string, unknown> };
}

beforeAll(async () => {
  process.env.ARTIFACT_DIR = mkdtempSync(join(tmpdir(), 'uaht-artifacts-'));
  process.env.DATA_ENCRYPTION_KEY = 'cd'.repeat(32);
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
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
  resetRateLimitBucketsForTests();
  for (const t of ['secrets', 'events', 'tasks', 'deployments', 'hosts', 'agents', 'projects']) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
  await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'proj')`, [P1]);
});

const post = (token: string | null, body: unknown) =>
  fetch(`${base}/v1/projects/${P1}/secrets`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });

// ---------------------------------------------------------------------------
// POST /v1/projects/:id/secrets — store/rotate, encrypted, never returned
// ---------------------------------------------------------------------------
describe('POST /v1/projects/:id/secrets', () => {
  it('201s, returns metadata only, encrypts at rest, emits secret.changed without the value', async () => {
    const token = await seedAgent(A1, 'keeper', { manage_secrets: true });
    const res = await post(token, { name: 'API_KEY', value: FIRST });
    expect(res.status).toBe(201);
    const raw = await res.text();
    expect(raw).not.toContain(FIRST);
    const body = JSON.parse(raw);
    expect(body.secret.name).toBe('API_KEY');
    expect(body.secret.project_id).toBe(P1);
    expect('value' in body.secret).toBe(false);
    expect('encrypted_value' in body.secret).toBe(false);

    // At rest: the stored bytes are ciphertext, never plaintext. (pg-mem
    // mangles Buffer query params on the bytea write path, so this asserts
    // on the stored bytes directly rather than decrypting: ASCII plaintext
    // would survive the mangling, ciphertext never contains it. Real
    // encrypt->decrypt round-trips are covered by secretsCrypto.test.ts and
    // the worker pull test below.)
    const { rows } = await state.pool.query('SELECT encrypted_value FROM secrets WHERE name = $1', ['API_KEY']);
    expect(rows).toHaveLength(1);
    const blob: Buffer = rows[0].encrypted_value;
    expect(Buffer.isBuffer(blob)).toBe(true);
    expect(blob.length).toBeGreaterThan(Buffer.byteLength(FIRST));
    expect(blob.toString('utf8')).not.toContain(FIRST);

    // Event journal: secret.changed, name only.
    const ev = await lastEvent();
    expect(ev.type).toBe('secret.changed');
    expect(ev.payload).toEqual({ project_id: P1, name: 'API_KEY' });
    expect(JSON.stringify(ev.payload)).not.toContain(FIRST);
  });

  it('rotation (second POST) updates the ciphertext and emits secret.changed again', async () => {
    const token = await seedAgent(A1, 'keeper', { manage_secrets: true });
    expect((await post(token, { name: 'API_KEY', value: FIRST })).status).toBe(201);
    const before = (
      await state.pool.query('SELECT encrypted_value FROM secrets WHERE project_id = $1 AND name = $2', [P1, 'API_KEY'])
    ).rows[0].encrypted_value as Buffer;
    expect((await post(token, { name: 'API_KEY', value: ROTATED })).status).toBe(201);
    const { rows } = await state.pool.query('SELECT encrypted_value FROM secrets WHERE project_id = $1 AND name = $2', [
      P1,
      'API_KEY',
    ]);
    expect(rows).toHaveLength(1);
    const after = rows[0].encrypted_value as Buffer;
    // Randomized AES-256-GCM: rotation rewrites the stored bytes.
    expect(after.equals(before)).toBe(false);
    expect(after.toString('utf8')).not.toContain(FIRST);
    expect(after.toString('utf8')).not.toContain(ROTATED);
    const { rows: evs } = await state.pool.query(
      `SELECT type, payload FROM events WHERE type = 'secret.changed' ORDER BY id`,
    );
    expect(evs).toHaveLength(2);
    for (const e of evs) {
      expect(e.payload).toEqual({ project_id: P1, name: 'API_KEY' });
      expect(JSON.stringify(e.payload)).not.toContain(ROTATED);
    }
  });

  it('401s without a token, 403s without manage_secrets', async () => {
    expect((await post(null, { name: 'X', value: FIRST })).status).toBe(401);
    const token = await seedAgent(A2, 'reader', { read_status: true });
    const res = await post(token, { name: 'X', value: FIRST });
    expect(res.status).toBe(403);
  });

  it('400s on bad input, 404s on unknown project', async () => {
    const token = await seedAgent(A1, 'keeper', { manage_secrets: true });
    for (const body of [{}, { name: 'X' }, { value: FIRST }, { name: '', value: FIRST }, { name: 'X', value: '' }]) {
      expect((await post(token, body)).status).toBe(400);
    }
    expect((await post(token, { name: 'X'.repeat(129), value: FIRST })).status).toBe(400);
    const res = await fetch(`${base}/v1/projects/99999999-9999-9999-9999-999999999999/secrets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ name: 'X', value: FIRST }),
    });
    expect(res.status).toBe(404);
  });
});

// ---------------------------------------------------------------------------
// GET /v1/projects/:id/secrets — names/metadata ONLY, never values
// ---------------------------------------------------------------------------
describe('GET /v1/projects/:id/secrets', () => {
  it('returns names only; the raw response body never contains a value', async () => {
    const token = await seedAgent(A1, 'keeper', { manage_secrets: true });
    await post(token, { name: 'API_KEY', value: FIRST });
    await post(token, { name: 'DB_PASS', value: 'another-test-key-not-real' });
    const res = await fetch(`${base}/v1/projects/${P1}/secrets`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(200);
    const raw = await res.text();
    expect(raw).not.toContain(FIRST);
    expect(raw).not.toContain('another-test-key-not-real');
    const body = JSON.parse(raw);
    expect(body.secrets.map((s: any) => s.name).sort()).toEqual(['API_KEY', 'DB_PASS']);
    for (const s of body.secrets) {
      expect(Object.keys(s).sort()).toEqual(['created_at', 'name', 'updated_at']);
    }
  });

  it('403s without manage_secrets', async () => {
    const token = await seedAgent(A2, 'reader', { read_status: true });
    const res = await fetch(`${base}/v1/projects/${P1}/secrets`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// DELETE /v1/projects/:id/secrets/:name — emits secret.deleted, no value
// ---------------------------------------------------------------------------
describe('DELETE /v1/projects/:id/secrets/:name', () => {
  it('204s, removes the row, emits secret.deleted without the value', async () => {
    const token = await seedAgent(A1, 'keeper', { manage_secrets: true });
    await post(token, { name: 'API_KEY', value: FIRST });
    const res = await fetch(`${base}/v1/projects/${P1}/secrets/API_KEY`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(204);
    const { rows } = await state.pool.query('SELECT id FROM secrets WHERE name = $1', ['API_KEY']);
    expect(rows).toHaveLength(0);
    const ev = await lastEvent();
    expect(ev.type).toBe('secret.deleted');
    expect(ev.payload).toEqual({ project_id: P1, name: 'API_KEY' });
    expect(JSON.stringify(ev.payload)).not.toContain(FIRST);
  });

  it('404s for a missing secret, 403s without manage_secrets', async () => {
    const keeper = await seedAgent(A1, 'keeper', { manage_secrets: true });
    expect(
      (
        await fetch(`${base}/v1/projects/${P1}/secrets/NOPE`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${keeper}` },
        })
      ).status,
    ).toBe(404);
    const reader = await seedAgent(A2, 'reader', { read_status: true });
    expect(
      (
        await fetch(`${base}/v1/projects/${P1}/secrets/NOPE`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${reader}` },
        })
      ).status,
    ).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// Worker pull endpoint: host tokens only, never agent tokens
// ---------------------------------------------------------------------------
describe('GET /v1/worker/projects/:id/secrets auth boundary', () => {
  it('403s an agent token (host token required)', async () => {
    const token = await seedAgent(A1, 'keeper', { manage_secrets: true });
    const res = await fetch(`${base}/v1/worker/projects/${P1}/secrets`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(403);
  });

  it('401s with no token at all', async () => {
    const res = await fetch(`${base}/v1/worker/projects/${P1}/secrets`);
    expect(res.status).toBe(401);
  });

  it('200s for the bound host with live work (values only over this channel)', async () => {
    // Seeded via hex_to_bytea: pg-mem mangles Buffer params on the bytea
    // write path, and this test needs intact ciphertext for the
    // server-side decrypt.
    const encHex = encryptSecret(FIRST).toString('hex');
    await state.pool.query(
      `INSERT INTO secrets (project_id, name, encrypted_value)
       VALUES ($1, 'API_KEY', hex_to_bytea('${encHex}'))`,
      [P1],
    );
    const htoken = await seedHost(H1, 'host-a');
    await state.pool.query(
      `INSERT INTO tasks (id, type, status, claimed_by, payload)
       VALUES ('77777777-7777-7777-7777-777777777777', 'deploy', 'claimed', $1, $2)`,
      [H1, JSON.stringify({ project_id: P1 })],
    );
    const res = await fetch(`${base}/v1/worker/projects/${P1}/secrets`, {
      headers: { Authorization: `Bearer ${htoken}` },
    });
    expect(res.status).toBe(200);
    expect((await res.json()).secrets).toEqual({ API_KEY: FIRST });
  });
});
