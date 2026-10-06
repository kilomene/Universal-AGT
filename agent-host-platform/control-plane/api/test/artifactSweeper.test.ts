// Stale-pending artifact sweeper tests (real pg-mem database).
//
// §16/§60: an artifact row left `pending` by an abandoned upload must not
// linger forever — the sweeper fails it (with an auditable event) once it is
// older than the TTL. Fresh pending rows (upload may still be in flight) and
// non-pending rows are never touched.
import { newDb } from 'pg-mem';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ pool: null as any }));
vi.mock('../src/db/pool', () => ({
  getPool: () => state.pool,
  closePool: async () => {},
}));

import { readArtifactSweeperConfig, sweepStalePendingArtifacts } from '../src/lib/artifactSweeper';

function setupDb() {
  const db = newDb();
  db.public.none(`
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      filename TEXT NOT NULL,
      storage_path TEXT NOT NULL DEFAULT '',
      checksum TEXT NOT NULL DEFAULT '',
      size BIGINT NOT NULL DEFAULT 0,
      version TEXT NOT NULL DEFAULT 'v1',
      status TEXT NOT NULL DEFAULT 'pending',
      manifest JSONB,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
  state.pool = new pg.Pool();
}

beforeEach(() => {
  setupDb();
});

afterEach(async () => {
  await state.pool.query('DELETE FROM events');
  await state.pool.query('DELETE FROM artifacts');
});

async function seedArtifact(id: string, status: string, createdAgoS: number) {
  await state.pool.query(
    `INSERT INTO artifacts (id, project_id, filename, status, created_at)
     VALUES ($1, 'proj-1', $2, $3, now() - ($4 || ' seconds')::interval)`,
    [id, `${id}.tar.gz`, status, String(createdAgoS)],
  );
}

describe('readArtifactSweeperConfig', () => {
  it('defaults to 300s interval, 24h pending TTL', () => {
    expect(readArtifactSweeperConfig({})).toEqual({ intervalS: 300, pendingTtlS: 86400 });
  });
  it('reads env overrides and ignores invalid values', () => {
    expect(
      readArtifactSweeperConfig({ ARTIFACT_SWEEP_INTERVAL_S: '60', ARTIFACT_PENDING_TTL_S: '3600' }),
    ).toEqual({ intervalS: 60, pendingTtlS: 3600 });
    expect(readArtifactSweeperConfig({ ARTIFACT_PENDING_TTL_S: 'nope' })).toEqual({
      intervalS: 300,
      pendingTtlS: 86400,
    });
  });
});

describe('sweepStalePendingArtifacts', () => {
  it('fails pending rows older than the TTL and emits an event', async () => {
    await seedArtifact('old-pending', 'pending', 90000); // 25h > 24h TTL
    const failed = await sweepStalePendingArtifacts(state.pool);
    expect(failed).toBe(1);
    const { rows } = await state.pool.query(`SELECT status FROM artifacts WHERE id = 'old-pending'`);
    expect(rows[0].status).toBe('failed');
    const ev = await state.pool.query(`SELECT type, payload FROM events`);
    expect(ev.rows).toHaveLength(1);
    expect(ev.rows[0].type).toBe('artifact.upload_failed');
    expect(ev.rows[0].payload.reason).toBe('abandoned');
  });

  it('leaves fresh pending rows alone (upload may be in flight)', async () => {
    await seedArtifact('fresh-pending', 'pending', 60);
    expect(await sweepStalePendingArtifacts(state.pool)).toBe(0);
    const { rows } = await state.pool.query(`SELECT status FROM artifacts WHERE id = 'fresh-pending'`);
    expect(rows[0].status).toBe('pending');
  });

  it('never touches non-pending rows', async () => {
    await seedArtifact('old-ready', 'ready', 90000);
    await seedArtifact('old-failed', 'failed', 90000);
    expect(await sweepStalePendingArtifacts(state.pool)).toBe(0);
  });

  it('is idempotent across runs', async () => {
    await seedArtifact('twice', 'pending', 90000);
    expect(await sweepStalePendingArtifacts(state.pool)).toBe(1);
    expect(await sweepStalePendingArtifacts(state.pool)).toBe(0);
  });
});
