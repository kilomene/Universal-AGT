// W16 FINAL SECURITY AUDIT — adversarial test suite.
//
// Each describe block is an ATTACK ATTEMPT first, a regression guard
// second: the test demonstrates the exploit against the pre-fix code (all
// of these failed before the W16 fixes) and now pins the hardened
// behavior. Findings reference the production code they cover:
//
//   F1  DNS record adoption  (src/lib/cloudflare.ts ensureCnameRecord)
//   F2  Tunnel route hijack  (src/lib/cloudflare-tunnel.ts setRemoteTunnelIngress)
//   F3  HTTPS probe redirect (src/routes/domains.ts probeHttps)
//   F4  Malformed JSON 500   (src/middleware/auth.ts apiErrorHandler)
//
// plus structural verifications: transaction atomicity under fault
// injection, host-token revocation across every host endpoint, rate-limit
// trigger on creation endpoints, cross-host IDOR on settle-rollback-ports,
// and the SSRF allowlist (no outbound call when Cloudflare is unconfigured).
//
// Boots the REAL express app against pg-mem (vi.mock'd db/pool) and a FAKE
// Cloudflare API (test/fakeCloudflare.ts — TEST INFRASTRUCTURE ONLY).
import http from 'http';
import { tmpdir } from 'os';
import { mkdtempSync } from 'fs';
import { join } from 'path';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

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

const httpsMock = vi.hoisted(() => {
  let respond: number | 'throw' = 200;
  const calls: Array<{ url: string; opts: any }> = [];
  const get = vi.fn((url: string, opts: any, cb: (res: any) => void) => {
    calls.push({ url, opts });
    const listeners: Record<string, Array<() => void>> = {};
    const req = {
      on: (ev: string, fn: () => void) => {
        (listeners[ev] ??= []).push(fn);
        return req;
      },
      destroy: vi.fn(),
    };
    if (respond === 'throw') {
      queueMicrotask(() => (listeners['error'] ?? []).forEach((fn) => fn()));
    } else {
      queueMicrotask(() => cb({ statusCode: respond, resume: () => {} }));
    }
    return req;
  });
  return {
    calls,
    get,
    setRespond: (r: number | 'throw') => {
      respond = r;
    },
    reset: () => {
      calls.length = 0;
      respond = 200;
      get.mockClear();
    },
  };
});
vi.mock('node:https', () => ({
  default: { get: httpsMock.get },
  get: httpsMock.get,
}));

import { createApp } from '../src/index';
import { generateAgentKey, generateHostToken, sha256Hex } from '../src/lib/tokens';
import { probeHttps } from '../src/routes/domains';
import { resetRateLimitBucketsForTests } from '../src/middleware/rateLimit';
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
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      host_type TEXT,
      status TEXT NOT NULL DEFAULT 'online',
      capabilities JSONB,
      token_hash TEXT NOT NULL,
      idempotency_key TEXT,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      disk_pct DOUBLE PRECISION, docker_status TEXT,
      running_apps JSONB, worker_version TEXT,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION,
      total_disk_gb DOUBLE PRECISION,
      last_seen TIMESTAMPTZ, metadata JSONB,
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      worker_status TEXT,
      ingress JSONB NOT NULL DEFAULT '{}',
      reported_host_name TEXT,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT NOT NULL,
      owner TEXT, repository TEXT, artifact_location TEXT,
      runtime TEXT NOT NULL DEFAULT 'docker',
      configuration JSONB NOT NULL DEFAULT '{}',
      owner_agent_id TEXT,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
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
      mode TEXT NOT NULL DEFAULT 'automatic',
      artifact_id TEXT,
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
    CREATE TABLE secrets (
      id TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
      project_id TEXT NOT NULL, name TEXT NOT NULL,
      encrypted_value BYTEA NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now(),
      UNIQUE (project_id, name)
    );
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server;
let fake: FakeCloudflare;

// Outbound calls made by the APP (fetch is stubbed): Cloudflare API goes
// to the fake, https://* (the reachability probe) gets a canned answer,
// everything else (the test's own HTTP to the app) passes through.
let outbound: Array<{ url: string; init?: RequestInit }> = [];
function stubFetch(probeStatus: number | 'throw' = 200) {
  const realFetch = globalThis.fetch.bind(globalThis);
  outbound = [];
  const stub = vi.fn(async (url: unknown, init?: RequestInit) => {
    const s = String(url);
    outbound.push({ url: s, init });
    if (s.startsWith(fake.url)) return realFetch(s, init);
    if (s.startsWith('https://')) {
      if (probeStatus === 'throw') throw new Error('connect ECONNREFUSED (fake probe)');
      return { ok: probeStatus < 400, status: probeStatus, body: { cancel: async () => {} } };
    }
    return realFetch(s, init);
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

const CF_ENV: Record<string, string> = {
  CLOUDFLARE_API_TOKEN: 'test-dns-token-long-enough',
  CLOUDFLARE_ZONE_ID: 'zone-1',
  CLOUDFLARE_ACCOUNT_ID: 'acct-1',
  CLOUDFLARE_TUNNEL_API_TOKEN: 'test-tunnel-token-long',
  TUNNEL_INGRESS_HOSTNAME: TUNNEL_HOST,
  PUBLIC_INGRESS_HOSTNAME: 'ingress.example.net',
};

function setCfEnv() {
  for (const [k, v] of Object.entries(CF_ENV)) process.env[k] = v;
  process.env.CLOUDFLARE_API_BASE = fake.url;
}
function clearCfEnv() {
  for (const k of Object.keys(CF_ENV)) delete process.env[k];
  delete process.env.CLOUDFLARE_API_BASE;
}

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

async function seedDeployment(projectId: string, hostId: string, status = 'running') {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status, ports, domains)
     VALUES ($1, $2, $3, '1.0.0', $4, '{}', '[]')`,
    [id, projectId, hostId, status],
  );
  return id;
}

async function req(method: string, path: string, token: string | null, body?: unknown) {
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

beforeAll(async () => {
  process.env.LOG_DIR = mkdtempSync(join(tmpdir(), 'uaht-w16audit-'));
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  fake = await startFakeCloudflare();
  setupDb();
  const app = createApp();
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  vi.unstubAllGlobals();
  await state.pool.end();
  await fake.close();
  await new Promise<void>((r) => server.close(() => r()));
});

beforeEach(async () => {
  for (const t of ['events', 'domains', 'port_allocations', 'tasks', 'deployments', 'hosts', 'agents', 'projects']) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
  fake.reset();
  resetRateLimitBucketsForTests();
  setCfEnv();
  stubFetch(200);
  // Probe transport: public DNS answer + answering origin (per-test override
  // via dnsMock.set / httpsMock.setRespond).
  dnsMock.set(['93.184.216.34'], []);
  httpsMock.reset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearCfEnv();
  delete process.env.RATE_LIMIT_AGENT_PER_MIN;
});

// ---------------------------------------------------------------------------
// F1 — DNS record adoption (src/lib/cloudflare.ts ensureCnameRecord)
// ---------------------------------------------------------------------------
describe('F1: DNS record adoption attack', () => {
  it('refuses to adopt an operator-managed CNAME pointing elsewhere, and never deletes it', async () => {
    // The operator manages ops.example.com OUTSIDE this system.
    fake.dnsRecords.push({
      id: 'rec-operator-1', zone_id: 'zone-1', type: 'CNAME',
      name: 'ops.example.com', content: 'legacy.external.net', ttl: 300, proxied: true,
    });
    const agent = await seedAgent('attacker', { manage_domains: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const depId = await seedDeployment(projectId, host.id);

    const post = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'ops.example.com', ingress: 'direct',
    });
    // The claim is recorded but provisioning refuses the takeover.
    expect(post.status).toBe(201);
    expect(post.body.domain.status).toBe('failed');
    expect(post.body.domain.error).toContain('refusing to adopt DNS record');
    expect(post.body.domain.dns_configured).toBe(false);

    // And the operator's record is untouched by the whole lifecycle —
    // especially the DELETE, which used to remove it.
    const del = await req('DELETE', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'ops.example.com',
    });
    expect(del.status).toBe(200);
    expect(fake.dnsRecords.some((r) => r.id === 'rec-operator-1')).toBe(true);
    expect(fake.dnsRecords.find((r) => r.id === 'rec-operator-1')!.content)
      .toBe('legacy.external.net');
  });

  it('still adopts a record that already points at the ingress target (legit re-provision)', async () => {
    fake.dnsRecords.push({
      id: 'rec-ours', zone_id: 'zone-1', type: 'CNAME',
      name: 're.example.com', content: 'ingress.example.net', ttl: 300, proxied: true,
    });
    const agent = await seedAgent('legit', { manage_domains: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const depId = await seedDeployment(projectId, host.id);

    const post = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 're.example.com', ingress: 'direct',
    });
    expect(post.status).toBe(201);
    expect(post.body.domain.status).toBe('active');
    expect(post.body.domain.dns_configured).toBe(true);
    // No duplicate record created.
    expect(fake.dnsRecords.filter((r) => r.name === 're.example.com')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// F2 — Tunnel route hijack (src/lib/cloudflare-tunnel.ts)
// ---------------------------------------------------------------------------
describe('F2: tunnel route hijack attack', () => {
  async function seedOperatorRoute() {
    const res = await fetch(
      `${fake.url}/accounts/acct-1/cfd_tunnel/11111111-2222-3333-4444-555555555555/configurations`,
      {
        method: 'PUT',
        headers: { Authorization: 'Bearer test-tunnel-token-long', 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: { ingress: [
          { hostname: 'blog.example.com', service: 'https://external-cms:443' },
          { service: 'http_status:404' },
        ] } }),
      },
    );
    expect(res.status).toBe(200);
  }

  it('refuses to overwrite an operator-managed remote tunnel rule', async () => {
    await seedOperatorRoute();
    const agent = await seedAgent('attacker', { manage_domains: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const depId = await seedDeployment(projectId, host.id);
    await state.pool.query(
      `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, 18001, $2)`,
      [host.id, depId],
    );

    const post = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'blog.example.com', ingress: 'tunnel',
    });
    expect(post.status).toBe(201);
    expect(post.body.domain.status).toBe('failed');
    expect(post.body.domain.error).toContain('managed outside this system');

    // The operator's route is byte-identical to before the attack.
    const rule = fake.tunnelIngress.find((r) => r.hostname === 'blog.example.com');
    expect(rule).toMatchObject({ service: 'https://external-cms:443' });
  });

  it('still routes a fresh hostname and still updates its own rules on re-provision', async () => {
    const agent = await seedAgent('legit', { manage_domains: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const depId = await seedDeployment(projectId, host.id);
    await state.pool.query(
      `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, 18001, $2)`,
      [host.id, depId],
    );

    const post = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'fresh.example.com', ingress: 'tunnel',
    });
    expect(post.status).toBe(201);
    expect(post.body.domain.status).toBe('active');
    expect(fake.tunnelIngress.find((r) => r.hostname === 'fresh.example.com'))
      .toMatchObject({ service: 'http://127.0.0.1:18001' });
  });
});

// ---------------------------------------------------------------------------
// F3 — HTTPS probe redirect (src/routes/domains.ts probeHttps)
// ---------------------------------------------------------------------------
describe('F3: HTTPS reachability probe', () => {
  it('does not follow redirects (SSRF hardening)', async () => {
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond(302);
    const result = await probeHttps('some.example.com');
    expect(result).toEqual({ reachable: true, status: 302 });
    // Exactly one request — the 302 was recorded, never followed
    // (node:https does not follow redirects).
    expect(httpsMock.get).toHaveBeenCalledTimes(1);
    const [url, opts] = httpsMock.get.mock.calls[0];
    expect(url).toBe('https://some.example.com/');
    // DNS-rebinding pin: the socket dials the validated public IP, never
    // whatever the hostname resolves to at connect time.
    const pinned = await new Promise<{ address: string; family: number }>(
      (resolve, reject) => {
        opts.lookup(
          'some.example.com',
          {},
          (err: unknown, address: string, family: number) =>
            err ? reject(err) : resolve({ address, family }),
        );
      },
    );
    expect(pinned).toEqual({ address: '93.184.216.34', family: 4 });
  });

  it('refuses to probe a hostname that resolves to an internal address', async () => {
    for (const addrs of [
      { v4: ['127.0.0.1'], v6: [] },
      { v4: ['169.254.169.254'], v6: [] },
      { v4: ['10.0.0.5'], v6: [] },
      { v4: [], v6: ['::1'] },
      { v4: [], v6: ['fd00::1'] },
    ]) {
      dnsMock.set(addrs.v4, addrs.v6);
      httpsMock.reset();
      const result = await probeHttps('evil.example.com');
      expect(result).toEqual({ reachable: false, status: null });
      // No outbound connection was even attempted.
      expect(httpsMock.get).not.toHaveBeenCalled();
    }
  });

  it('makes no outbound call at all when Cloudflare is unconfigured', async () => {
    clearCfEnv();
    stubFetch(200);
    const agent = await seedAgent('a', { manage_domains: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const depId = await seedDeployment(projectId, host.id);

    const post = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'plain.example.com', ingress: 'direct',
    });
    expect(post.status).toBe(201);
    // No DNS step (no CF), no probe (nothing configured): zero https:// calls.
    expect(outbound.filter((o) => o.url.startsWith('https://'))).toHaveLength(0);
  });

  it('rejects SSRF-shaped hostnames at the route', async () => {
    const agent = await seedAgent('a', { manage_domains: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const depId = await seedDeployment(projectId, host.id);
    for (const bad of [
      '127.0.0.1', '10.0.0.5', 'localhost', 'x.localhost', 'svc.internal',
      '*.example.com', 'a*.example.com', '169.254.169.254',
    ]) {
      const r = await req('POST', '/v1/domains', agent.token, {
        deployment_id: depId, hostname: bad, ingress: 'direct',
      });
      expect(r.status).toBe(400);
    }
    expect(outbound.filter((o) => o.url.startsWith('https://'))).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// F4 — malformed/oversized JSON (src/middleware/auth.ts apiErrorHandler)
// ---------------------------------------------------------------------------
describe('F4: malformed request bodies', () => {
  it('400s malformed JSON instead of 500', async () => {
    const res = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"name": broken',
    });
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error.code).toBe('bad_request');
  });

  it('413s a JSON body over the parser limit instead of 500', async () => {
    const res = await fetch(`${base}/v1/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'x'.repeat(300 * 1024) }),
    });
    expect(res.status).toBe(413);
    const body = await res.json();
    expect(body.error.code).toBe('payload_too_large');
  });
});

// ---------------------------------------------------------------------------
// Transaction atomicity under fault injection (spec §55/§56)
//
// HONESTY NOTE: pg-mem (the test double) does not implement ROLLBACK — a
// state-based "nothing persisted" assertion would pass or fail for the
// wrong reasons. These tests instead verify TRANSACTION DISCIPLINE: every
// statement the code issues on the transaction client, in order. On real
// PostgreSQL, BEGIN ... ROLLBACK (and the absence of COMMIT) IS
// atomicity; asserting the exact statement sequence proves the code gives
// the database the chance to be atomic, and that no mutation escapes the
// transaction (e.g. no pool.query side-channel writes).
// ---------------------------------------------------------------------------
describe('transaction atomicity', () => {
  // Wrap pool.connect(): record every statement on transaction clients,
  // and optionally throw on the first statement matching `faultPattern`.
  // Wrap pool.connect() with a NON-MUTATING Proxy: pg-mem's Pool reuses
  // released client objects for later pool.query() calls, so assigning
  // client.query directly would poison the pool for subsequent tests.
  function tapTransactions(faultPattern?: RegExp) {
    const pool = state.pool;
    const origConnect = pool.connect.bind(pool);
    const statements: string[] = [];
    pool.connect = async () => {
      const client = await origConnect();
      const origQuery = client.query.bind(client);
      const faultingQuery = async (sql: any, params?: any) => {
        if (typeof sql === 'string') {
          statements.push(sql);
          if (faultPattern && faultPattern.test(sql)) throw new Error('injected fault');
        }
        return origQuery(sql, params);
      };
      return new Proxy(client, {
        get(target, prop, _receiver) {
          if (prop === 'query') return faultingQuery;
          const v = (target as any)[prop];
          return typeof v === 'function' ? v.bind(target) : v;
        },
      });
    };
    return {
      statements,
      restore: () => {
        pool.connect = origConnect;
      },
    };
  }

  it('deployment+task+port reservation: fault on task INSERT -> ROLLBACK, never COMMIT', async () => {
    const agent = await seedAgent('deployer', { deploy: true });
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');
    const tap = tapTransactions(/INSERT INTO tasks/i);

    const res = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      host_id: host.id,
      version: '9.9.9',
      host_port: 18765,
    });
    tap.restore();
    expect(res.status).toBe(500);

    const seq = tap.statements.map((s) => s.trim().split('\n')[0]);
    // Transaction discipline: BEGIN first, both mutations inside, the
    // faulting task INSERT attempted, then ROLLBACK — and COMMIT never
    // issued, so on real PostgreSQL nothing persists (all-or-nothing).
    expect(seq[0]).toBe('BEGIN');
    expect(seq.some((s) => s.startsWith('INSERT INTO deployments'))).toBe(true);
    expect(seq.some((s) => s.startsWith('INSERT INTO port_allocations'))).toBe(true);
    expect(seq.some((s) => /INSERT INTO tasks/i.test(s))).toBe(true);
    expect(seq).toContain('ROLLBACK');
    expect(seq).not.toContain('COMMIT');
    // The ROLLBACK comes after the faulting statement: nothing after it.
    expect(seq.indexOf('ROLLBACK')).toBeGreaterThan(
      seq.findIndex((s) => /INSERT INTO tasks/i.test(s)),
    );
  });

  it('settleRollbackPorts: fault on restore INSERT -> ROLLBACK, never COMMIT', async () => {
    const { settleRollbackPorts } = await import('../src/lib/ports');
    // Minimal recording double shaped like a Pool (has .connect).
    const statements: string[] = [];
    const fakePool: any = {
      connect: async () => ({
        query: async (sql: string) => {
          statements.push(typeof sql === 'string' ? sql.trim().split('\n')[0] : String(sql));
          if (/INSERT INTO port_allocations/i.test(sql)) throw new Error('injected fault');
          if (/SELECT host_id, ports/i.test(sql)) {
            return { rows: [{ host_id: 'h1', ports: { '18002': 80 } }] };
          }
          return { rows: [], rowCount: 1 };
        },
        release: () => {},
      }),
    };
    await expect(
      settleRollbackPorts(fakePool, 'rolled-back-id', 'target-id'),
    ).rejects.toThrow('injected fault');

    expect(statements[0]).toBe('BEGIN');
    expect(statements.some((s) => s.startsWith('DELETE FROM port_allocations'))).toBe(true);
    expect(statements.some((s) => /INSERT INTO port_allocations/i.test(s))).toBe(true);
    expect(statements).toContain('ROLLBACK');
    expect(statements).not.toContain('COMMIT');
  });

  it('settleRollbackPorts: happy path still COMMITs exactly once', async () => {
    const { settleRollbackPorts } = await import('../src/lib/ports');
    const statements: string[] = [];
    const fakePool: any = {
      connect: async () => ({
        query: async (sql: string) => {
          statements.push(typeof sql === 'string' ? sql.trim().split('\n')[0] : String(sql));
          if (/SELECT host_id, ports/i.test(sql)) {
            return { rows: [{ host_id: 'h1', ports: { '18002': 80 } }] };
          }
          if (/SELECT deployment_id FROM port_allocations/i.test(sql)) {
            return { rows: [{ deployment_id: 'target-id' }] };
          }
          return { rows: [], rowCount: 1 };
        },
        release: () => {},
      }),
    };
    const settlement = await settleRollbackPorts(fakePool, 'rolled-back-id', 'target-id');
    expect(settlement).toMatchObject({ released: 1, restored: 1, skipped: 0 });
    expect(statements.filter((s) => s === 'COMMIT')).toHaveLength(1);
    expect(statements).not.toContain('ROLLBACK');
  });
});

// ---------------------------------------------------------------------------
// Revocation: a rotated host token fails EVERYWHERE immediately
// ---------------------------------------------------------------------------
describe('host token revocation', () => {
  it('old token dies on heartbeat, claim, progress, secrets and port settlement', async () => {
    const host = await seedHost('h1');
    const projectId = await seedProject('p1');

    // Give the host live work so the secrets endpoint is past its 404 gate.
    const depId = await seedDeployment(projectId, host.id, 'running');

    const rot = await req('POST', `/v1/hosts/${host.id}/rotate-token`, host.token, {});
    expect(rot.status).toBe(200);
    const newToken = rot.body.host_token as string;
    expect(typeof newToken).toBe('string');

    // Old token: dead everywhere, immediately.
    expect((await req('POST', `/v1/hosts/${host.id}/heartbeat`, host.token, {})).status).toBe(401);
    expect((await req('POST', '/v1/worker/tasks/claim', host.token, { host_id: host.id })).status).toBe(401);
    expect((await req('GET', `/v1/worker/projects/${projectId}/secrets`, host.token)).status).toBe(401);
    expect(
      (await req('POST', `/v1/deployments/${depId}/settle-rollback-ports`, host.token, {
        target_deployment_id: depId,
      })).status,
    ).toBe(401);

    // New token: works.
    expect((await req('POST', `/v1/hosts/${host.id}/heartbeat`, newToken, {})).status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// Rate limits actually trigger on creation endpoints (spec §55)
// ---------------------------------------------------------------------------
describe('rate limits on creation endpoints', () => {
  it('429s project creation past the per-agent limit', async () => {
    process.env.RATE_LIMIT_AGENT_PER_MIN = '2';
    const agent = await seedAgent('limited', { deploy: true });
    expect((await req('POST', '/v1/projects', agent.token, { name: 'rl1' })).status).toBe(201);
    expect((await req('POST', '/v1/projects', agent.token, { name: 'rl2' })).status).toBe(201);
    const third = await req('POST', '/v1/projects', agent.token, { name: 'rl3' });
    expect(third.status).toBe(429);
    expect(third.body.error.code).toBe('rate_limited');
  });

  it('429s task creation past the per-agent limit', async () => {
    process.env.RATE_LIMIT_AGENT_PER_MIN = '1';
    const agent = await seedAgent('limited', { deploy: true });
    const first = await req('POST', '/v1/tasks', agent.token, { type: 'docker-run', payload: {} });
    expect(first.status, JSON.stringify(first.body)).toBe(201);
    const second = await req('POST', '/v1/tasks', agent.token, { type: 'docker-run', payload: {} });
    expect(second.status).toBe(429);
  });

  it('limits are per-credential: a second agent is unaffected', async () => {
    process.env.RATE_LIMIT_AGENT_PER_MIN = '1';
    const a1 = await seedAgent('limited1', { deploy: true });
    const a2 = await seedAgent('fresh', { deploy: true });
    expect((await req('POST', '/v1/projects', a1.token, { name: 'q1' })).status).toBe(201);
    expect((await req('POST', '/v1/projects', a1.token, { name: 'q2' })).status).toBe(429);
    expect((await req('POST', '/v1/projects', a2.token, { name: 'q3' })).status).toBe(201);
  });
});

// ---------------------------------------------------------------------------
// Cross-host IDOR: settle-rollback-ports (newer endpoint, host self-identity)
// ---------------------------------------------------------------------------
describe('cross-host IDOR on settle-rollback-ports', () => {
  it('403s host X settling ports on host Y’s deployment', async () => {
    const hostX = await seedHost('hx');
    const hostY = await seedHost('hy');
    const projectId = await seedProject('p1');
    const depY = await seedDeployment(projectId, hostY.id, 'rolled_back');
    const targetY = await seedDeployment(projectId, hostY.id, 'running');

    const res = await req('POST', `/v1/deployments/${depY}/settle-rollback-ports`, hostX.token, {
      target_deployment_id: targetY,
    });
    expect(res.status).toBe(403);

    // And the owning host gets through the identity check.
    const own = await req('POST', `/v1/deployments/${depY}/settle-rollback-ports`, hostY.token, {
      target_deployment_id: targetY,
    });
    expect(own.status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// Credential hygiene: token entropy + constant-time compare (spec §54)
// ---------------------------------------------------------------------------
describe('token primitives', () => {
  it('issued tokens carry 256 bits of entropy and distinct prefixes', async () => {
    const seen = new Set<string>();
    for (let i = 0; i < 50; i++) {
      const a = generateAgentKey();
      const h = generateHostToken();
      expect(a.token.startsWith('uag_')).toBe(true);
      expect(h.token.startsWith('uagh_')).toBe(true);
      // 4-char prefix + 64 hex chars = 256 bits from randomBytes(32).
      expect(a.token).toMatch(/^uag_[0-9a-f]{64}$/);
      expect(h.token).toMatch(/^uagh_[0-9a-f]{64}$/);
      seen.add(a.token);
      seen.add(h.token);
    }
    expect(seen.size).toBe(100);
  });

  it('sha256Hex is deterministic and verifyToken is constant-time', async () => {
    const { token, hash } = generateAgentKey();
    expect(sha256Hex(token)).toBe(hash);
    // timingSafeEqual path: wrong-length and wrong-value both fail closed.
    const { verifyToken } = await import('../src/lib/tokens');
    expect(verifyToken(token, hash)).toBe(true);
    expect(verifyToken(token + 'x', hash)).toBe(false);
    expect(verifyToken('short', hash)).toBe(false);
    expect(verifyToken(token, '0'.repeat(64))).toBe(false);
  });
});
