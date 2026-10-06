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
// A structural test below runs the REAL tryClaim SQL against a stub client
// to prove the claim no longer stamps assigned_to (§13).
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
import { tryClaim } from '../src/routes/worker';

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
      created_at TIMESTAMPTZ DEFAULT now(),
      worker_draining BOOLEAN NOT NULL DEFAULT false,
      total_cpu DOUBLE PRECISION, total_ram_mb DOUBLE PRECISION
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
      created_at TIMESTAMPTZ DEFAULT now(),
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION
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

/** Mirrors routes/worker.ts tryClaim (pg-mem cannot run the real FOR UPDATE
 *  SKIP LOCKED claim). §13: the claim sets claimed_by but deliberately does
 *  NOT stamp assigned_to — assigned_to is the agent-requested host pin only.
 */
async function simulateClaim(taskId: string, hostId: string, leasePast = false) {
  await state.pool.query(
    `UPDATE tasks SET status = 'claimed', claimed_by = $2,
       attempts = attempts + 1,
       lease_expires_at = now() ${leasePast ? `-` : `+`} interval '1 second'
     WHERE id = $1`,
    [taskId, hostId],
  );
}

/** The exact claim filter predicate from tryClaim, minus SKIP LOCKED
 *  (which pg-mem cannot parse): which queued tasks may this host claim? */
async function claimableBy(hostId: string): Promise<string[]> {
  const { rows } = await state.pool.query(
    `SELECT id FROM tasks
     WHERE status = 'queued' AND (assigned_to IS NULL OR assigned_to = $1)`,
    [hostId],
  );
  return rows.map((r: any) => r.id as string);
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

describe('§13 host disappears mid-task -> lease expires -> another host may claim', () => {
  it('a requeued task is NOT pinned to the dead host; any eligible host can claim it', async () => {
    const hostA = await seedHost('w13b-dead-a');
    const hostB = await seedHost('w13b-alive-b');
    const taskId = await seedTask(); // assigned_to NULL: no agent pin

    // Host A claims the task, then dies without another progress report.
    await simulateClaim(taskId, hostA.id, /* leasePast */ true);
    const claimed = await getTask(taskId);
    expect(claimed.claimed_by).toBe(hostA.id);
    // §13: the claim must not stamp assigned_to — it stays NULL.
    expect(claimed.assigned_to).toBeNull();

    // The REAL sweeper requeues the lease-expired task.
    await sweepTasksOnce(state.pool);
    const swept = await getTask(taskId);
    expect(swept.status).toBe('queued');
    expect(swept.claimed_by).toBeNull();
    // The requeued task is not pinned to the dead host...
    expect(swept.assigned_to).toBeNull();

    // ...so the real claim filter admits host B (this is the exact
    // predicate from tryClaim; previously assigned_to was still host A and
    // B could never match it).
    expect(await claimableBy(hostB.id)).toContain(taskId);

    // Host B claims it for real.
    await simulateClaim(taskId, hostB.id);
    const bClaimed = await getTask(taskId);
    expect(bClaimed.status).toBe('claimed');
    expect(bClaimed.claimed_by).toBe(hostB.id);
    expect(bClaimed.assigned_to).toBeNull();
  });

  it('an agent-requested host pin (assigned_to) is still honored after requeue', async () => {
    const hostA = await seedHost('w13b-pin-a');
    const hostB = await seedHost('w13b-pin-b');
    const { rows } = await state.pool.query(
      `INSERT INTO tasks (id, type, status, payload, assigned_to, max_attempts)
       VALUES ($1, 'system-info', 'queued', '{}', $2, 10) RETURNING id`,
      [randomUUID(), hostA.id],
    );
    const taskId: string = rows[0].id;

    // Host A claims its pinned task and dies; the sweeper requeues it.
    await simulateClaim(taskId, hostA.id, /* leasePast */ true);
    await sweepTasksOnce(state.pool);
    const swept = await getTask(taskId);
    expect(swept.status).toBe('queued');
    // The agent's pin survives the requeue — it was never the claim stamp.
    expect(swept.assigned_to).toBe(hostA.id);

    // Host B still cannot take pinned work; host A can.
    expect(await claimableBy(hostB.id)).not.toContain(taskId);
    expect(await claimableBy(hostA.id)).toContain(taskId);
  });

  it('tryClaim SQL never stamps assigned_to and keeps the pin filter (structural)', async () => {
    const seen: string[] = [];
    const stubClient = {
      query: async (sql: string) => {
        seen.push(sql);
        return { rows: [] };
      },
    };
    await tryClaim(stubClient as never, 'host-1', 600);
    expect(seen.length).toBe(1);
    const sql = seen[0];
    // The SET clause must not touch assigned_to ...
    const setClause = sql.slice(sql.indexOf('SET'), sql.indexOf('WHERE id = ('));
    expect(setClause).toContain('claimed_by = $1');
    expect(setClause).not.toMatch(/assigned_to/);
    // ... while the claim filter still honors the agent-requested pin.
    expect(sql).toContain("(assigned_to IS NULL OR assigned_to = $1)");
    expect(sql).toContain('FOR UPDATE SKIP LOCKED');
  });
});
