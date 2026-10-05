"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
// CLI entry for `npm run migrate`: applies pending migrations then exits.
require("dotenv/config");
const pg_1 = require("pg");
const migrate_1 = require("./migrate");
const log_1 = require("../lib/log");
async function main() {
    const pool = new pg_1.Pool({ connectionString: process.env.DATABASE_URL });
    try {
        const applied = await (0, migrate_1.runMigrations)(pool);
        log_1.logger.info('migrations complete', { applied });
    }
    finally {
        await pool.end();
    }
}
main().catch((err) => {
    log_1.logger.error('migration failed', { err: String(err) });
    process.exit(1);
});
