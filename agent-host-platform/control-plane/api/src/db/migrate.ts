import { readdirSync, readFileSync } from 'fs';
import { join, resolve } from 'path';
import { Pool } from 'pg';
import { logger } from '../lib/log';

// Applies database/migrations/*.sql in filename order, tracking what has
// been applied in the schema_migrations table. Each migration runs inside
// its own transaction.

function findMigrationsDir(): string {
  // dist layout: <repo>/agent-host-platform/control-plane/api/dist/db
  let dir = resolve(__dirname, '..', '..', '..', '..');
  for (let i = 0; i < 4; i++) {
    const candidate = join(dir, 'database', 'migrations');
    try {
      readdirSync(candidate);
      return candidate;
    } catch {
      /* try one level up */
    }
    dir = resolve(dir, '..');
  }
  throw new Error('could not locate database/migrations directory');
}

// ---------------------------------------------------------------------------
// no-transaction migrations.
//
// PostgreSQL forbids some statements inside a transaction block
// (notably CREATE EVENT TRIGGER). A migration whose header (first 10 lines)
// contains:
//
//   -- migrate: no-transaction
//
// runs WITHOUT the BEGIN/COMMIT wrapper. Such migrations MUST be written
// idempotently (CREATE OR REPLACE / DROP IF EXISTS), because a failure
// partway through cannot be rolled back and the file will be retried.
//
// Only the header is honored: a marker buried deeper in the file cannot
// silently disable the transactional safety of the migration.
// ---------------------------------------------------------------------------

const NO_TRANSACTION_MARKER = /^[ \t]*--[ \t]*migrate:[ \t]*no-transaction[ \t]*$/im;

export function requiresNoTransaction(sql: string): boolean {
  return NO_TRANSACTION_MARKER.test(sql.split('\n').slice(0, 10).join('\n'));
}

// ---------------------------------------------------------------------------
// §46 privilege preflight.
//
// Migrations that need elevated privileges are declared here. Before such a
// migration runs, the current role is probed; a missing privilege throws a
// PRECISE, actionable error instead of letting the migration die halfway
// with a native Postgres message. The safe path is always the same: apply
// the single migration file as a superuser (Supabase SQL editor runs as
// superuser), mark it applied, then restart — never comment the migration
// out, never mark it applied without running it.
//
// Current entries:
//   001_initial.sql — CREATE EXTENSION pgcrypto needs superuser UNLESS the
//     extension is already installed (fresh Supabase projects ship it).
//
// Historical note: 005_events_truncate_block.sql USED to install an EVENT
// TRIGGER (CREATE EVENT TRIGGER needs a true superuser). That approach was
// removed: PostgreSQL does not support event triggers on TRUNCATE at all
// (server error "event triggers are not supported for TRUNCATE TABLE",
// verified on PG 16), so the migration is now a plain REVOKE, which the
// table owner can run. If you see an old preflight for 005 here, delete it.
// ---------------------------------------------------------------------------

async function isSuperuser(pool: Pool): Promise<boolean> {
  const { rows } = await pool.query(
    `SELECT current_setting('is_superuser', true) = 'on' AS super`,
  );
  return rows[0]?.super === true;
}

export async function checkMigrationPrivileges(pool: Pool, file: string): Promise<void> {
  if (file === '001_initial.sql') {
    let extInstalled = false;
    let superuser = false;
    try {
      const { rows } = await pool.query(
        `SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto') AS installed`,
      );
      extInstalled = rows[0]?.installed === true;
      if (!extInstalled) superuser = await isSuperuser(pool);
    } catch {
      return;
    }
    if (!extInstalled && !superuser) {
      throw new Error(
        `migration ${file} needs the pgcrypto extension, which is not installed and the current ` +
          `database role cannot install extensions (CREATE EXTENSION needs superuser). ` +
          `Safe path: install it once via the Supabase dashboard (Database -> Extensions -> pgcrypto) ` +
          `or run CREATE EXTENSION IF NOT EXISTS "pgcrypto"; as superuser in the SQL editor, ` +
          `then restart the API.`,
      );
    }
  }
}

export async function runMigrations(pool: Pool): Promise<string[]> {
  const migrationsDir = findMigrationsDir();
  await pool.query(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      name TEXT PRIMARY KEY,
      applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
  `);
  const { rows } = await pool.query('SELECT name FROM schema_migrations');
  const applied = new Set(rows.map((r: { name: string }) => r.name));

  const files = readdirSync(migrationsDir)
    .filter((f) => f.endsWith('.sql'))
    .sort();
  const newlyApplied: string[] = [];

  for (const file of files) {
    if (applied.has(file)) continue;
    // §46 privilege preflight: some migrations need elevated privileges
    // (superuser, pg extensions). Detect the missing privilege BEFORE the
    // migration fails with a cryptic native error, and tell the operator
    // exactly how to apply it safely. Security migrations are NEVER
    // silently skipped — a missing privilege is a hard error here.
    await checkMigrationPrivileges(pool, file);
    const sql = readFileSync(join(migrationsDir, file), 'utf8');
    const noTx = requiresNoTransaction(sql);
    const client = await pool.connect();
    try {
      if (noTx) {
        // Statements like CREATE EVENT TRIGGER cannot run inside a
        // transaction block. The migration must be idempotent (see header
        // contract above); record it applied only after it succeeds.
        await client.query(sql);
        await client.query('INSERT INTO schema_migrations (name) VALUES ($1)', [file]);
        logger.info('migration applied (no-transaction)', { file });
      } else {
        await client.query('BEGIN');
        try {
          await client.query(sql);
          await client.query('INSERT INTO schema_migrations (name) VALUES ($1)', [file]);
          await client.query('COMMIT');
        } catch (err) {
          await client.query('ROLLBACK');
          throw err;
        }
        logger.info('migration applied', { file });
      }
      newlyApplied.push(file);
    } catch (err) {
      throw new Error(`migration ${file} failed: ${String(err)}`);
    } finally {
      client.release();
    }
  }
  return newlyApplied;
}
