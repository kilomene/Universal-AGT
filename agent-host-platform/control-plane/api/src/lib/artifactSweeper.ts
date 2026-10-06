import type { Pool } from 'pg';
import { insertEvent, publishEvent } from './events';
import { logger } from './log';

// Stale-pending artifact sweeper (§16 failed-upload handling, §60 cleanup).
//
// An artifact row is created `pending` by POST /v1/artifacts/init and becomes
// `ready`/`failed` when the PUT upload completes or fails verification. If the
// client disappears mid-upload (or never PUTs at all), the row stays `pending`
// forever: harmless metadata, but it pollutes listings and — worse — a retry
// with the same project+version 409s against the unique constraint with no
// path forward. Every ARTIFACT_SWEEP_INTERVAL_S (default 300s) this sweeper
// fails `pending` rows older than ARTIFACT_PENDING_TTL_S (default 24h) with an
// `artifact.upload_failed` event (reason `abandoned`). The TTL is far beyond
// any legitimate upload window, so an in-flight upload can never be swept.

export interface ArtifactSweeperConfig {
  intervalS: number;
  pendingTtlS: number;
}

export function readArtifactSweeperConfig(env: NodeJS.ProcessEnv = process.env): ArtifactSweeperConfig {
  const num = (v: string | undefined, def: number): number => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : def;
  };
  return {
    intervalS: num(env.ARTIFACT_SWEEP_INTERVAL_S, 300),
    pendingTtlS: num(env.ARTIFACT_PENDING_TTL_S, 86400),
  };
}

/** One sweep pass; exported for tests. Returns the number of rows failed. */
export async function sweepStalePendingArtifacts(
  pool: Pool,
  cfg: ArtifactSweeperConfig = readArtifactSweeperConfig(),
): Promise<number> {
  const client = await pool.connect();
  let failed = 0;
  try {
    const { rows } = await client.query(
      `SELECT id, project_id, filename
         FROM artifacts
        WHERE status = 'pending'
          AND created_at < now() - ($1 || ' seconds')::interval
        ORDER BY created_at ASC
        LIMIT 500`,
      [String(cfg.pendingTtlS)],
    );
    for (const row of rows) {
      try {
        await client.query('BEGIN');
        const upd = await client.query(
          `UPDATE artifacts SET status = 'failed'
            WHERE id = $1 AND status = 'pending'`,
          [row.id],
        );
        if (upd.rowCount === 0) {
          await client.query('ROLLBACK');
          continue; // won the race elsewhere (upload completed concurrently)
        }
        // Transactional (cf. hostSweeper): insert inside the tx, publish
        // only after commit so the journal and state cannot diverge.
        const eventRow = await insertEvent(client, {
          type: 'artifact.upload_failed',
          actor_type: 'system',
          actor_id: null,
          payload: {
            artifact_id: row.id,
            project_id: row.project_id,
            filename: row.filename,
            reason: 'abandoned',
          },
        });
        await client.query('COMMIT');
        publishEvent(eventRow);
        failed += 1;
      } catch (err) {
        await client.query('ROLLBACK').catch(() => {});
        logger.warn('artifact sweeper row failed', { artifact: row.id, err: String(err) });
      }
    }
  } finally {
    client.release();
  }
  if (failed > 0) logger.info('artifact sweeper failed stale pending rows', { failed });
  return failed;
}

export function startArtifactSweeper(
  pool: Pool,
  cfg: ArtifactSweeperConfig = readArtifactSweeperConfig(),
): () => void {
  const timer = setInterval(() => {
    sweepStalePendingArtifacts(pool, cfg).catch((err) => {
      logger.warn('artifact sweeper pass failed', { err: String(err) });
    });
  }, cfg.intervalS * 1000);
  timer.unref?.();
  logger.info('artifact sweeper started', { ...cfg });
  return () => clearInterval(timer);
}
