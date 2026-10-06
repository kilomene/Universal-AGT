// §8 deployment host_id contract + §9 atomic check-and-reserve.
//
// Boots the REAL express app (createApp) against pg-mem — genuine
// route-level assertions on POST /v1/deployments:
//   * host_id supplied  -> the requested host is used (concrete host_id row)
//   * host_id omitted    -> the scheduler picks an eligible host
//   * no eligible host   -> 503 no_capacity (never a silent arbitrary deploy)
//   * capability / resource / host-ACL mismatches make a host ineligible
// Plus unit tests for the scheduler constraint derivation and the
// FOR UPDATE locking mechanism (§9: PostgreSQL row locks, no in-memory
// locking).
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
import {
  deploymentConstraintsForProject,
  selectHostForDeployment,
} from '../src/lib/scheduler';

// ---------------------------------------------------------------------------
// Test database
// ---------------------------------------------------------------------------
function setupDb() {
  const db = newDb();
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
      cpu_pct DOUBLE PRECISION, ram_pct DOUBLE PRECISION, disk_pct DOUBLE PRECISION,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION, total_disk_gb DOUBLE PRECISION,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      owner TEXT, owner_agent_id TEXT, repository TEXT,
      runtime TEXT NOT NULL DEFAULT 'docker',
      configuration JSONB NOT NULL DEFAULT '{}'
    );
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY, idempotency_key TEXT,
      project_id TEXT NOT NULL, host_id TEXT NOT NULL, version TEXT,
      artifact_id TEXT, mode TEXT NOT NULL DEFAULT 'automatic',
      status TEXT NOT NULL DEFAULT 'requested', task_id TEXT
    );
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY DEFAULT 't', type TEXT NOT NULL,
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
let server: http.Server;

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
    capabilities?: string[];
    total_cpu?: number;
    total_ram_mb?: number;
    cpu_pct?: number;
    ram_pct?: number;
  } = {},
) {
  const { hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, capabilities,
                        total_cpu, total_ram_mb, cpu_pct, ram_pct)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
    [
      id,
      name,
      hash,
      opts.status ?? 'online',
      JSON.stringify(opts.capabilities ?? ['docker']),
      opts.total_cpu ?? 4,
      opts.total_ram_mb ?? 8192,
      opts.cpu_pct ?? 10,
      opts.ram_pct ?? 10,
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
  const app = createApp();
  server = http.createServer(app);
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

beforeEach(wipe);

const AGENT_PERMS = { deploy: true, read_status: true };

// ---------------------------------------------------------------------------
// §8: the host_id contract
// ---------------------------------------------------------------------------
describe('§8 deployment host_id contract', () => {
  it('uses the explicitly requested host when host_id is supplied', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const h1 = await seedHost('h1');
    const h2 = await seedHost('h2');
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      host_id: h2.id,
    });
    expect(status).toBe(201);
    expect(body.deployment.host_id).toBe(h2.id);
    expect(body.deployment.host_id).not.toBe(h1.id);
    expect(body.task.assigned_to).toBe(h2.id);
    expect(body.task.payload.host_id).toBe(h2.id);
  });

  it('selects an eligible host when host_id is omitted — the row always has a concrete host', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('drained', { status: 'draining' });
    await seedHost('offline', { status: 'offline' });
    const online = await seedHost('online-1');
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(201);
    expect(body.deployment.host_id).toBe(online.id);
    expect(body.deployment.host_id).toBeTruthy();
    expect(body.task.assigned_to).toBe(online.id);
  });

  it('returns 503 no_capacity when no host is eligible — never a silent arbitrary deploy', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('drained', { status: 'draining' });
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(503);
    expect(body.error.code).toBe('no_capacity');
    // And nothing was reserved: no deployment row, no task row.
    const deps = await state.pool.query('SELECT count(*)::int AS n FROM deployments');
    const tasks = await state.pool.query('SELECT count(*)::int AS n FROM tasks');
    expect(deps.rows[0].n).toBe(0);
    expect(tasks.rows[0].n).toBe(0);
  });

  it('treats a host missing a required capability as ineligible', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('docker-only', { capabilities: ['docker'] });
    const projectId = await seedProject('p1', agent.id, { runtime: 'docker-compose' });
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(503);
    expect(body.error.code).toBe('no_capacity');
  });

  it('treats a host without enough free resources as ineligible', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost('small', { total_ram_mb: 512, total_cpu: 4 });
    const projectId = await seedProject('p1', agent.id, {
      configuration: { resources: { memory: '1g', cpu: 1 } },
    });
    const blocked = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(blocked.status).toBe(503);

    // Give the host enough RAM and the same request succeeds.
    await state.pool.query(`UPDATE hosts SET total_ram_mb = 4096 WHERE name = 'small'`);
    const ok = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.1',
    });
    expect(ok.status).toBe(201);
    expect(ok.body.deployment.host_id).toBeTruthy();
  });

  it('skips hosts the agent may not target (strict host ACL) during auto-select', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const other = await seedAgent('a2', AGENT_PERMS);
    const restricted = await seedHost('restricted');
    const open = await seedHost('open');
    // restricted enters strict mode, granted to `other` only.
    await state.pool.query(
      'INSERT INTO agent_host_access (agent_id, host_id) VALUES ($1,$2)',
      [other.id, restricted.id],
    );
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(status).toBe(201);
    expect(body.deployment.host_id).toBe(open.id);
  });

  it('rejects an explicitly requested host the agent may not target', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const other = await seedAgent('a2', AGENT_PERMS);
    const restricted = await seedHost('restricted');
    await state.pool.query(
      'INSERT INTO agent_host_access (agent_id, host_id) VALUES ($1,$2)',
      [other.id, restricted.id],
    );
    const projectId = await seedProject('p1', agent.id);
    const { status, body } = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      host_id: restricted.id,
    });
    expect(status).toBe(403);
    expect(body.error.code).toBe('forbidden');
  });
});

// ---------------------------------------------------------------------------
// §9: atomic check-and-reserve
// ---------------------------------------------------------------------------
describe('§9 atomic host check-and-reserve', () => {
  it('locks candidate host rows FOR UPDATE inside the caller transaction', async () => {
    const seen: string[] = [];
    const fakeClient = {
      query: vi.fn(async (text: string) => {
        seen.push(text);
        return {
          rows: [
            {
              id: 'h1',
              capabilities: ['docker'],
              total_cpu: 4,
              total_ram_mb: 8192,
              cpu_pct: 10,
              ram_pct: 10,
            },
          ],
        };
      }),
    };
    // The fake client reports no ACL rows -> the host is legacy-open.
    (fakeClient.query as any).mockImplementation(async (text: string) => {
      seen.push(text);
      if (/agent_host_access/.test(text)) return { rows: [] };
      return {
        rows: [
          {
            id: 'h1',
            capabilities: ['docker'],
            total_cpu: 4,
            total_ram_mb: 8192,
            cpu_pct: 10,
            ram_pct: 10,
          },
        ],
      };
    });
    const picked = await selectHostForDeployment(fakeClient as never, {
      agentId: 'a1',
      requiredCapabilities: ['docker'],
      requiredCpuCores: null,
      requiredRamMb: null,
    });
    expect(picked).toBe('h1');
    expect(seen.some((sql) => /FOR UPDATE/.test(sql))).toBe(true);
    // Eligibility is evaluated on locked rows: online only, ACL-aware.
    expect(seen.some((sql) => /status = 'online'/.test(sql))).toBe(true);
    expect(seen.some((sql) => /agent_host_access/.test(sql))).toBe(true);
  });

  it('returns null when no candidate meets the constraints', async () => {
    const fakeClient = {
      query: vi.fn(async () => ({
        rows: [
          {
            id: 'h1',
            capabilities: ['docker'],
            total_cpu: 1,
            total_ram_mb: 512,
            cpu_pct: 90,
            ram_pct: 90,
          },
        ],
      })),
    };
    const picked = await selectHostForDeployment(fakeClient as never, {
      agentId: 'a1',
      requiredCapabilities: ['gpu'],
      requiredCpuCores: 4,
      requiredRamMb: 8192,
    });
    expect(picked).toBeNull();
  });
});

describe('deploymentConstraintsForProject', () => {
  it('derives capabilities and resources from the project configuration', () => {
    expect(
      deploymentConstraintsForProject({
        runtime: 'docker',
        configuration: { resources: { memory: '512m', cpu: 1.5 }, capabilities: ['gpu'] },
      }),
    ).toEqual({
      requiredCapabilities: ['docker', 'gpu'],
      requiredCpuCores: 1.5,
      requiredRamMb: 512,
    });
    expect(
      deploymentConstraintsForProject({
        runtime: 'docker-compose',
        configuration: { resources: { memory: '2g' } },
      }),
    ).toEqual({
      requiredCapabilities: ['docker-compose'],
      requiredCpuCores: null,
      requiredRamMb: 2048,
    });
    expect(deploymentConstraintsForProject({ runtime: 'static', configuration: {} })).toEqual({
      requiredCapabilities: [],
      requiredCpuCores: null,
      requiredRamMb: null,
    });
  });
});
