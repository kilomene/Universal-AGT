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
//   005_events_truncate_block.sql — CREATE EVENT TRIGGER needs a true
//     superuser (service_role is NOT enough). This is the append-only
//     guarantee for the events journal; skipping it silently would leave
//     the journal truncatable.
//   001_initial.sql — CREATE EXTENSION pgcrypto needs superuser UNLESS the
//     extension is already installed (fresh Supabase projects ship it).
// ---------------------------------------------------------------------------

async function isSuperuser(pool: Pool): Promise<boolean> {
  const { rows } = await pool.query(
    `SELECT current_setting('is_superuser', true) = 'on' AS super`,
  );
  return rows[0]?.super === true;
}

export async function checkMigrationPrivileges(pool: Pool, file: string): Promise<void> {
  if (file === '005_events_truncate_block.sql') {
    let superuser = false;
    try {
      superuser = await isSuperuser(pool);
    } catch {
      // The probe itself is unsupported (exotic PG-compatible backend):
      // let the migration attempt run so its native error surfaces.
      return;
    }
    if (!superuser) {
      throw new Error(
        `migration ${file} requires superuser: it installs an EVENT TRIGGER ` +
          `(CREATE EVENT TRIGGER needs a true superuser; Supabase service_role is not enough) ` +
          `that aborts TRUNCATE on the append-only events journal. ` +
          `Safe path: paste the contents of database/migrations/${file} into the Supabase SQL editor ` +
          `(it runs as superuser) and execute it, then record it as applied with\n` +
          `  INSERT INTO schema_migrations (name) VALUES ('${file}');\n` +
          `and restart the API. Do NOT skip this migration — without it the events journal is truncatable.`,
      );
    }
    return;
  }
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
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      await client.query(sql);
      await client.query('INSERT INTO schema_migrations (name) VALUES ($1)', [file]);
      await client.query('COMMIT');
      logger.info('migration applied', { file });
      newlyApplied.push(file);
    } catch (err) {
      await client.query('ROLLBACK');
      throw new Error(`migration ${file} failed: ${String(err)}`);
    } finally {
      client.release();
    }
  }
  return newlyApplied;
}
