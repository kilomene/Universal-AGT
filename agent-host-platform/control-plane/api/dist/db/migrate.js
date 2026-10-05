"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.runMigrations = runMigrations;
const fs_1 = require("fs");
const path_1 = require("path");
const log_1 = require("../lib/log");
// Applies database/migrations/*.sql in filename order, tracking what has
// been applied in the schema_migrations table. Each migration runs inside
// its own transaction.
function findMigrationsDir() {
    // dist layout: <repo>/agent-host-platform/control-plane/api/dist/db
    let dir = (0, path_1.resolve)(__dirname, '..', '..', '..', '..');
    for (let i = 0; i < 4; i++) {
        const candidate = (0, path_1.join)(dir, 'database', 'migrations');
        try {
            (0, fs_1.readdirSync)(candidate);
            return candidate;
        }
        catch {
            /* try one level up */
        }
        dir = (0, path_1.resolve)(dir, '..');
    }
    throw new Error('could not locate database/migrations directory');
}
async function runMigrations(pool) {
    const migrationsDir = findMigrationsDir();
    await pool.query(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      name TEXT PRIMARY KEY,
      applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
  `);
    const { rows } = await pool.query('SELECT name FROM schema_migrations');
    const applied = new Set(rows.map((r) => r.name));
    const files = (0, fs_1.readdirSync)(migrationsDir)
        .filter((f) => f.endsWith('.sql'))
        .sort();
    const newlyApplied = [];
    for (const file of files) {
        if (applied.has(file))
            continue;
        const sql = (0, fs_1.readFileSync)((0, path_1.join)(migrationsDir, file), 'utf8');
        const client = await pool.connect();
        try {
            await client.query('BEGIN');
            await client.query(sql);
            await client.query('INSERT INTO schema_migrations (name) VALUES ($1)', [file]);
            await client.query('COMMIT');
            log_1.logger.info('migration applied', { file });
            newlyApplied.push(file);
        }
        catch (err) {
            await client.query('ROLLBACK');
            throw new Error(`migration ${file} failed: ${String(err)}`);
        }
        finally {
            client.release();
        }
    }
    return newlyApplied;
}
