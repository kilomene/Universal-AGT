// §6 migration: 012_rollback_failed_status.sql.
//
// Runs the REAL migration file against pg-mem in two scenarios:
//   * upgrade: a database whose deployments table carries the 001 check
//     constraint (named as real PostgreSQL auto-names it,
//     deployments_status_check) -> 'rollback_failed' becomes insertable,
//     all pre-existing statuses still valid, re-running is a no-op.
//   * the widened constraint still rejects statuses outside the set.
//
// NOTE: pg-mem auto-names an inline CHECK differently than real PostgreSQL
// (deployments_constraint_1 vs deployments_status_check), so the legacy
// table here declares the constraint with the explicit real-PG name —
// exactly what the migration's DROP CONSTRAINT IF EXISTS targets.
import { readFileSync } from 'fs';
import { join } from 'path';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { describe, expect, it } from 'vitest';

const MIGRATION_FILE = join(
  __dirname,
  '..',
  '..',
  '..',
  'database',
  'migrations',
  '012_rollback_failed_status.sql',
);

function migrationStatements(): string[] {
  const sql = readFileSync(MIGRATION_FILE, 'utf8');
  const stripped = sql
    .split('\n')
    .filter((line) => !/^\s*--/.test(line))
    .join('\n');
  return stripped
    .split(';')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

// Legacy deployments table: the 001 CHECK, explicitly named the way real
// PostgreSQL auto-names an inline CHECK (deployments_status_check).
function legacyDb() {
  const db = newDb();
  db.public.none(`
    CREATE TABLE deployments (
      id TEXT PRIMARY KEY,
      status TEXT NOT NULL DEFAULT 'requested',
      CONSTRAINT deployments_status_check
        CHECK (status IN ('requested','approved','building','starting',
                          'healthcheck','running','failed','rolled_back',
                          'stopped','stopping'))
    );
    INSERT INTO deployments (id, status) VALUES
      ('${randomUUID()}', 'running'),
      ('${randomUUID()}', 'rolled_back');
  `);
  const pg = db.adapters.createPg();
  return new pg.Pool();
}

async function runMigration(pool: any) {
  for (const stmt of migrationStatements()) {
    await pool.query(stmt);
  }
}

describe('012_rollback_failed_status migration', () => {
  it('upgrade: admits rollback_failed, keeps legacy rows valid, re-run is a no-op', async () => {
    const pool = legacyDb();

    await runMigration(pool);

    // The new status is now insertable (previously it violated the check).
    await pool.query(
      `INSERT INTO deployments (id, status) VALUES ($1, 'rollback_failed')`,
      [randomUUID()],
    );
    // Every legacy status still passes.
    for (const s of ['requested', 'approved', 'running', 'failed', 'rolled_back', 'stopped']) {
      await pool.query(`INSERT INTO deployments (id, status) VALUES ($1, $2)`, [
        randomUUID(),
        s,
      ]);
    }

    // Idempotent: running the migration again changes nothing.
    await runMigration(pool);
    await pool.query(`INSERT INTO deployments (id, status) VALUES ($1, 'rollback_failed')`, [
      randomUUID(),
    ]);
    await pool.end();
  });

  it('still rejects statuses outside the widened set', async () => {
    const pool = legacyDb();
    await runMigration(pool);
    await expect(
      pool.query(`INSERT INTO deployments (id, status) VALUES ($1, 'bogus')`, [randomUUID()]),
    ).rejects.toThrow();
    await pool.end();
  });
});
