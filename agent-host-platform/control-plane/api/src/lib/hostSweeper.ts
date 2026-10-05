import type { Pool } from 'pg';
import { appendEvent } from './events';
import { logger } from './log';

// Stale-host sweeper: every HEARTBEAT_SWEEP_INTERVAL_S (default 30s) the
// control plane marks hosts `degraded` when their last heartbeat is older
// than HEARTBEAT_DEGRADED_AFTER_S (default 90s) and `offline` when older
// than HEARTBEAT_OFFLINE_AFTER_S (default 300s), emitting host.degraded /
// host.offline events on each transition.
//
// Hosts in `draining` are operator-owned: the sweeper never touches them —
// a draining host that stops heartbeating stays draining, it does not flip
// to offline on its own.

export type StaleTransition = 'degraded' | 'offline' | null;

export function staleHostTransition(
  status: string,
  lastSeen: Date,
  now: Date,
  degradedAfterS: number,
  offlineAfterS: number,
): StaleTransition {
  if (status === 'draining') return null; // operator-owned, never auto-flipped
  const ageS = (now.getTime() - lastSeen.getTime()) / 1000;
  if (ageS >= offlineAfterS) {
    return status === 'offline' ? null : 'offline';
  }
  if (ageS >= degradedAfterS) {
    return status === 'online' ? 'degraded' : null;
  }
  return null;
}

export interface SweeperConfig {
  intervalS: number;
  degradedAfterS: number;
  offlineAfterS: number;
}

export function readSweeperConfig(env: NodeJS.ProcessEnv = process.env): SweeperConfig {
  const num = (v: string | undefined, def: number): number => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : def;
  };
  return {
    intervalS: num(env.HEARTBEAT_SWEEP_INTERVAL_S, 30),
    degradedAfterS: num(env.HEARTBEAT_DEGRADED_AFTER_S, 90),
    offlineAfterS: num(env.HEARTBEAT_OFFLINE_AFTER_S, 300),
  };
}

async function sweepOnce(pool: Pool, cfg: SweeperConfig): Promise<void> {
  const now = new Date();
  const { rows } = await pool.query(
    `SELECT id, name, status, last_seen FROM hosts
     WHERE status IN ('online', 'degraded', 'offline')`,
  );
  for (const host of rows) {
    if (!host.last_seen) continue;
    const transition = staleHostTransition(
      host.status,
      new Date(host.last_seen),
      now,
      cfg.degradedAfterS,
      cfg.offlineAfterS,
    );
    if (!transition) continue;
    await pool.query(
      `UPDATE hosts SET status = $2, updated_at = now() WHERE id = $1`,
      [host.id, transition],
    );
    await appendEvent(pool, {
      type: transition === 'degraded' ? 'host.degraded' : 'host.offline',
      actor_type: 'system',
      actor_id: 'host-sweeper',
      host_id: host.id,
      payload: {
        host_name: host.name,
        previous_status: host.status,
        last_seen: host.last_seen,
      },
    });
    logger.info('host marked stale', {
      host: host.name,
      from: host.status,
      to: transition,
    });
  }
}

/** Start the periodic stale-host sweep. Returns a stop function. */
export function startHostSweeper(pool: Pool, cfg: SweeperConfig = readSweeperConfig()): () => void {
  const timer = setInterval(() => {
    sweepOnce(pool, cfg).catch((err) => {
      logger.warn('host sweeper pass failed', { err: String(err) });
    });
  }, cfg.intervalS * 1000);
  timer.unref?.();
  logger.info('host sweeper started', { ...cfg });
  return () => clearInterval(timer);
}

export { sweepOnce };
