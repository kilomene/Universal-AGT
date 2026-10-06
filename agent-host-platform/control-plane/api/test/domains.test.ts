// Domain lifecycle E2E (W9/W10): boots the REAL express app against pg-mem
// and drives the full domain flow against a FAKE Cloudflare API
// (test/fakeCloudflare.ts — TEST INFRASTRUCTURE ONLY, not production code).
//
// Covered:
//   POST /v1/domains -> requested -> configuring -> active, with DNS CNAME
//     created AND the remote tunnel ingress rule written (the routing
//     authority for token-based tunnels), both verified by re-read, plus
//     the HTTPS reachability probe and the deployments.domains mirror.
//   Validation: wildcards, localhost/internal/special-use names, IP
//     literals rejected; attach to a terminal deployment -> 422;
//     duplicate hostname -> 409.
//   DELETE /v1/domains -> removing -> removed, with the DNS record and the
//     tunnel route verified gone from the fake.
//   Deployment death (remove task -> stopped): attached domains -> failed
//     with an event each, and the tunnel route dropped from the remote
//     config. Deployment recovery (start task -> running): domains
//     re-provisioned back to active.
//   Tunnel API unconfigured -> failed with an actionable error (the system
//     says plainly it cannot manage remote routes, instead of pretending
//     the worker's local config.yml does it).
//   Direct mode: CNAME -> PUBLIC_INGRESS_HOSTNAME, no tunnel route.
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
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { startFakeCloudflare, type FakeCloudflare } from './fakeCloudflare';

const TUNNEL_HOST = '11111111-2222-3333-4444-555555555555.cfargotunnel.com';

function setupDb() {
  const db = newDb();
  db.public.registerFunction({
    name: 'gen_random_uuid',
    returns: 'text',
    // impure: pg-mem would otherwise constant-fold the DEFAULT expression
    // and reuse one UUID for every inserted row (-> spurious 23505).
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
      status TEXT NOT NULL DEFAULT 'online',
      token_hash TEXT NOT NULL,
      idempotency_key TEXT,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now(),
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION
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

function setCfEnv() {
  for (const [k, v] of Object.entries(CF_ENV)) process.env[k] = v;
  process.env.CLOUDFLARE_API_BASE = fake.url;
}

function clearCfEnv() {
  for (const k of Object.keys(CF_ENV)) delete process.env[k];
  delete process.env.CLOUDFLARE_API_BASE;
}

// Route Cloudflare API calls to the fake; answer HTTPS probes with a
// canned 200 (an answering origin); let the test's own HTTP to the app
// through untouched.
function stubFetch(probeStatus: number | 'throw' = 200) {
  const realFetch = globalThis.fetch.bind(globalThis);
  const stub = vi.fn(async (url: unknown, init?: RequestInit) => {
    const s = String(url);
    if (s.startsWith(fake.url)) return realFetch(s, init);
    if (s.startsWith('https://')) {
      if (probeStatus === 'throw') throw new Error('connect ECONNREFUSED (fake probe)');
      return { ok: true, status: probeStatus, body: { cancel: async () => {} } };
    }
    return realFetch(s, init);
  });
  vi.stubGlobal('fetch', stub);
  return stub;
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

async function seedDeployment(projectId: string, hostId: string, status = 'running') {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status, ports, domains)
     VALUES ($1, $2, $3, '1.0.0', $4, '{}', '[]')`,
    [id, projectId, hostId, status],
  );
  return id;
}

async function req(method: string, path: string, token: string, body?: unknown) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      'Authorization': `Bearer ${token}`,
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

async function eventTypes(): Promise<string[]> {
  const { rows } = await state.pool.query(`SELECT type FROM events ORDER BY id`);
  return rows.map((r: any) => r.type);
}

async function domainRow(hostname: string) {
  const { rows } = await state.pool.query(`SELECT * FROM domains WHERE hostname = $1`, [hostname]);
  return rows[0];
}

beforeAll(async () => {
  process.env.LOG_DIR = mkdtempSync(join(tmpdir(), 'uaht-domain-logs-'));
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
  await state.pool.end();
  await fake.close();
  await new Promise<void>((r) => server.close(() => r()));
});

beforeEach(async () => {
  for (const t of ['events', 'domains', 'port_allocations', 'tasks', 'deployments', 'hosts', 'agents', 'projects']) {
    await state.pool.query(`DELETE FROM ${t}`);
  }
  fake.reset();
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
});

async function seedAll() {
  const agent = await seedAgent('dom-agent', { manage_domains: true, deploy: true });
  const host = await seedHost('h1');
  const projectId = randomUUID();
  await state.pool.query(`INSERT INTO projects (id, name) VALUES ($1, 'demo')`, [projectId]);
  const depId = await seedDeployment(projectId, host.id, 'running');
  await state.pool.query(
    `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, 18001, $2)`,
    [host.id, depId],
  );
  return { agent, host, projectId, depId };
}

describe('POST /v1/domains — tunnel mode full flow', () => {
  it('provisions DNS + remote tunnel route, verifies both, probes HTTPS', async () => {
    const { agent, depId } = await seedAll();

    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId,
      hostname: 'api.example.com',
      ingress: 'tunnel',
    });
    expect(r.status).toBe(201);
    const d = r.body.domain;
    expect(d.hostname).toBe('api.example.com');
    expect(d.ingress).toBe('tunnel');
    expect(d.status).toBe('active');
    expect(d.dns_configured).toBe(true);
    expect(d.tunnel_configured).toBe(true);
    expect(d.https_reachable).toBe(true);
    expect(d.https_status).toBe(200);
    expect(d.verified_at).not.toBeNull();
    expect(d.error).toBeNull();

    // The DNS record exists on the (fake) Cloudflare API: proxied CNAME -> tunnel host.
    const rec = fake.dnsRecords.find((x) => x.name === 'api.example.com');
    expect(rec).toBeDefined();
    expect(rec!.type).toBe('CNAME');
    expect(rec!.content).toBe(TUNNEL_HOST);
    expect(rec!.proxied).toBe(true);

    // The REMOTE tunnel configuration (the routing authority) carries the
    // hostname -> http://127.0.0.1:<host port> rule, catch-all last.
    const rules = fake.tunnelIngress;
    const rule = rules.find((x) => x.hostname === 'api.example.com');
    expect(rule).toBeDefined();
    expect(rule!.service).toBe('http://127.0.0.1:18001');
    expect(rules[rules.length - 1].service).toBe('http_status:404');

    // Events + the deployments.domains summary mirror.
    expect(await eventTypes()).toEqual(['domain.requested', 'domain.active']);
    const dep = await state.pool.query(`SELECT domains FROM deployments WHERE id = $1`, [depId]);
    expect(dep.rows[0].domains).toHaveLength(1);
    expect(dep.rows[0].domains[0]).toMatchObject({ hostname: 'api.example.com', status: 'active' });

    // An ingress-sync task was queued for the worker's LOCAL mirror.
    const tasks = await state.pool.query(`SELECT type, status FROM tasks`);
    expect(tasks.rows).toHaveLength(1);
    expect(tasks.rows[0]).toMatchObject({ type: 'ingress-sync', status: 'queued' });
  });

  it('409s a duplicate hostname (same or different deployment)', async () => {
    const { agent, depId, host, projectId } = await seedAll();
    const other = await seedDeployment(projectId, host.id, 'running');

    const r1 = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r1.status).toBe(201);

    const r2 = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r2.status).toBe(409);

    const r3 = await req('POST', '/v1/domains', agent.token, {
      deployment_id: other, hostname: 'API.example.com', ingress: 'direct',
    });
    expect(r3.status).toBe(409);
  });

  it('400s invalid hostnames: wildcards, localhost, internal, IP literals', async () => {
    const { agent, depId } = await seedAll();
    for (const bad of [
      '*.example.com',
      'api.*.example.com',
      'localhost',
      'api.localhost',
      'app.local',
      'db.internal',
      'x.invalid',
      'y.example',
      'z.test',
      'q.onion',
      '127.0.0.1',
      '10.0.0.5',
    ]) {
      const r = await req('POST', '/v1/domains', agent.token, {
        deployment_id: depId, hostname: bad, ingress: 'tunnel',
      });
      expect(r.status).toBe(400);
    }
    // Nothing reached the fake Cloudflare API and no rows were created.
    expect(fake.dnsRecords).toHaveLength(0);
    const { rows } = await state.pool.query(`SELECT COUNT(*)::int AS n FROM domains`);
    expect(rows[0].n).toBe(0);
  });

  it('422s attaching to a terminal deployment', async () => {
    const { agent, host, projectId } = await seedAll();
    const dead = await seedDeployment(projectId, host.id, 'stopped');
    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: dead, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r.status).toBe(422);
    expect(r.body.error.message).toContain('stopped');
  });

  it('422s tunnel mode without TUNNEL_INGRESS_HOSTNAME', async () => {
    const { agent, depId } = await seedAll();
    delete process.env.TUNNEL_INGRESS_HOSTNAME;
    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r.status).toBe(422);
    expect(r.body.error.message).toContain('TUNNEL_INGRESS_HOSTNAME');
  });

  it('fails honestly when the tunnel API is not configured (no pretending the local mirror routes)', async () => {
    const { agent, depId } = await seedAll();
    delete process.env.CLOUDFLARE_ACCOUNT_ID;
    delete process.env.CLOUDFLARE_TUNNEL_API_TOKEN;

    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r.status).toBe(201);
    expect(r.body.domain.status).toBe('failed');
    expect(r.body.domain.error).toContain('tunnel routes cannot be managed remotely');
    expect(r.body.domain.error).toContain('dashboard');
    // DNS was still configured (that API is available); the tunnel rule was not.
    expect(r.body.domain.dns_configured).toBe(true);
    expect(r.body.domain.tunnel_configured).toBe(false);
    expect(fake.tunnelIngress).toHaveLength(1); // catch-all only
    expect(await eventTypes()).toEqual(['domain.requested', 'domain.failed']);
  });

  it('records https_reachable=false when the probe fails, without blocking active', async () => {
    const { agent, depId } = await seedAll();
    httpsMock.setRespond('throw');
    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r.status).toBe(201);
    expect(r.body.domain.status).toBe('active');
    expect(r.body.domain.https_reachable).toBe(false);
    expect(r.body.domain.https_status).toBeNull();
  });
});

describe('POST /v1/domains — direct mode', () => {
  it('creates CNAME -> PUBLIC_INGRESS_HOSTNAME and no tunnel route', async () => {
    const { agent, depId } = await seedAll();
    const r = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'www.example.com', ingress: 'direct',
    });
    expect(r.status).toBe(201);
    expect(r.body.domain.status).toBe('active');
    expect(r.body.domain.dns_configured).toBe(true);
    expect(r.body.domain.tunnel_configured).toBe(false);

    const rec = fake.dnsRecords.find((x) => x.name === 'www.example.com');
    expect(rec).toBeDefined();
    expect(rec!.content).toBe('ingress.example.net');
    expect(fake.tunnelIngress).toHaveLength(1); // catch-all only: no tunnel route

    // No ingress-sync task for direct mode (the worker is not involved).
    const tasks = await state.pool.query(`SELECT COUNT(*)::int AS n FROM tasks`);
    expect(tasks.rows[0].n).toBe(0);
  });
});

describe('GET /v1/domains', () => {
  it('lists lifecycle rows for a deployment, and fetches one by hostname', async () => {
    const { agent, depId } = await seedAll();
    await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });

    const list = await req('GET', `/v1/domains?deployment_id=${depId}`, agent.token);
    expect(list.status).toBe(200);
    expect(list.body.domains).toHaveLength(1);
    expect(list.body.domains[0]).toMatchObject({
      hostname: 'api.example.com', status: 'active', ingress: 'tunnel',
    });

    const one = await req('GET', '/v1/domains/api.example.com', agent.token);
    expect(one.status).toBe(200);
    expect(one.body.domain.hostname).toBe('api.example.com');
    expect(one.body.domain.tunnel_configured).toBe(true);

    const missing = await req('GET', '/v1/domains/nope.example.com', agent.token);
    expect(missing.status).toBe(404);
  });
});

describe('DELETE /v1/domains — clean removal', () => {
  it('deletes the DNS record and the tunnel route, verified gone, then removed', async () => {
    const { agent, depId } = await seedAll();
    await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(fake.dnsRecords).toHaveLength(1);

    const r = await req('DELETE', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com',
    });
    expect(r.status).toBe(200);
    expect(r.body.removed).toBe('api.example.com');
    expect(r.body.status).toBe('removed');

    // Verified gone on the (fake) authority: no DNS record, no tunnel rule.
    expect(fake.dnsRecords).toHaveLength(0);
    expect(fake.tunnelIngress.find((x) => x.hostname === 'api.example.com')).toBeUndefined();
    expect(fake.tunnelIngress[fake.tunnelIngress.length - 1].service).toBe('http_status:404');

    const row = await domainRow('api.example.com');
    expect(row.status).toBe('removed');
    expect(row.dns_configured).toBe(false);
    expect(row.tunnel_configured).toBe(false);

    // Mirror no longer lists it; events recorded.
    const dep = await state.pool.query(`SELECT domains FROM deployments WHERE id = $1`, [depId]);
    expect(dep.rows[0].domains).toHaveLength(0);
    expect(await eventTypes()).toContain('domain.removed');

    // Re-adding the same hostname afterwards works (partial unique index).
    const r2 = await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(r2.status).toBe(201);
    expect(r2.body.domain.status).toBe('active');
  });

  it('404s detaching a hostname that is not attached', async () => {
    const { agent, depId } = await seedAll();
    const r = await req('DELETE', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'ghost.example.com',
    });
    expect(r.status).toBe(404);
  });
});

describe('deployment death and recovery', () => {
  async function seedTask(type: string, deploymentId: string, hostId: string, agentId: string) {
    const id = randomUUID();
    await state.pool.query(
      `INSERT INTO tasks (id, created_by, assigned_to, type, status, payload)
       VALUES ($1, $2, $3, $4, 'claimed', $5)`,
      [id, agentId, hostId, type, JSON.stringify({ deployment_id: deploymentId })],
    );
    await state.pool.query(`UPDATE tasks SET claimed_by = $2 WHERE id = $1`, [id, hostId]);
    return id;
  }

  it('remove task -> stopped: domains fail with events, tunnel route dropped', async () => {
    const { agent, host, depId } = await seedAll();
    await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });
    expect(fake.tunnelIngress.find((x) => x.hostname === 'api.example.com')).toBeDefined();

    const taskId = await seedTask('remove', depId, host.id, agent.id);
    await req('POST', `/v1/worker/tasks/${taskId}/progress`, host.token, { status: 'running' });
    const r = await req('POST', `/v1/worker/tasks/${taskId}/progress`, host.token, { status: 'completed' });
    expect(r.status).toBe(200);

    const row = await domainRow('api.example.com');
    expect(row.status).toBe('failed');
    expect(row.error).toContain('stopped');

    // The dead route is gone from the authoritative remote config.
    expect(fake.tunnelIngress.find((x) => x.hostname === 'api.example.com')).toBeUndefined();

    const types = await eventTypes();
    expect(types).toContain('domain.failed');
    expect(types).toContain('service.removed');

    // The worker's local mirror no longer lists it either.
    const dom = await req('GET', '/v1/worker/domains', host.token);
    expect(dom.status).toBe(200);
    expect(dom.body.domains).toHaveLength(0);
  });

  it('start task -> running: failed domains re-provision back to active', async () => {
    const { agent, host, depId } = await seedAll();
    await req('POST', '/v1/domains', agent.token, {
      deployment_id: depId, hostname: 'api.example.com', ingress: 'tunnel',
    });

    // Kill the deployment (ports released with it, as in production).
    const killId = await seedTask('remove', depId, host.id, agent.id);
    await req('POST', `/v1/worker/tasks/${killId}/progress`, host.token, { status: 'running' });
    await req('POST', `/v1/worker/tasks/${killId}/progress`, host.token, { status: 'completed' });
    expect((await domainRow('api.example.com')).status).toBe('failed');

    // The worker reports fresh ports on the next deploy completion; seed the
    // fallback mapping the reprovisioner reads.
    await state.pool.query(`UPDATE deployments SET ports = '{"19001": 3000}'::jsonb WHERE id = $1`, [depId]);

    const startId = await seedTask('start', depId, host.id, agent.id);
    await req('POST', `/v1/worker/tasks/${startId}/progress`, host.token, { status: 'running' });
    const r = await req('POST', `/v1/worker/tasks/${startId}/progress`, host.token, { status: 'completed' });
    expect(r.status).toBe(200);

    const row = await domainRow('api.example.com');
    expect(row.status).toBe('active');
    expect(row.error).toBeNull();
    const rule = fake.tunnelIngress.find((x) => x.hostname === 'api.example.com');
    expect(rule).toBeDefined();
    expect(rule!.service).toBe('http://127.0.0.1:19001');
    expect((await eventTypes()).filter((t) => t === 'domain.active')).toHaveLength(2);
  });
});
