// Part 1L — persistent/atomic resource reservation tests (W1).
//
// Boots the REAL express app (createApp) against pg-mem — genuine
// route-level assertions on POST /v1/deployments plus unit tests for the
// scheduler's reservation accounting:
//
//   * reservations are snapshotted on the deployment row (reserved_cpu /
//     reserved_ram_mb, migration 013) inside the same transaction that
//     selects and locks the host;
//   * capacity is computed from persistent reservations over deployments in
//     resource-consuming states — never from cpu_pct/ram_pct utilization;
//   * terminal states (failed, rolled_back, rollback_failed, stopped)
//     release reservations; every non-terminal state consumes;
//   * draining exclusion uses the repo's single definition
//     (isHostSchedulable): hosts.status='draining' OR worker_draining=true;
//     draining never destroys existing apps or owned tasks;
//   * reservations live in PostgreSQL, so they survive control-plane
//     restarts.
//
// SCOPE NOTE (same honesty as concurrentDeploy.test.ts): pg-mem is
// single-threaded with no MVCC isolation — it cannot reproduce true
// multi-backend row-lock blocking. What IS honestly exercised here:
//   * the exact production SQL (FOR UPDATE + reservation SUM inside the
//     selecting transaction + reservation-carrying INSERT) under
//     overlapping callers;
//   * the mechanism: a second selector observes the first request's
//     reservation row and is rejected;
//   * on real PostgreSQL the FOR UPDATE row lock makes the second
//     transaction block until the first commits, so the SUM it then reads
//     always includes the rival reservation — exactly one winner.
import http from 'http';
import { randomUUID } from 'crypto';
import { readFileSync } from 'fs';
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
import {
  CONSUMING_DEPLOYMENT_STATUSES,
  deploymentConstraintsForProject,
  isHostSchedulable,
  normalizeResources,
  selectHostForDeployment,
} from '../src/lib/scheduler';

// ---------------------------------------------------------------------------
// Test database (mirrors the real schema where Part 1 lives, incl.
// migration 013 columns).
// ---------------------------------------------------------------------------
function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_uuid',
    args: [],
    returns: 'text',
    impure: true, // must be re-evaluated per row: pg-mem otherwise folds
    // the DEFAULT once per prepared plan and reuses it (real PostgreSQL
    // evaluates gen_random_uuid() per row)
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
      status TEXT NOT NULL DEFAULT 'offline',
      capabilities JSONB NOT NULL DEFAULT '[]',
      token_hash TEXT NOT NULL,
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      owner_agent_id TEXT,
      runtime TEXT NOT NULL DEFAULT 'docker',
      configuration JSONB NOT NULL DEFAULT '{}'
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT,
      project_id TEXT NOT NULL, host_id TEXT NOT NULL, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested', task_id TEXT,
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY DEFAULT gen_uuid(), type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      idempotency_key TEXT,
      created_by TEXT, assigned_to TEXT,
      payload JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT now()
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
  `);
  const pg = db.adapters.createPg();
  state.db = db;
  state.pool = new pg.Pool();
}

let base = '';
let server: http.Server | null = null;

async function startApp() {
  const app = createApp();
  server = http.createServer(app);
  await new Promise<void>((resolve) => server!.listen(0, resolve));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
}

async function stopApp() {
  if (server) {
    await new Promise<void>((resolve) => server!.close(() => resolve()));
    server = null;
  }
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

async function seedHost(
  name: string,
  opts: {
    status?: string;
    worker_draining?: boolean;
    capabilities?: string[];
    total_cpu?: number | null;
    total_ram_mb?: number | null;
  } = {},
) {
  const { hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, worker_draining,
                        capabilities, total_cpu, total_ram_mb)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
    [
      id,
      name,
      hash,
      opts.status ?? 'online',
      opts.worker_draining ?? false,
      JSON.stringify(opts.capabilities ?? ['docker']),
      opts.total_cpu ?? 4,
      opts.total_ram_mb ?? 16384,
    ],
  );
  return { id };
}

async function seedProject(
  name: string,
  ownerAgentId: string,
  opts: { runtime?: string; configuration?: Record<string, unknown> } = {},
) {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO projects (id, name, owner_agent_id, runtime, configuration)
     VALUES ($1,$2,$3,$4,$5)`,
    [id, name, ownerAgentId, opts.runtime ?? 'docker', JSON.stringify(opts.configuration ?? {})],
  );
  return id;
}

async function wipe() {
  for (const t of [
    'events',
    'port_allocations',
    'tasks',
    'deployments',
    'agent_host_access',
    'project_members',
    'hosts',
    'agents',
    'projects',
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

beforeAll(async () => {
  process.env.RATE_LIMIT_UNAUTH_PER_MIN = '10000';
  setupDb();
  await startApp();
});

afterAll(async () => {
  await stopApp();
});

beforeEach(wipe);

const AGENT_PERMS = { deploy: true, read_status: true };
const BIG = { resources: { cpu: 3, memory: '8g' } }; // 3 CPU / 8GB

// ---------------------------------------------------------------------------
// Part 1B: the single authoritative parser
// ---------------------------------------------------------------------------
describe('Part 1B — normalizeResources (single parsing rule)', () => {
  it("parses {'cpu':1.5,'memory':'512Mi'} -> {cpu:1.5, ram_mb:512}", () => {
    expect(normalizeResources({ cpu: 1.5, memory: '512Mi' })).toEqual({
      cpu: 1.5,
      ram_mb: 512,
    });
  });

  it('parses every accepted memory suffix', () => {
    expect(normalizeResources({ memory: '256m' }).ram_mb).toBe(256);
    expect(normalizeResources({ memory: '1g' }).ram_mb).toBe(1024);
    expect(normalizeResources({ memory: '2G' }).ram_mb).toBe(2048);
    expect(normalizeResources({ memory: '1Gi' }).ram_mb).toBe(1024);
    expect(normalizeResources({ memory: '512MI' }).ram_mb).toBe(512);
  });

  it('normalizes malformed/absent fields to null and never throws', () => {
    expect(normalizeResources({ cpu: 'x', memory: '512MB' })).toEqual({
      cpu: null,
      ram_mb: null,
    });
    expect(normalizeResources({ cpu: -1, memory: '10x' })).toEqual({
      cpu: null,
      ram_mb: null,
    });
    expect(normalizeResources({ cpu: NaN, memory: 512 })).toEqual({
      cpu: null,
      ram_mb: null,
    });
    expect(normalizeResources(null)).toEqual({ cpu: null, ram_mb: null });
    expect(normalizeResources('nope')).toEqual({ cpu: null, ram_mb: null });
  });

  it('deploymentConstraintsForProject derives constraints via the unified parser', () => {
    expect(
      deploymentConstraintsForProject({
        runtime: 'docker',
        configuration: { resources: { cpu: 1.5, memory: '512Mi' } },
      }),
    ).toEqual({
      requiredCapabilities: ['docker'],
      requiredCpuCores: 1.5,
      requiredRamMb: 512,
    });
    // No resources -> no requirements (NULL reservation columns).
    expect(deploymentConstraintsForProject({ runtime: 'docker', configuration: {} })).toEqual({
      requiredCapabilities: ['docker'],
      requiredCpuCores: null,
      requiredRamMb: null,
    });
  });
});

// ---------------------------------------------------------------------------
// Part 1A: consuming vs terminal states (the repo's ACTUAL state names)
// ---------------------------------------------------------------------------
describe('Part 1A — consuming states hold reservations; terminal states release', () => {
  it('every non-terminal deployment state consumes capacity', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const host = await seedHost('h1');
    // One reservation-holder per consuming state, seeded directly.
    for (const s of CONSUMING_DEPLOYMENT_STATUSES) {
      await state.pool.query(
        `INSERT INTO deployments (id, project_id, host_id, version, status, reserved_cpu, reserved_ram_mb)
         VALUES ($1,$2,$3,$4,$5,3,8192)`,
        [randomUUID(), randomUUID(), host.id, `v-${s}`, s],
      );
    }
    const projectId = await seedProject('p1', agent.id, { configuration: BIG });
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '2.0.0',
    });
    // 7 x 3 CPU reserved on a 4-CPU host: nothing fits.
    expect(status).toBe(503);
    expect(body.error.code).toBe('no_capacity');
  });

  it.each(['failed', 'rolled_back', 'rollback_failed', 'stopped'])(
    "terminal state '%s' releases the reservation",
    async (terminal) => {
      const agent = await seedAgent('a1', AGENT_PERMS);
      const host = await seedHost('h1');
      const p1 = await seedProject('p1', agent.id, { configuration: BIG });
      const first = await req('POST', '/v1/deployments', agent.token, {
        project_id: p1,
        version: '1.0.0',
      });
      expect(first.status).toBe(201);
      // The full host is now reserved: a second 3CPU/8GB deploy is rejected.
      const p2 = await seedProject('p2', agent.id, { configuration: BIG });
      const blocked = await req('POST', '/v1/deployments', agent.token, {
        project_id: p2,
        version: '1.0.0',
      });
      expect(blocked.status).toBe(503);
      // The first deployment reaches a terminal state -> reservation freed.
      await state.pool.query('UPDATE deployments SET status = $1 WHERE id = $2', [
        terminal,
        first.body.deployment.id,
      ]);
      const ok = await req('POST', '/v1/deployments', agent.token, {
        project_id: p2,
        version: '1.0.0',
      });
      expect(ok.status).toBe(201);
      expect(ok.body.deployment.reserved_cpu).toBe(3);
      expect(ok.body.deployment.reserved_ram_mb).toBe(8192);
    },
  );
});

// ---------------------------------------------------------------------------
// Part 1C/1D: persistent reservation on the deployment row, same transaction
// ---------------------------------------------------------------------------
describe('Part 1C/1D — atomic check-and-reserve with persistent reservations', () => {
  it('snapshots the normalized reservation on the deployment row', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('h1');
    const projectId = await seedProject('p1', agent.id, {
      configuration: { resources: { cpu: 1.5, memory: '512Mi' } },
    });
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(201);
    expect(body.deployment.reserved_cpu).toBe(1.5);
    expect(body.deployment.reserved_ram_mb).toBe(512);
    const { rows } = await state.pool.query(
      'SELECT reserved_cpu, reserved_ram_mb FROM deployments WHERE id = $1',
      [body.deployment.id],
    );
    expect(rows[0].reserved_cpu).toBe(1.5);
    expect(rows[0].reserved_ram_mb).toBe(512);
  });

  it('stores NULL reservations when the project declares no resources', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('h1');
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(201);
    expect(body.deployment.reserved_cpu).toBeNull();
    expect(body.deployment.reserved_ram_mb).toBeNull();
  });

  it('rejects a deployment that would over-commit persistent reservations', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('h1'); // 4 CPU / 16GB
    const p1 = await seedProject('p1', agent.id, { configuration: BIG });
    const first = await req('POST', '/v1/deployments', agent.token, {
      project_id: p1,
      version: '1.0.0',
    });
    expect(first.status).toBe(201);
    // 3 of 4 CPUs and 8 of 16GB are now reserved: another 3CPU/8GB cannot fit.
    const p2 = await seedProject('p2', agent.id, { configuration: BIG });
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: p2,
      version: '1.0.0',
    });
    expect(status).toBe(503);
    expect(body.error.code).toBe('no_capacity');
    // The rejected request reserved nothing: exactly one deployment exists.
    const { rows } = await state.pool.query('SELECT count(*)::int AS n FROM deployments');
    expect(rows[0].n).toBe(1);
  });

  it('capacity is reservation-based, not utilization-based', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    // Host is 99% "utilized" but has zero reservations: a fitting deploy is
    // admitted — utilization snapshots do not gate scheduling.
    const host = await seedHost('busy');
    await state.pool.query('UPDATE hosts SET cpu_pct = 99, ram_pct = 99 WHERE id = $1', [host.id]);
    const p1 = await seedProject('p1', agent.id, {
      configuration: { resources: { cpu: 1, memory: '1g' } },
    });
    const ok = await req('POST', '/v1/deployments', agent.token, {
      project_id: p1,
      version: '1.0.0',
    });
    expect(ok.status).toBe(201);
    // Seed 3.5 CPUs of persistent reservation directly: another 1-CPU
    // request no longer fits even though utilization is unchanged.
    await state.pool.query(
      `INSERT INTO deployments (id, project_id, host_id, version, status, reserved_cpu, reserved_ram_mb)
       VALUES ($1,$2,$3,'9.9.9','running',3.5,1024)`,
      [randomUUID(), randomUUID(), host.id],
    );
    const p2 = await seedProject('p2', agent.id, {
      configuration: { resources: { cpu: 1, memory: '1g' } },
    });
    const blocked = await req('POST', '/v1/deployments', agent.token, {
      project_id: p2,
      version: '1.0.0',
    });
    expect(blocked.status).toBe(503);
    expect(blocked.body.error.code).toBe('no_capacity');
  });

  it('the reservation SUM runs inside the locked selecting transaction', async () => {
    const seen: string[] = [];
    const fakeClient = {
      query: vi.fn(async (text: string) => {
        seen.push(String(text));
        if (/agent_host_access/.test(text)) return { rows: [] };
        if (/SUM\(reserved_cpu\)/.test(text)) return { rows: [{ cpu: 3.5, ram_mb: 1024 }] };
        return {
          rows: [
            {
              id: 'h1',
              status: 'online',
              worker_draining: false,
              capabilities: ['docker'],
              total_cpu: 4,
              total_ram_mb: 16384,
            },
          ],
        };
      }),
    };
    // 3.5 of 4 CPUs already reserved: a 1-CPU request is ineligible.
    const picked = await selectHostForDeployment(fakeClient as never, {
      agentId: 'a1',
      requiredCapabilities: ['docker'],
      requiredCpuCores: 1,
      requiredRamMb: 1024,
    });
    expect(picked).toBeNull();
    expect(seen.some((sql) => /FOR UPDATE/.test(sql))).toBe(true);
    expect(seen.some((sql) => /SUM\(reserved_cpu\)/.test(sql))).toBe(true);
    expect(seen.some((sql) => /reserved_ram_mb/.test(sql))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Part 1E: concurrency — two simultaneous 3CPU/8GB requests, one winner
// ---------------------------------------------------------------------------
describe('Part 1E — concurrent requests serialize on the reservation', () => {
  it('two overlapping 3CPU/8GB requests on a 4CPU/16GB host -> exactly one wins', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('h1'); // 4 CPU / 16GB
    const p1 = await seedProject('p1', agent.id, { configuration: BIG });
    const p2 = await seedProject('p2', agent.id, { configuration: BIG });

    // Fire the first request; wait until its reservation row is visible,
    // then fire the second while the first is still in flight. On real
    // PostgreSQL the second blocks on the FOR UPDATE host-row lock until
    // the first commits, then its reservation SUM includes the first
    // request's row -> 503 no_capacity. Exactly one deployment exists.
    const firstFlight = req('POST', '/v1/deployments', agent.token, {
      project_id: p1,
      version: '1.0.0',
    });
    let visible = false;
    for (let i = 0; i < 100 && !visible; i++) {
      const { rows } = await state.pool.query(
        'SELECT count(*)::int AS n FROM deployments WHERE project_id = $1',
        [p1],
      );
      visible = rows[0].n === 1;
      if (!visible) await new Promise((r) => setTimeout(r, 25));
    }
    expect(visible).toBe(true);
    const second = await req('POST', '/v1/deployments', agent.token, {
      project_id: p2,
      version: '1.0.0',
    });
    const first = await firstFlight;

    const statuses = [first.status, second.status].sort();
    expect(statuses).toEqual([201, 503]);
    const loser = first.status === 503 ? first : second;
    expect(loser.body.error.code).toBe('no_capacity');
    const { rows } = await state.pool.query('SELECT count(*)::int AS n FROM deployments');
    expect(rows[0].n).toBe(1);
  });

  it('a second selector observes the first transaction\'s reservation row', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const host = await seedHost('h1');
    const c1 = await state.pool.connect();
    const c2 = await state.pool.connect();
    try {
      await c1.query('BEGIN');
      const pick1 = await selectHostForDeployment(c1, {
        agentId: agent.id,
        requiredCapabilities: ['docker'],
        requiredCpuCores: 3,
        requiredRamMb: 8192,
      });
      expect(pick1).toBe(host.id);
      // First transaction reserves but has not committed yet.
      await c1.query(
        `INSERT INTO deployments (id, project_id, host_id, version, status, reserved_cpu, reserved_ram_mb)
         VALUES ($1,$2,$3,'1.0.0','requested',3,8192)`,
        [randomUUID(), randomUUID(), host.id],
      );
      await c2.query('BEGIN');
      // The rival's reservation is accounted for: no host can fit 3CPU/8GB.
      const pick2 = await selectHostForDeployment(c2, {
        agentId: agent.id,
        requiredCapabilities: ['docker'],
        requiredCpuCores: 3,
        requiredRamMb: 8192,
      });
      expect(pick2).toBeNull();
      await c2.query('ROLLBACK');
      await c1.query('ROLLBACK');
    } finally {
      c1.release();
      c2.release();
    }
  });
});

// ---------------------------------------------------------------------------
// Part 1F/1G: draining exclusion via the single isHostSchedulable helper
// ---------------------------------------------------------------------------
describe('Part 1F/1G — draining exclusion', () => {
  it('isHostSchedulable implements the repo\'s single draining definition', () => {
    expect(isHostSchedulable({ status: 'online', worker_draining: false })).toBe(true);
    expect(isHostSchedulable({ status: 'online' })).toBe(true);
    expect(isHostSchedulable({ status: 'draining', worker_draining: false })).toBe(false);
    expect(isHostSchedulable({ status: 'online', worker_draining: true })).toBe(false);
    expect(isHostSchedulable({ status: 'draining', worker_draining: true })).toBe(false);
  });

  it('auto-select skips status=draining and worker_draining hosts', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('drained', { status: 'draining' });
    await seedHost('self-drained', { status: 'online', worker_draining: true });
    const healthy = await seedHost('healthy');
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(201);
    expect(body.deployment.host_id).toBe(healthy.id);
    // With only draining hosts left, the request fails fast — never a
    // silent deploy onto a draining host.
    await state.pool.query("UPDATE hosts SET status = 'draining' WHERE name = 'healthy'");
    const p2 = await seedProject('p2', agent.id);
    const blocked = await req('POST', '/v1/deployments', agent.token, {
      project_id: p2,
      version: '1.0.0',
    });
    expect(blocked.status).toBe(503);
    expect(blocked.body.error.code).toBe('no_capacity');
  });

  it('explicit host_id on a draining host is rejected; existing apps/tasks are untouched', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const host = await seedHost('h1');
    const projectId = await seedProject('p1', agent.id);
    const first = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      host_id: host.id,
    });
    expect(first.status).toBe(201);
    const depId = first.body.deployment.id;
    const taskId = first.body.task.id;

    await state.pool.query("UPDATE hosts SET status = 'draining' WHERE id = $1", [host.id]);
    const rejected = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '2.0.0',
      host_id: host.id,
    });
    expect(rejected.status).toBe(503);
    expect(rejected.body.error.code).toBe('no_capacity');

    // Draining destroys nothing: the existing deployment and its owned
    // task are exactly as they were.
    const { rows: deps } = await state.pool.query('SELECT * FROM deployments WHERE id = $1', [
      depId,
    ]);
    expect(deps).toHaveLength(1);
    expect(deps[0].status).toBe('requested');
    const { rows: tasks } = await state.pool.query('SELECT * FROM tasks WHERE id = $1', [taskId]);
    expect(tasks).toHaveLength(1);
    expect(tasks[0].status).toBe('queued');
  });

  it('explicit host_id on a worker_draining host is rejected', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const host = await seedHost('h1', { worker_draining: true });
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      host_id: host.id,
    });
    expect(status).toBe(503);
    expect(body.error.code).toBe('no_capacity');
  });
});

// ---------------------------------------------------------------------------
// Part 1K: reservations survive a control-plane restart (they live in PG)
// ---------------------------------------------------------------------------
describe('Part 1K — reservations survive control-plane restart', () => {
  it('a fresh app instance on the same database still enforces reservations', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('h1');
    const p1 = await seedProject('p1', agent.id, { configuration: BIG });
    const first = await req('POST', '/v1/deployments', agent.token, {
      project_id: p1,
      version: '1.0.0',
    });
    expect(first.status).toBe(201);

    // "Restart" the control plane: brand-new express app, same PostgreSQL.
    await stopApp();
    await startApp();

    const p2 = await seedProject('p2', agent.id, { configuration: BIG });
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: p2,
      version: '1.0.0',
    });
    expect(status).toBe(503);
    expect(body.error.code).toBe('no_capacity');
    // The reservation row itself survived the restart untouched.
    const { rows } = await state.pool.query(
      'SELECT reserved_cpu, reserved_ram_mb, status FROM deployments WHERE id = $1',
      [first.body.deployment.id],
    );
    expect(rows[0].reserved_cpu).toBe(3);
    expect(rows[0].reserved_ram_mb).toBe(8192);
    expect(rows[0].status).toBe('requested');
  });
});

// ---------------------------------------------------------------------------
// Part 1C: migration 013
// ---------------------------------------------------------------------------
describe('Part 1C — migration 013 adds the reservation columns', () => {
  it('013_reserved_resources.sql applies cleanly and adds reserved_cpu/reserved_ram_mb', async () => {
    const sql = readFileSync(
      join(__dirname, '..', '..', '..', 'database', 'migrations', '013_reserved_resources.sql'),
      'utf8',
    );
    // The migration only ADDs columns — it never edits historical schema.
    expect(sql).toMatch(/ADD COLUMN IF NOT EXISTS reserved_cpu/);
    expect(sql).toMatch(/ADD COLUMN IF NOT EXISTS reserved_ram_mb/);
    const db = newDb();
    db.public.none('CREATE TABLE deployments (id TEXT PRIMARY KEY, status TEXT)');
    db.public.none(sql);
    // Re-running is a safe no-op (IF NOT EXISTS).
    db.public.none(sql);
    const cols = db.public.many(
      `SELECT column_name FROM information_schema.columns
       WHERE table_name = 'deployments' AND column_name IN ('reserved_cpu','reserved_ram_mb')`,
    );
    expect(cols.map((r: any) => r.column_name).sort()).toEqual([
      'reserved_cpu',
      'reserved_ram_mb',
    ]);
  });
});
