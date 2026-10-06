// W13b §51 — domain reconciliation: DB-vs-Cloudflare convergence.
//
// The Cloudflare API here is test/fakeCloudflare.ts — a TEST DOUBLE, not
// the real API (no Cloudflare token is available in this environment).
// An outage is simulated by pointing CLOUDFLARE_API_BASE at a dead port;
// out-of-band drift is simulated by writing to the fake's tunnel state
// directly over HTTP while the control plane looks elsewhere.
//
// Covered:
//   1. DB says active + CF API down -> the row is marked 'degraded', NOT
//      left claiming 'active'; when CF returns the row converges back to
//      'active' (retry/reconciliation until convergence).
//   2. Reconciliation policy for "CF route exists + DB says missing":
//      remote rules for hostnames the DB does not manage are preserved
//      verbatim (operator-managed); remote rules for managed-but-undesired
//      hostnames (failed domains) are dropped — no orphaned public routes.
//   3. requested -> failed is a legal transition (deployment-death hook no
//      longer 409s mid-loop on a still-'requested' row).
//   4. removeDomain refuses 'removed' when DNS state exists but the DNS
//      API is unconfigured (no orphaned CNAME).
//   5. setDomainStatus is optimistic: a stale from-state 409s instead of
//      double-applying a transition.
//   6. A 'degraded' row reprovisions back to 'active' via provisionDomain.
//   7. Direct-mode rows are untouched by tunnel verification.
import { tmpdir } from 'os';
import { mkdtempSync } from 'fs';
import { join } from 'path';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  failDomainsForDeployment,
  provisionDomain,
  removeDomain,
  setDomainStatus,
  verifyTunnelRoutes,
} from '../src/routes/domains';
import {
  readDomainReconcilerConfig,
  reconcileDomainsOnce,
  startDomainReconciler,
} from '../src/lib/domainReconciler';
import { startFakeCloudflare, type FakeCloudflare } from './fakeCloudflare';

const TUNNEL_ID = '11111111-2222-3333-4444-555555555555';
const TUNNEL_HOST = `${TUNNEL_ID}.cfargotunnel.com`;

let pool: any;
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

function setupDb() {
  const db = newDb();
  db.public.registerFunction({
    name: 'gen_random_uuid',
    returns: 'text',
    impure: true,
    implementation: () => randomUUID(),
  });
  db.public.none(`
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'online',
      token_hash TEXT NOT NULL,
      idempotency_key TEXT,
      created_at TIMESTAMPTZ DEFAULT now(),
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY,
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
    -- mirrors migrations 007 + 008 (degraded state in the CHECK)
    CREATE TABLE domains (
      id TEXT PRIMARY KEY DEFAULT gen_random_uuid(), hostname TEXT NOT NULL,
      idempotency_key TEXT,
      deployment_id TEXT NOT NULL, host_id TEXT NOT NULL,
      ingress TEXT NOT NULL DEFAULT 'tunnel'
        CHECK (ingress IN ('tunnel', 'direct')),
      status TEXT NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'configuring', 'active', 'degraded',
                          'failed', 'removing', 'removed')),
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
  pool = new pg.Pool();
}

async function seedDeployment(status = 'running') {
  const hostId = randomUUID();
  await pool.query(`INSERT INTO hosts (id, name, token_hash) VALUES ($1, 'h1', 'x')`, [hostId]);
  const depId = randomUUID();
  await pool.query(
    `INSERT INTO deployments (id, project_id, host_id, version, status)
     VALUES ($1, $2, $3, '1.0.0', $4)`,
    [depId, randomUUID(), hostId, status],
  );
  await pool.query(
    `INSERT INTO port_allocations (host_id, port, deployment_id) VALUES ($1, 18001, $2)`,
    [hostId, depId],
  );
  return { depId, hostId };
}

async function insertDomain(
  depId: string,
  hostId: string,
  hostname: string,
  ingress: 'tunnel' | 'direct',
  status: string,
): Promise<string> {
  const { rows } = await pool.query(
    `INSERT INTO domains (hostname, deployment_id, host_id, ingress, status)
     VALUES ($1, $2, $3, $4, $5) RETURNING id`,
    [hostname, depId, hostId, ingress, status],
  );
  return rows[0].id as string;
}

async function domainRow(id: string) {
  const { rows } = await pool.query(`SELECT * FROM domains WHERE id = $1`, [id]);
  return rows[0] as any;
}

function tunnelRule(hostname: string): string | undefined {
  return fake.tunnelIngress.find((r) => r.hostname === hostname)?.service;
}

/** Write the fake's tunnel state directly (out-of-band drift / operator rules). */
async function fakeSetTunnelIngress(ingress: Array<{ hostname?: string; service: string }>) {
  const res = await fetch(
    `${fake.url}/accounts/acct-1/cfd_tunnel/${TUNNEL_ID}/configurations`,
    {
      method: 'PUT',
      headers: { Authorization: 'Bearer test-tunnel-token', 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: { ingress } }),
    },
  );
  const json = (await res.json()) as any;
  expect(json.success).toBe(true);
}

beforeAll(async () => {
  process.env.LOG_DIR = mkdtempSync(join(tmpdir(), 'uaht-reconcile-logs-'));
  fake = await startFakeCloudflare();
  setupDb();
  setCfEnv(fake.url);
  // HTTPS reachability probes: canned 200. Every other fetch (the fake CF
  // API over http://) goes through for real.
  const realFetch = globalThis.fetch.bind(globalThis);
  vi.stubGlobal(
    'fetch',
    async (url: unknown, init?: RequestInit) => {
      const s = String(url);
      if (s.startsWith('https://')) {
        return { ok: true, status: 200, body: { cancel: async () => {} } };
      }
      return realFetch(s, init as any);
    },
  );
});

afterAll(async () => {
  vi.unstubAllGlobals();
  for (const k of Object.keys(CF_ENV)) delete process.env[k];
  delete process.env.CLOUDFLARE_API_BASE;
  await pool.end();
  await fake.close();
});

// Each test starts from an empty domains/events table and a pristine fake
// (the §51 test mutates the fake's tunnel state mid-test).
beforeEach(async () => {
  await pool.query('DELETE FROM domains');
  await pool.query('DELETE FROM events');
  fake.reset();
  setCfEnv(fake.url);
});

describe('§51 DB active + CF API down: no permanent false success, convergence on recovery', () => {
  it('marks the row degraded during the outage and heals it when CF returns', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'app1.example.com', 'tunnel', 'requested');
    const active = await provisionDomain(pool, id);
    expect(active.status).toBe('active');
    expect(tunnelRule('app1.example.com')).toBe('http://127.0.0.1:18001');

    // --- outage: CF API unreachable; the route also drifts out-of-band ---
    setCfEnv('http://127.0.0.1:9/client/v4');
    await fakeSetTunnelIngress([{ service: 'http_status:404' }]); // drift while blind
    const during = await reconcileDomainsOnce(pool);
    expect(during.tunnelReconciled).toBe(false);
    const degraded = await domainRow(id);
    expect(degraded.status).toBe('degraded'); // NOT 'active'
    expect(degraded.error).toMatch(/unreachable|unverifiable/i);
    const { rows: ev } = await pool.query(
      `SELECT type FROM events WHERE deployment_id = $1 ORDER BY id`,
      [depId],
    );
    expect(ev.map((r: any) => r.type)).toContain('domain.degraded');

    // --- recovery: the reconciler heals the table and the row converges ---
    setCfEnv(fake.url);
    const after = await reconcileDomainsOnce(pool);
    expect(after.tunnelReconciled).toBe(true);
    expect(after.verify.healed).toContain('app1.example.com');
    const healed = await domainRow(id);
    expect(healed.status).toBe('active');
    expect(healed.error).toBeNull();
    expect(tunnelRule('app1.example.com')).toBe('http://127.0.0.1:18001');
  });
});

describe('§51 reconciliation policy: remote rule exists + DB says missing', () => {
  it('preserves operator-managed rules verbatim, drops stale managed rules', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'app2.example.com', 'tunnel', 'requested');
    await provisionDomain(pool, id);
    // A failed row owns 'gone.example.com' in the DB but it must not route.
    await insertDomain(depId, hostId, 'gone.example.com', 'tunnel', 'failed');
    await fakeSetTunnelIngress([
      { hostname: 'ops.example.com', service: 'http://127.0.0.1:9999' }, // operator's
      { hostname: 'gone.example.com', service: 'http://127.0.0.1:9998' }, // stale, ours
      { hostname: 'app2.example.com', service: 'http://127.0.0.1:18001' },
      { service: 'http_status:404' },
    ]);

    const report = await reconcileDomainsOnce(pool);
    expect(report.tunnelReconciled).toBe(true);

    // Operator-managed: preserved verbatim, never adopted or dropped.
    expect(tunnelRule('ops.example.com')).toBe('http://127.0.0.1:9999');
    // Still-desired: untouched.
    expect(tunnelRule('app2.example.com')).toBe('http://127.0.0.1:18001');
    // Stale managed rule: dropped — no orphaned public route.
    expect(tunnelRule('gone.example.com')).toBeUndefined();
    // The failed row itself is not resurrected by the pass.
    const gone = await pool.query(`SELECT status FROM domains WHERE hostname = 'gone.example.com'`);
    expect(gone.rows[0].status).toBe('failed');
  });
});

describe('domain state machine fixes', () => {
  it('requested -> failed is legal: deployment death fails still-requested rows', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'new.example.com', 'tunnel', 'requested');
    // Before the fix this threw 409 (illegal transition) and aborted the
    // loop, leaving sibling rows live and routes in place.
    const n = await failDomainsForDeployment(pool, depId, 'deployment failed');
    expect(n).toBe(1);
    expect((await domainRow(id)).status).toBe('failed');
  });

  it('setDomainStatus is optimistic: stale from-state 409s, illegal transitions 409', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'app5.example.com', 'tunnel', 'requested');
    await provisionDomain(pool, id); // -> active
    const moved = await setDomainStatus(pool, id, 'active', 'removing');
    expect(moved.status).toBe('removing');
    // The row already moved on: a second transition from the stale state
    // cannot double-apply.
    await expect(setDomainStatus(pool, id, 'active', 'removing')).rejects.toMatchObject({
      status: 409,
    });
    // Illegal transition still rejected.
    await expect(setDomainStatus(pool, id, 'removing', 'active')).rejects.toMatchObject({
      status: 409,
    });
  });

  it('a degraded row reprovisions back to active', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'app6.example.com', 'tunnel', 'degraded');
    const after = await provisionDomain(pool, id); // degraded -> configuring -> active
    expect(after.status).toBe('active');
    expect(after.error).toBeNull();
    expect(tunnelRule('app6.example.com')).toBe('http://127.0.0.1:18001');
  });

  it('verifyTunnelRoutes leaves direct-mode rows alone', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'direct.example.com', 'direct', 'active');
    const res = await verifyTunnelRoutes(pool);
    expect(res.checked).toBe(0);
    expect((await domainRow(id)).status).toBe('active');
  });
});

describe('removal never orphans DNS', () => {
  it('refuses removed when DNS state exists but the DNS API is unconfigured', async () => {
    const { depId, hostId } = await seedDeployment();
    const id = await insertDomain(depId, hostId, 'app4.example.com', 'tunnel', 'requested');
    await provisionDomain(pool, id);
    expect((await domainRow(id)).dns_configured).toBe(true);

    // Drop only the DNS credentials; the tunnel API stays configured.
    delete process.env.CLOUDFLARE_API_TOKEN;
    delete process.env.CLOUDFLARE_ZONE_ID;
    try {
      await expect(removeDomain(pool, id, 'tester')).rejects.toMatchObject({ status: 502 });
      // The row stays 'removing' — the DB never claims the CNAME is gone.
      expect((await domainRow(id)).status).toBe('removing');
    } finally {
      process.env.CLOUDFLARE_API_TOKEN = 'test-dns-token';
      process.env.CLOUDFLARE_ZONE_ID = 'zone-1';
    }

    const removed = await removeDomain(pool, id, 'tester');
    expect(removed.status).toBe('removed');
    expect(tunnelRule('app4.example.com')).toBeUndefined();
    expect(fake.dnsRecords.some((r) => r.name === 'app4.example.com')).toBe(false);
  });
});

describe('domain reconciler driver', () => {
  it('reads config with a sane default and starts/stops cleanly', () => {
    expect(readDomainReconcilerConfig({} as any).intervalS).toBe(300);
    expect(readDomainReconcilerConfig({ DOMAIN_RECONCILE_INTERVAL_S: '45' } as any).intervalS).toBe(45);
    expect(readDomainReconcilerConfig({ DOMAIN_RECONCILE_INTERVAL_S: 'nope' } as any).intervalS).toBe(300);
    const stop = startDomainReconciler(pool, { intervalS: 3600 });
    expect(typeof stop).toBe('function');
    stop();
  });
});
