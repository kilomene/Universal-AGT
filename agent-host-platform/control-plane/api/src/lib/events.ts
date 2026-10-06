import { EventEmitter } from 'events';
import type { Pool, PoolClient } from 'pg';
import { logger } from './log';

// Append-only event journal. Rows are INSERTed here and SELECTed by readers;
// the trg_events_no_update_delete trigger in the schema blocks UPDATE/DELETE.
//
// insertEvent performs ONLY the INSERT (it accepts a transaction client so
// state transitions and their events can commit atomically — see
// hostSweeper). publishEvent fans the row out to the live bus. appendEvent
// is the common case: insert + publish in one call. Publish must happen
// AFTER the surrounding transaction commits, or SSE consumers see phantom
// events for rolled-back rows.

export interface NewEvent {
  type: string;
  actor_type?: string | null;
  actor_id?: string | null;
  task_id?: string | null;
  deployment_id?: string | null;
  host_id?: string | null;
  payload?: Record<string, unknown> | null;
}

export interface DbEvent extends NewEvent {
  id: number;
  payload: Record<string, unknown>;
  created_at: string;
}

/** INSERT the event row. Accepts a Pool or a transaction PoolClient. No live publish. */
export async function insertEvent(client: Pool | PoolClient, e: NewEvent): Promise<DbEvent> {
  const { rows } = await client.query(
    `INSERT INTO events (type, actor_type, actor_id, task_id, deployment_id, host_id, payload)
     VALUES ($1,$2,$3,$4,$5,$6,$7)
     RETURNING id, type, actor_type, actor_id, task_id, deployment_id, host_id, payload, created_at`,
    [
      e.type,
      e.actor_type ?? null,
      e.actor_id ?? null,
      e.task_id ?? null,
      e.deployment_id ?? null,
      e.host_id ?? null,
      JSON.stringify(e.payload ?? {}),
    ],
  );
  return rows[0] as DbEvent;
}

/** Fan a committed event row out to the live bus. Call only after commit. */
export function publishEvent(row: DbEvent): void {
  eventBus.emit('event', row);
}

export async function appendEvent(pool: Pool, e: NewEvent): Promise<DbEvent> {
  const row = await insertEvent(pool, e);
  publishEvent(row);
  return row;
}

// ---------------------------------------------------------------------------
// Live event bus for the SSE stream.
// Primary: pg LISTEN/NOTIFY on the 'uag_events' channel (trigger in
// migration 003_events_notify.sql fires pg_notify on every INSERT).
// Backstop: a 5s poll catches anything the listener misses (e.g. trigger
// absent on an older database, or a reconnect gap). Both paths dedupe by id.
// ---------------------------------------------------------------------------

const bus = new EventEmitter();
bus.setMaxListeners(0);
const eventBus = bus;

const seenIds = new Set<number>();
let listenClient: PoolClient | null = null;
let pollTimer: NodeJS.Timeout | null = null;
let busPool: Pool | null = null;
let lastPolledId = 0;

function publish(row: DbEvent): void {
  if (seenIds.has(row.id)) return;
  seenIds.add(row.id);
  if (seenIds.size > 10000) {
    const oldest = [...seenIds].sort((a, b) => a - b).slice(0, 5000);
    for (const id of oldest) seenIds.delete(id);
  }
  if (row.id > lastPolledId) lastPolledId = row.id;
  bus.emit('live', row);
}

export function onLiveEvent(cb: (row: DbEvent) => void): () => void {
  bus.on('live', cb);
  return () => bus.off('live', cb);
}

export async function startEventBus(pool: Pool): Promise<void> {
  busPool = pool;
  // Every event appended through appendEvent is published in-process too,
  // so single-instance deployments never depend on LISTEN.
  bus.on('event', publish);
  try {
    listenClient = await pool.connect();
    listenClient.on('notification', async (msg) => {
      if (msg.channel !== 'uag_events') return;
      const id = Number(msg.payload);
      if (!Number.isFinite(id)) return;
      try {
        const { rows } = await pool.query(
          `SELECT id, type, actor_type, actor_id, task_id, deployment_id, host_id, payload, created_at
           FROM events WHERE id = $1`,
          [id],
        );
        if (rows[0]) publish(rows[0] as DbEvent);
      } catch (err) {
        logger.warn('event notify fetch failed', { id, err: String(err) });
      }
    });
    listenClient.on('error', (err) => {
      logger.warn('event listen client error', { err: String(err) });
    });
    await listenClient.query('LISTEN uag_events');
    logger.info('listening for events on channel uag_events');
  } catch (err) {
    logger.warn('LISTEN uag_events unavailable, SSE will rely on polling', { err: String(err) });
    listenClient = null;
  }
  // Backstop poll (also the primary path when LISTEN is unavailable).
  if (!pollTimer) {
    pollTimer = setInterval(async () => {
      if (!busPool) return;
      try {
        const { rows } = await busPool.query(
          `SELECT id, type, actor_type, actor_id, task_id, deployment_id, host_id, payload, created_at
           FROM events WHERE id > $1 ORDER BY id ASC LIMIT 100`,
          [lastPolledId],
        );
        for (const row of rows) publish(row as DbEvent);
      } catch (err) {
        logger.warn('event backstop poll failed', { err: String(err) });
      }
    }, 5000);
    pollTimer.unref?.();
  }
}

export async function stopEventBus(): Promise<void> {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (listenClient) {
    try {
      await listenClient.query('UNLISTEN uag_events');
    } catch {
      /* ignore */
    }
    listenClient.release();
    listenClient = null;
  }
  bus.removeAllListeners('event');
  bus.removeAllListeners('live');
  busPool = null;
}
