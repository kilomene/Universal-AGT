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
