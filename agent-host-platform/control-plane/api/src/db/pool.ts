import { Pool } from 'pg';
import { logger } from '../lib/log';

let pool: Pool | null = null;

export function getPool(): Pool {
  if (pool) return pool;
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error('DATABASE_URL is not set');
  }
  pool = new Pool({
    connectionString,
    max: Number(process.env.PG_POOL_MAX ?? 10),
  });
  pool.on('error', (err) => {
    logger.error('pg pool error', { err: String(err) });
  });
  return pool;
}

export async function closePool(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
  }
}
