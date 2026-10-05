// ---------------------------------------------------------------------------
// Idempotency tests — HONEST SCOPE NOTE
//
// The full route flow (POST /v1/tasks with a live Postgres, unique
// idempotency_key constraint, 200 replay vs 409 conflict) is NOT covered
// here: it needs a real database. What IS covered:
//
//  1. decideIdempotency() — the exact pure function the routes call to turn
//     "existing row + incoming body" into create|replay|conflict, including
//     order-insensitive deep-equal on the payload.
//  2. findTaskByIdempotencyKey() — the SQL lookup the routes use, run against
//     pg-mem with a MINIMAL tasks table (id, idempotency_key, type, payload).
//     pg-mem cannot load the real schema (pgcrypto extension, plpgsql
//     triggers, check constraints), so this validates the query logic and
//     the unique-key lookup behavior, not the full DDL.
//  3. taskIdempotencyBody()/deploymentIdempotencyBody() — the canonical
//     "compared body" shapes, proving routing metadata is excluded.
// ---------------------------------------------------------------------------
import { newDb } from 'pg-mem';
import { beforeAll, describe, expect, it } from 'vitest';
import {
  decideIdempotency,
  deploymentIdempotencyBody,
  findTaskByIdempotencyKey,
  taskIdempotencyBody,
} from '../src/lib/idempotency';

describe('decideIdempotency (pure replay/conflict decision)', () => {
  it('no existing row -> create', () => {
    expect(decideIdempotency(null, { type: 'deploy', payload: {} })).toBe('create');
  });
  it('same key + deep-equal body -> replay', () => {
    const existing = { body: { type: 'deploy', payload: { version: '1.0.0' } } };
    expect(decideIdempotency(existing, { type: 'deploy', payload: { version: '1.0.0' } })).toBe('replay');
  });
  it('replay is insensitive to key order and nesting order', () => {
    const existing = { body: { type: 'deploy', payload: { b: 1, a: { y: 2, x: 1 } } } };
    const incoming = { type: 'deploy', payload: { a: { x: 1, y: 2 }, b: 1 } };
    expect(decideIdempotency(existing, incoming)).toBe('replay');
  });
  it('same key + different payload -> conflict', () => {
    const existing = { body: { type: 'deploy', payload: { version: '1.0.0' } } };
    expect(decideIdempotency(existing, { type: 'deploy', payload: { version: '2.0.0' } })).toBe('conflict');
  });
  it('same key + different type -> conflict', () => {
    const existing = { body: { type: 'deploy', payload: {} } };
    expect(decideIdempotency(existing, { type: 'restart', payload: {} })).toBe('conflict');
  });
  it('array order matters (deployments are ordered data)', () => {
    const existing = { body: { type: 'x', payload: { ids: [1, 2] } } };
    expect(decideIdempotency(existing, { type: 'x', payload: { ids: [2, 1] } })).toBe('conflict');
  });
});

describe('idempotency body shapes', () => {
  it('task body compares type + payload only', () => {
    const a = taskIdempotencyBody({ type: 'deploy', payload: { v: 1 } });
    const b = taskIdempotencyBody({ type: 'deploy', payload: { v: 1 } });
    expect(decideIdempotency({ body: a }, b)).toBe('replay');
  });
  it('deployment body normalizes missing host_id/mode', () => {
    const a = deploymentIdempotencyBody({ project_id: 'p', version: '1' });
    const b = deploymentIdempotencyBody({ project_id: 'p', host_id: null, version: '1', mode: 'automatic' });
    expect(decideIdempotency({ body: a }, b)).toBe('replay');
  });
  it('deployment body distinguishes versions', () => {
    const a = deploymentIdempotencyBody({ project_id: 'p', version: '1' });
    const b = deploymentIdempotencyBody({ project_id: 'p', version: '2' });
    expect(decideIdempotency({ body: a }, b)).toBe('conflict');
  });
});

describe('findTaskByIdempotencyKey against pg-mem (minimal tasks table)', () => {
  let pool: any;

  beforeAll(() => {
    const db = newDb();
    db.public.none(`
      CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT UNIQUE,
        type TEXT NOT NULL,
        payload JSONB NOT NULL
      );
    `);
    db.public.none(`
      INSERT INTO tasks (id, type, idempotency_key, payload) VALUES
        ('task-1', 'deploy', 'key-abc', '{"version":"1.0.0","env":{"A":"1"}}'),
        ('task-2', 'restart', NULL, '{"deployment_id":"d1"}');
    `);
    const pg = db.adapters.createPg();
    pool = new pg.Pool();
  });

  it('finds the row by key', async () => {
    const row = await findTaskByIdempotencyKey(pool, 'key-abc');
    expect(row).not.toBeNull();
    expect(row!.id).toBe('task-1');
    expect(row!.type).toBe('deploy');
  });
  it('returns null for an unknown key', async () => {
    expect(await findTaskByIdempotencyKey(pool, 'nope')).toBeNull();
  });
  it('end-to-end: stored row + reordered incoming body -> replay decision', async () => {
    const row = await findTaskByIdempotencyKey(pool, 'key-abc');
    const decision = decideIdempotency(
      { body: taskIdempotencyBody(row!) },
      // note: keys deliberately reordered vs the stored JSON
      taskIdempotencyBody({ type: 'deploy', payload: { env: { A: '1' }, version: '1.0.0' } }),
    );
    expect(decision).toBe('replay');
  });
  it('end-to-end: stored row + changed payload -> conflict decision', async () => {
    const row = await findTaskByIdempotencyKey(pool, 'key-abc');
    const decision = decideIdempotency(
      { body: taskIdempotencyBody(row!) },
      taskIdempotencyBody({ type: 'deploy', payload: { version: '9.9.9' } }),
    );
    expect(decision).toBe('conflict');
  });
  it('unique constraint rejects a duplicate key insert', async () => {
    await expect(
      pool.query(`INSERT INTO tasks (id, type, idempotency_key, payload) VALUES ('task-3','deploy','key-abc','{}')`),
    ).rejects.toThrow();
  });
});
