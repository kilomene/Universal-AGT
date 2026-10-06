// W13a §44 — Cloudflare API failure: the domain records a failed state
// with the error, the deployment itself is unaffected, and provisioning
// recovers cleanly once the API is back.
//
// Boots the REAL express app against pg-mem and points the Cloudflare
// client at a dead port (connection refused) to simulate the API outage,
// then at the fake Cloudflare API (test/fakeCloudflare.ts) for recovery.
import http from 'http';
import { tmpdir } from 'os';
import { mkdtempSync } from 'fs';
import { join } from 'path';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

// §5 SSRF hardening: the HTTPS probe resolves DNS itself and dials via
// node:https with a pinned lookup — mock both (no real DNS/network).
const dnsMock = vi.hoisted(() => {
  let v4: string[] = ['93.184.216.34'];
  let v6: string[] = [];
  return {
    resolve4: vi.fn(async (): Promise<string[]> => v4),
    resolve6: vi.fn(async (): Promise<string[]> => v6),
    set: (a: string[], aaaa: string[]) => {
      v4 = a;
      v6 = aaaa;
    },
  };
});
vi.mock('node:dns/promises', () => ({
  resolve4: dnsMock.resolve4,
  resolve6: dnsMock.resolve6,
}));

vi.mock('node:https', () => ({
  default: {
    get: vi.fn((_url: string, _opts: any, cb: (res: any) => void) => {
      const req = { on: () => req, destroy: () => {} };
      queueMicrotask(() => cb({ statusCode: 200, resume: () => {} }));
      return req;
    }),
  },
}));

import { createApp } from '../src/index';
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { startFakeCloudflare, type FakeCloudflare } from './fakeCloudflare';

const TUNNEL_HOST = '11111111-2222-3333-4444-555555555555.cfargotunnel.com';

function setupDb() {
  const db = newDb();
  db.public.registerFunction({
    name: 'gen_random_uuid',
    returns: 'text',
    impure: true,
    implementation: () => randomUUID(),
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      permissions JSONB, api_key_hash TEXT,
      idempotency_key TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ,
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
      id TEXT PRIMARY KEY DEFAULT gen_random_uuid(), hostname TEXT NOT NULL,
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
let fake: FakeCloudflare;

const CF_ENV = {
  CLOUDFLARE_API_TOKEN: 'test-dns-token',
  CLOUDFLARE_ZONE_ID: 'zone-1',
  CLOUDFLARE_ACCOUNT_ID: 'acct-1',
  CLOUDFLARE_TUNNEL_API_TOKEN: 'test-tunnel-token',
  TUNNEL_INGRESS_HOSTNAME: TUNNEL_HOST,
  PUBLIC_INGRESS_HOSTNAME: 'ingress.example.net',
};

function setCfEnv(apiBase: string) {
  for (const [k, v] of Object.entries(CF_ENV)) process.env[k] = v;
  process.env.CLOUDFLARE_API_BASE = apiBase;
}

function clearCfEnv() {
  for (const k of Object.keys(CF_ENV)) delete process.env[k];
  delete process.env.CLOUDFLARE_API_BASE;
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

async function seedAll() {
  const { token: agentToken, hash: agentHash } = generateAgentKey();
  await state.pool.query(
    `INSERT INTO agents (id, name, permissions, api_key_hash, status)
     VALUES ($1, 'w13a-cf-agent', $2, $3, 'active')`,
    [randomUUID(), JSON.stringify({ manage_domains: true, read_status: true }), agentHash],
  );
  const { token: hostToken, hash: hostHash } = generateHostToken();
  const hostId = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status) VALUES ($1, 'h1', $2, 'online')`,
    [hostId, hostHash],
  );
  const projectId = randomUUID();
  await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'demo')`, [projectId]);
  const depId = randomUUID();
  await state.pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status)
     VALUES ($1, $2, $3, '1.0.0', 'running')`,
    [depId, projectId, hostId],
  );
  await state.pool.query(
    `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, 18001, $2)`,
    [hostId, depId],
  );
  return { agentToken, hostToken, depId, hostId };
}

async function deploymentStatus(depId: string) {
  const { rows } = await state.pool.query(`SELECT status FROM deployments WHERE id = $1`, [depId]);
  return rows[0].status;
}

beforeAll(async () => {
  process.env.LOG_DIR = mkdtempSync(join(tmpdir(), 'uaht-cf-logs-'));
  fake = await startFakeCloudflare();
  setupDb();
  const app = createApp();
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  base = `http://127.0.0.1:${(addr as any).port}`;
  // HTTPS reachability probes: canned 200 (an answering origin); every
  // other fetch (the test's own HTTP, the fake CF API) goes through.
  const realFetch = globalThis.fetch.bind(globalThis);
  vi.stubGlobal('fetch', async (url: unknown, init?: RequestInit) => {
    const s = String(url);
    if (s.startsWith('https://')) {
      return { ok: true, status: 200, body: { cancel: async () => {} } };
    }
    return realFetch(s, init as any);
  });
});

afterAll(async () => {
  vi.unstubAllGlobals();
  clearCfEnv();
  await state.pool.end();
  await fake.close();
  await new Promise<void>((r) => server.close(() => r()));
});

describe('§44 Cloudflare API failure (real API)', () => {
  it('CF API down -> domain stays failed with the error; deployment unaffected; recovers when CF returns', async () => {
    const { agentToken, depId } = await seedAll();
    const hostname = 'app1.example.com';

    // --- outage: point the CF client at a dead port (connection refused)
    const deadPort = 9; // discard port: nothing listens here
    setCfEnv(`http://127.0.0.1:${deadPort}/client/v4`);
    try {
      const res = await req('POST', '/v1/domains', agentToken, {
        deployment_id: depId, hostname, ingress: 'tunnel',
      });
      expect(res.status).toBe(201);
      const domain = res.body.domain;
      expect(domain.status).toBe('failed');
      expect(domain.error).toMatch(/DNS configuration failed/i);

      const { rows } = await state.pool.query(
        `SELECT status, error FROM domains WHERE hostname = $1`, [hostname]);
      expect(rows[0].status).toBe('failed');
      expect(rows[0].error).toMatch(/DNS configuration failed/i);

      // The deployment itself is untouched by the CF outage.
      expect(await deploymentStatus(depId)).toBe('running');

      const { rows: ev } = await state.pool.query(
        `SELECT type FROM events WHERE deployment_id = $1 ORDER BY id`, [depId]);
      expect(ev.map((r: any) => r.type)).toContain('domain.failed');

      // No DNS record or tunnel route was created anywhere.
      expect(fake.dnsRecords).toHaveLength(0);
    } finally {
      clearCfEnv();
    }

    // --- recovery: CF back -> remove the failed row -> re-attach -> active
    setCfEnv(fake.url);
    try {
      const del = await req('DELETE', '/v1/domains', agentToken, {
        deployment_id: depId, hostname,
      });
      expect(del.status).toBe(200);

      const retry = await req('POST', '/v1/domains', agentToken, {
        deployment_id: depId, hostname, ingress: 'tunnel',
      });
      expect(retry.status).toBe(201);
      expect(retry.body.domain.status).toBe('active');
      expect(retry.body.domain.error).toBeNull();
      expect(fake.dnsRecords.some((r: any) => r.name === hostname)).toBe(true);

      // The deployment rode out the whole episode still running.
      expect(await deploymentStatus(depId)).toBe('running');
    } finally {
      clearCfEnv();
    }
  });
});
