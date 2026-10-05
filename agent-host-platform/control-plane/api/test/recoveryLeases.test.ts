// W13a §39(a) — lease ownership on the REAL API (pg-mem).
//
// Worker A claims a task and dies; the claim lease expires; the REAL
// sweepTasksOnce() (control-plane/api/src/lib/taskSweeper.ts) requeues the
// task; worker B claims it; worker A's stale progress is rejected with
// 403 and cannot corrupt B's task; B completes it.
//
// The claim step itself cannot run under pg-mem (FOR UPDATE SKIP LOCKED),
// so claims are simulated with the same UPDATE the route performs — the
// same sanctioned approach as test/e2eFlows.test.ts. Everything else
// (sweeper, progress ownership check, state machine) is the real route.
import http from 'http';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any, db: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { createApp } from '../src/index';
import { generateHostToken } from '../src/lib/tokens';
import { sha256Hex } from '../src/lib/tokens';
import { sweepTasksOnce } from '../src/lib/taskSweeper';

function setupDb() {
  const db = newDb();
  let n = 0;
  db.public.registerFunction({
    name: 'gen_random_uuid',
    returns: 'text',
    impure: true,
    implementation: () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`,
  });
  db.public.none(`
    CREATE TABLE agents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      permissions JSONB, api_key_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',
      last_seen TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE hosts (
      id TEXT PRIMARY KEY, name TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'online',
      token_hash TEXT NOT NULL,
      previous_token_hash TEXT, previous_token_expires_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now()
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
      task_id TEXT,
      created_at TIMESTAMPTZ DEFAULT now()
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

async function seedHost(name: string) {
  const { token, hash } = generateHostToken();
  const id = randomUUID();
  await state.pool.query(
    `INSERT INTO hosts (id, name, token_hash, status) VALUES ($1, $2, $3, 'online')`,
    [id, name, hash],
  );
  return { id, token };
}

async function seedTask(): Promise<string> {
  const { rows } = await state.pool.query(
    `INSERT INTO tasks (id, type, status, payload, max_attempts)
     VALUES ($1, 'system-info', 'queued', '{}', 10) RETURNING id`,
    [randomUUID()],
  );
  return rows[0].id;
}

/** The same UPDATE routes/worker.ts tryClaim performs (pg-mem cannot run
 *  the real FOR UPDATE SKIP LOCKED claim). */
async function simulateClaim(taskId: string, hostId: string, leasePast = false) {
  await state.pool.query(
    `UPDATE tasks SET status = 'claimed', claimed_by = $2, assigned_to = $2,
       attempts = attempts + 1,
       lease_expires_at = now() ${leasePast ? `-` : `+`} interval '1 second'
     WHERE id = $1`,
    [taskId, hostId],
  );
}

async function getTask(taskId: string) {
  const { rows } = await state.pool.query(`SELECT * FROM tasks WHERE id = $1`, [taskId]);
  return rows[0];
}

beforeAll(async () => {
  setupDb();
  const app = createApp();
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  base = `http://127.0.0.1:${(addr as any).port}`;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

describe('§39(a) lease expiry and claim ownership (real API + real sweeper)', () => {
  it('expired lease -> sweeper requeues; stale worker cannot corrupt the new claim', async () => {
    const hostA = await seedHost('w13a-lease-a');
    const hostB = await seedHost('w13a-lease-b');
    const taskId = await seedTask();

    // Worker A claims, then dies without another progress report.
    await simulateClaim(taskId, hostA.id, /* leasePast */ true);

    // The REAL sweeper requeues the lease-expired task.
    await sweepTasksOnce(state.pool);
    const swept = await getTask(taskId);
    expect(swept.status).toBe('queued');
    expect(swept.claimed_by).toBeNull();
    expect(swept.lease_expires_at).toBeNull();
    expect(swept.attempts).toBe(2); // claim + lost attempt both counted
    const { rows: ev } = await state.pool.query(
      `SELECT type FROM events WHERE task_id = $1 ORDER BY id`, [taskId]);
    expect(ev.map((r: any) => r.type)).toContain('task.requeued');

    // Worker B claims the requeued task.
    await simulateClaim(taskId, hostB.id);

    // Worker A comes back and tries to report on B's task: 403, precise.
    const stale = await req('POST', `/v1/worker/tasks/${taskId}/progress`, hostA.token, {
      status: 'running',
    });
    expect(stale.status).toBe(403);
    expect(stale.body.error.code).toBe('forbidden');
    expect(stale.body.error.message).toMatch(/only the claiming host/);

    // The stale write changed nothing.
    const untouched = await getTask(taskId);
    expect(untouched.status).toBe('claimed');
    expect(untouched.claimed_by).toBe(hostB.id);

    // B's legitimate progress flows: running (lease refreshed) -> completed.
    const leaseBefore = (await getTask(taskId)).lease_expires_at;
    const running = await req('POST', `/v1/worker/tasks/${taskId}/progress`, hostB.token, {
      status: 'running',
    });
    expect(running.status).toBe(200);
    const leaseAfter = (await getTask(taskId)).lease_expires_at;
    expect(new Date(leaseAfter).getTime()).toBeGreaterThanOrEqual(
      new Date(leaseBefore).getTime(),
    );

    const done = await req('POST', `/v1/worker/tasks/${taskId}/progress`, hostB.token, {
      status: 'completed',
      result: { ok: true },
    });
    expect(done.status).toBe(200);
    expect(done.body.task.status).toBe('completed');

    // After B's terminal report, A's token is still rejected (ownership is
    // per-claim, and the claim is over) — no resurrection of stale writes.
    const late = await req('POST', `/v1/worker/tasks/${taskId}/progress`, hostA.token, {
      status: 'failed',
      error: 'stale worker trying to fail a completed task',
    });
    expect(late.status).toBe(403);
    expect((await getTask(taskId)).status).toBe('completed');
  });

  it('an actively-reporting worker (live lease) is never swept', async () => {
    const host = await seedHost('w13a-lease-live');
    const taskId = await seedTask();
    await simulateClaim(taskId, host.id, /* leasePast */ false);

    await sweepTasksOnce(state.pool);
    const row = await getTask(taskId);
    expect(row.status).toBe('claimed');
    expect(row.claimed_by).toBe(host.id);
  });

  it('exhausted retry budget -> failed, not requeued forever', async () => {
    const host = await seedHost('w13a-lease-budget');
    const { rows } = await state.pool.query(
      `INSERT INTO tasks (id, type, status, payload, attempts, max_attempts)
       VALUES ($1, 'system-info', 'queued', '{}', 0, 1) RETURNING id`,
      [randomUUID()],
    );
    const taskId: string = rows[0].id;
    await simulateClaim(taskId, host.id, /* leasePast */ true);

    await sweepTasksOnce(state.pool);
    const row = await getTask(taskId);
    expect(row.status).toBe('failed');
    const { rows: ev } = await state.pool.query(
      `SELECT payload FROM events WHERE task_id = $1 AND type = 'task.failed'
       ORDER BY id DESC LIMIT 1`,
      [taskId],
    );
    expect(ev[0].payload.reason).toBe('max_attempts_exceeded');
  });
});
