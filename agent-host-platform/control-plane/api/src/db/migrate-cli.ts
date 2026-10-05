// CLI entry for `npm run migrate`: applies pending migrations then exits.
import 'dotenv/config';
import { Pool } from 'pg';
import { runMigrations } from './migrate';
import { logger } from '../lib/log';

async function main(): Promise<void> {
  const pool = new Pool({ connectionString: process.env.DATABASE_URL });
  try {
    const applied = await runMigrations(pool);
    logger.info('migrations complete', { applied });
  } finally {
    await pool.end();
  }
}

main().catch((err) => {
  logger.error('migration failed', { err: String(err) });
  process.exit(1);
});
