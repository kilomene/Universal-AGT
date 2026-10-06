// Fix #1 — the resource reservation must come from the artifact's validated
// agent.deploy.json manifest (the exact definition the worker deploys), not
// from the possibly-stale projects.configuration.
//
// Boots the REAL express app (createApp) against pg-mem — genuine
// route-level assertions:
//
//   * POST /v1/artifacts/init accepts, validates (shared grammar) and
//     stores the manifest; invalid manifests are rejected (422).
//   * POST /v1/deployments reserves from the artifact manifest when the
//     artifact carries one — even when it differs from the project
//     configuration (the scheduler must NOT reserve 1 CPU / 512Mi while
//     the worker launches 4 CPU / 8Gi).
//   * Legacy artifacts (manifest NULL) and artifact-less deployments fall
//     back to projects.configuration — previous behavior preserved.
//   * The reserved contract travels on the task payload for worker-side
//     enforcement.
//   * Unit tests: validateManifest (strict, worker-parity) and
//     deploymentConstraintsForManifest, incl. the unified memory grammar.
import http from 'http';
import { createHash, randomUUID } from 'crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import * as tar from 'tar';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { generateAgentKey, generateHostToken } from '../src/lib/tokens';
import { validateManifest } from '../src/lib/manifest';
import {
  deploymentConstraintsForManifest,
  normalizeResources,
} from '../src/lib/scheduler';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_uuid',
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
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
      filename TEXT NOT NULL, storage_path TEXT NOT NULL,
      checksum TEXT NOT NULL, size BIGINT NOT NULL,
      version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      manifest JSONB,
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

async function seedHost() {
  const { hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status, worker_draining,
                        capabilities, total_cpu, total_ram_mb)
     VALUES ($1,'h1',$2,'online',false,'["docker"]',4,16384)`,
    [id, hash],
  );
  return { id };
}

async function seedProject(name: string, ownerAgentId: string, configuration: Record<string, unknown>) {
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO projects (id, name, owner_agent_id, runtime, configuration)
     VALUES ($1,$2,$3,'docker',$4)`,
    [id, name, ownerAgentId, JSON.stringify(configuration)],
  );
  return id;
}

async function seedArtifact(
  projectId: string,
  manifest: Record<string, unknown> | null,
  status = 'ready',
) {
  const id = randomUUID();
  const checksum = `sha256:${'a'.repeat(64)}`;
  await state.pool.query(
    `INSERT INTO artifacts (id, project_id, filename, storage_path, checksum, size, version, status, manifest)
     VALUES ($1,$2,'app.tar.gz',$1,$3,100,'1.0.0',$4,$5)`,
    [id, projectId, checksum, status, manifest ? JSON.stringify(manifest) : null],
  );
  return id;
}

async function wipe() {
  for (const t of [
    'events', 'port_allocations', 'tasks', 'deployments', 'artifacts',
    'agent_host_access', 'project_members', 'hosts', 'agents', 'projects',
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
  process.env.ARTIFACT_DIR = mkdtempSync(join(tmpdir(), 'uaht-manifest-artifacts-'));
  setupDb();
  await startApp();
});

afterAll(async () => {
  await stopApp();
});

beforeEach(wipe);

const AGENT_PERMS = { deploy: true, read_status: true };

function manifest(resources: Record<string, unknown>, runtime = 'docker') {
  return {
    name: 'p1',
    runtime,
    service: { port: 3000, healthcheck: '/health' },
    resources,
    restart: 'unless-stopped',
  };
}

// ---------------------------------------------------------------------------
// Artifact init: manifest accepted, validated, stored
// ---------------------------------------------------------------------------
describe('POST /v1/artifacts/init — manifest handling', () => {
  it('stores a valid manifest on the artifact row', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const m = manifest({ cpu: 4, memory: '8Gi' });
    const res = await req('POST', '/v1/artifacts/init', agent.token, {
      project_id: projectId,
      filename: 'app.tar.gz',
      size: 100,
      checksum: `sha256:${'a'.repeat(64)}`,
      version: '1.0.0',
      manifest: m,
    });
    expect(res.status).toBe(201);
    expect(res.body.artifact.manifest).toEqual(m);
  });

  it('rejects an invalid manifest (422) and creates nothing', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const res = await req('POST', '/v1/artifacts/init', agent.token, {
      project_id: projectId,
      filename: 'app.tar.gz',
      size: 100,
      checksum: `sha256:${'a'.repeat(64)}`,
      version: '1.0.0',
      manifest: { name: 'p1', runtime: 'docker', resources: { memory: '512MB' } },
    });
    expect(res.status).toBe(422);
    const { rows } = await state.pool.query('SELECT COUNT(*)::int AS c FROM artifacts');
    expect(rows[0].c).toBe(0);
  });

  it('rejects a manifest with volumes (explicitly unsupported)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const res = await req('POST', '/v1/artifacts/init', agent.token, {
      project_id: projectId,
      filename: 'app.tar.gz',
      size: 100,
      checksum: `sha256:${'a'.repeat(64)}`,
      version: '1.0.0',
      manifest: { ...manifest({ cpu: 1 }), volumes: ['/data'] },
    });
    expect(res.status).toBe(422);
  });

  it('omitted manifest stays NULL (legacy behavior preserved)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const res = await req('POST', '/v1/artifacts/init', agent.token, {
      project_id: projectId,
      filename: 'app.tar.gz',
      size: 100,
      checksum: `sha256:${'a'.repeat(64)}`,
      version: '1.0.0',
    });
    expect(res.status).toBe(201);
    expect(res.body.artifact.manifest).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Scheduler reserves from the artifact manifest, not project configuration
// ---------------------------------------------------------------------------
describe('POST /v1/deployments — artifact manifest is the reservation source', () => {
  it('reserves the artifact manifest resources when they differ from project config', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost();
    // Project config says 1 CPU / 512Mi ...
    const projectId = await seedProject('p1', agent.id, {
      resources: { cpu: 1, memory: '512Mi' },
    });
    // ... but the artifact manifest (what the worker deploys) says 4 CPU / 8Gi.
    const artifactId = await seedArtifact(projectId, manifest({ cpu: 4, memory: '8Gi' }));
    const res = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      artifact_id: artifactId,
    });
    expect(res.status).toBe(201);
    // The reservation MUST be the artifact's 4 CPU / 8192 MiB — reserving
    // 1 CPU / 512Mi here while the worker launches 4/8Gi would corrupt
    // the persistent accounting.
    expect(res.body.deployment.reserved_cpu).toBe(4);
    expect(res.body.deployment.reserved_ram_mb).toBe(8192);
    // The canonical contract travels on the task payload for worker
    // enforcement (tarball <= reservation, fail loudly otherwise).
    const { rows } = await state.pool.query('SELECT payload FROM tasks WHERE id = $1', [
      res.body.task.id,
    ]);
    expect(rows[0].payload.reserved_cpu).toBe(4);
    expect(rows[0].payload.reserved_ram_mb).toBe(8192);
  });

  it('matching project and artifact resources reserve the same value', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost();
    const projectId = await seedProject('p1', agent.id, {
      resources: { cpu: 2, memory: '1Gi' },
    });
    const artifactId = await seedArtifact(projectId, manifest({ cpu: 2, memory: '1Gi' }));
    const res = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      artifact_id: artifactId,
    });
    expect(res.status).toBe(201);
    expect(res.body.deployment.reserved_cpu).toBe(2);
    expect(res.body.deployment.reserved_ram_mb).toBe(1024);
  });

  it('legacy artifact without a manifest falls back to project configuration', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost();
    const projectId = await seedProject('p1', agent.id, {
      resources: { cpu: 1, memory: '512Mi' },
    });
    const artifactId = await seedArtifact(projectId, null);
    const res = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      artifact_id: artifactId,
    });
    expect(res.status).toBe(201);
    expect(res.body.deployment.reserved_cpu).toBe(1);
    expect(res.body.deployment.reserved_ram_mb).toBe(512);
  });

  it('deployment without an artifact uses project configuration', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost();
    const projectId = await seedProject('p1', agent.id, {
      resources: { cpu: 1, memory: '512Mi' },
    });
    const res = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
    });
    expect(res.status).toBe(201);
    expect(res.body.deployment.reserved_cpu).toBe(1);
    expect(res.body.deployment.reserved_ram_mb).toBe(512);
  });

  it('a larger artifact reservation can exhaust the host that the project config would fit', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost(); // 4 CPU / 16384 MiB
    const projectId = await seedProject('p1', agent.id, {
      resources: { cpu: 1, memory: '512Mi' },
    });
    // Project config alone would fit 4x over; the artifact needs 3 CPU / 8Gi.
    const artA = await seedArtifact(projectId, manifest({ cpu: 3, memory: '8Gi' }));
    const first = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      artifact_id: artA,
    });
    expect(first.status).toBe(201);
    const artB = await seedArtifact(projectId, manifest({ cpu: 3, memory: '8Gi' }));
    const second = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '2.0.0',
      artifact_id: artB,
    });
    // 3+3 > 4 CPU: the second deployment is rejected on the ARTIFACT's
    // resources, not the project's.
    expect(second.status).toBe(503);
    expect(second.body.error.code).toBe('no_capacity');
  });
});

// ---------------------------------------------------------------------------
// Unit: one grammar everywhere
// ---------------------------------------------------------------------------
describe('validateManifest — strict worker-parity validation', () => {
  it('accepts every intended memory format', () => {
    for (const memory of ['512m', '512Mi', '1g', '1Gi', '8g', '8Gi']) {
      expect(validateManifest(manifest({ cpu: 1, memory }))).toEqual([]);
    }
  });

  it('all accepted formats normalize to the same internal value', () => {
    expect(normalizeResources({ memory: '1024m' }).ram_mb).toBe(1024);
    expect(normalizeResources({ memory: '1024Mi' }).ram_mb).toBe(1024);
    expect(normalizeResources({ memory: '1g' }).ram_mb).toBe(1024);
    expect(normalizeResources({ memory: '1Gi' }).ram_mb).toBe(1024);
  });

  it('rejects non-conforming shapes', () => {
    expect(validateManifest({})).not.toEqual([]);
    expect(validateManifest(manifest({ memory: '512MB' }))).not.toEqual([]);
    expect(validateManifest(manifest({ cpu: 0 }))).not.toEqual([]);
    expect(validateManifest(manifest({ cpu: NaN }))).not.toEqual([]);
    expect(validateManifest({ ...manifest({ cpu: 1 }), volumes: ['/x'] })).not.toEqual([]);
    expect(validateManifest({ ...manifest({ cpu: 1 }), runtime: 'kubernetes' })).not.toEqual([]);
  });

  it('deploymentConstraintsForManifest prefers the artifact manifest runtime', () => {
    const c = deploymentConstraintsForManifest(manifest({ cpu: 1 }, 'docker-compose'), 'docker');
    expect(c.requiredCapabilities).toEqual(['docker-compose']);
    expect(c.requiredCpuCores).toBe(1);
  });

  it('§5 acceptance: 4CPU/16Gi host fits A+B (2/8Gi each), rejects C (1/1Gi) atomically', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    await seedHost(); // 4 CPU / 16384 MiB
    const projectId = await seedProject('p1', agent.id, {});

    const deployWith = (cpu: number, memory: string, version: string) =>
      seedArtifact(projectId, manifest({ cpu, memory })).then((artifactId) =>
        req('POST', '/v1/deployments', agent.token, {
          project_id: projectId,
          version,
          artifact_id: artifactId,
        }),
      );

    const a = await deployWith(2, '8Gi', 'a');
    expect(a.status).toBe(201);
    expect(a.body.deployment.reserved_cpu).toBe(2);
    expect(a.body.deployment.reserved_ram_mb).toBe(8192);

    const b = await deployWith(2, '8Gi', 'b');
    expect(b.status).toBe(201); // A + B = 4 CPU / 16Gi: exactly at capacity, allowed.

    const c = await deployWith(1, '1Gi', 'c');
    // A + B + C = 5 CPU / 17Gi > 4 / 16: rejected...
    expect(c.status).toBe(503);
    expect(c.body.error.code).toBe('no_capacity');

    // ...atomically: no deployment row and no task row were created for C.
    const { rows } = await state.pool.query(
      `SELECT COUNT(*)::int AS d FROM deployments WHERE version = 'c'`,
    );
    expect(rows[0].d).toBe(0);
    const tasks = await state.pool.query(
      `SELECT COUNT(*)::int AS t FROM tasks WHERE payload->>'version' = 'c'`,
    );
    expect(tasks.rows[0].t).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Finalization: the tarball's embedded agent.deploy.json is verified
// against the init-registered manifest BEFORE the artifact is marked ready
// ---------------------------------------------------------------------------
async function makeTarball(manifestObj: Record<string, unknown> | null): Promise<Buffer> {
  const dir = mkdtempSync(join(tmpdir(), 'uaht-tarball-'));
  const files: string[] = ['app.py'];
  writeFileSync(join(dir, 'app.py'), 'x = 1\n');
  if (manifestObj) {
    writeFileSync(join(dir, 'agent.deploy.json'), JSON.stringify(manifestObj));
    files.push('agent.deploy.json');
  }
  const out = join(dir, 'artifact.tar.gz');
  await tar.create({ gzip: true, file: out, cwd: dir }, files);
  return readFileSync(out);
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

async function initArtifact(
  token: string,
  projectId: string,
  tarball: Buffer,
  manifestObj?: Record<string, unknown>,
) {
  const checksum = 'sha256:' + createHash('sha256').update(tarball).digest('hex');
  const body: Record<string, unknown> = {
    project_id: projectId,
    filename: 'app.tar.gz',
    size: tarball.length,
    checksum,
    version: '1.0.0',
  };
  if (manifestObj) body.manifest = manifestObj;
  const init = await req('POST', '/v1/artifacts/init', token, body);
  expect(init.status).toBe(201);
  return init.body.artifact.id as string;
}

describe('PUT /v1/artifacts/:id/content — tarball manifest verification', () => {
  it('matching init and tarball manifests -> ready with the manifest stored', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const m = manifest({ cpu: 2, memory: '1Gi' });
    const tarball = await makeTarball(m);
    const artifactId = await initArtifact(agent.token, projectId, tarball, m);

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(200);
    expect(up.body.artifact.status).toBe('ready');
    expect(up.body.artifact.manifest).toEqual(m);
  });

  it('tarball manifest disagreeing with the init manifest -> 422, failed', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    // Registered: 2 CPU / 1Gi. Tarball actually: 8 CPU / 8Gi.
    const tarball = await makeTarball(manifest({ cpu: 8, memory: '8Gi' }));
    const artifactId = await initArtifact(agent.token, projectId, tarball, manifest({ cpu: 2, memory: '1Gi' }));

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(422);
    const { rows } = await state.pool.query('SELECT status FROM artifacts WHERE id = $1', [artifactId]);
    expect(rows[0].status).toBe('failed');
    const events = await state.pool.query(
      `SELECT type FROM events WHERE payload->>'artifact_id' = $1`,
      [artifactId],
    );
    expect(events.rows.map((r: any) => r.type)).toContain('artifact.manifest_mismatch');
    // A failed artifact can never be deployed from.
    const dep = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      artifact_id: artifactId,
    });
    expect(dep.status).toBe(422);
  });

  it('over-reservation direction is also rejected (registered 8 CPU, tarball 2 CPU)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const tarball = await makeTarball(manifest({ cpu: 2, memory: '1Gi' }));
    const artifactId = await initArtifact(agent.token, projectId, tarball, manifest({ cpu: 8, memory: '8Gi' }));

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(422);
    const { rows } = await state.pool.query('SELECT status FROM artifacts WHERE id = $1', [artifactId]);
    expect(rows[0].status).toBe('failed');
  });

  it('registered manifest but tarball has none -> 422 (would be a lying registration)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const tarball = await makeTarball(null);
    const artifactId = await initArtifact(agent.token, projectId, tarball, manifest({ cpu: 2, memory: '1Gi' }));

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(422);
    const { rows } = await state.pool.query('SELECT status FROM artifacts WHERE id = $1', [artifactId]);
    expect(rows[0].status).toBe('failed');
  });

  it('no registered manifest + tarball with manifest -> adopted at finalization', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const m = manifest({ cpu: 2, memory: '1Gi' });
    const tarball = await makeTarball(m);
    const artifactId = await initArtifact(agent.token, projectId, tarball);

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(200);
    expect(up.body.artifact.status).toBe('ready');
    expect(up.body.artifact.manifest).toEqual(m);

    // The adopted manifest drives scheduling.
    await seedHost();
    const dep = await req('POST', '/v1/deployments', agent.token, {
      project_id: projectId,
      version: '1.0.0',
      artifact_id: artifactId,
    });
    expect(dep.status).toBe(201);
    expect(dep.body.deployment.reserved_cpu).toBe(2);
    expect(dep.body.deployment.reserved_ram_mb).toBe(1024);
  });

  it('neither registered nor embedded manifest -> legacy ready with NULL manifest', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const tarball = await makeTarball(null);
    const artifactId = await initArtifact(agent.token, projectId, tarball);

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(200);
    expect(up.body.artifact.status).toBe('ready');
    expect(up.body.artifact.manifest).toBeNull();
  });

  it('equivalent contract in different notation is accepted (1g == 1024Mi)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    // Registered "1g", tarball "1024Mi": same canonical contract.
    const tarball = await makeTarball(manifest({ cpu: 1, memory: '1024Mi' }));
    const artifactId = await initArtifact(agent.token, projectId, tarball, manifest({ cpu: 1, memory: '1g' }));

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(200);
    expect(up.body.artifact.status).toBe('ready');
  });
});

describe('PUT /v1/artifacts/:id/content — archive hardening', () => {
  it('detects the archive format by content, not filename (zip named .tar.gz)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const m = manifest({ cpu: 1, memory: '512Mi' });
    // Build a real zip but name it .tar.gz: the worker sniffs content
    // (tarfile.is_tarfile), so the control plane must too.
    const dir = mkdtempSync(join(tmpdir(), 'uaht-zip-'));
    writeFileSync(join(dir, 'agent.deploy.json'), JSON.stringify(m));
    const { execFileSync } = await import('child_process');
    execFileSync('python3', ['-c',
      `import zipfile; z = zipfile.ZipFile(${JSON.stringify(join(dir, 'sneaky.tar.gz'))}, 'w'); ` +
      `z.write(${JSON.stringify(join(dir, 'agent.deploy.json'))}, 'agent.deploy.json'); z.close()`]);
    const tarball = readFileSync(join(dir, 'sneaky.tar.gz'));
    const artifactId = await initArtifact(agent.token, projectId, tarball, m);

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(200);
    expect(up.body.artifact.status).toBe('ready');
  });

  it('an oversized agent.deploy.json is treated as absent (bomb guard)', async () => {
    const agent = await seedAgent('a1', AGENT_PERMS);
    const projectId = await seedProject('p1', agent.id, {});
    const dir = mkdtempSync(join(tmpdir(), 'uaht-big-'));
    const { execFileSync } = await import('child_process');
    execFileSync('python3', ['-c',
      `import zipfile; z = zipfile.ZipFile(${JSON.stringify(join(dir, 'big.zip'))}, 'w'); ` +
      `z.writestr('agent.deploy.json', '{"a": "' + 'x' * 2000000 + '"}'); z.close()`]);
    const tarball = readFileSync(join(dir, 'big.zip'));
    // Registered manifest + tarball whose manifest exceeds the 1MB cap:
    // the embedded manifest cannot be verified -> rejected, never adopted.
    const artifactId = await initArtifact(agent.token, projectId, tarball, manifest({ cpu: 1, memory: '512Mi' }));

    const up = await putBytes(`/v1/artifacts/${artifactId}/content`, agent.token, tarball);
    expect(up.status).toBe(422);
    const { rows } = await state.pool.query('SELECT status FROM artifacts WHERE id = $1', [artifactId]);
    expect(rows[0].status).toBe('failed');
  });
});
