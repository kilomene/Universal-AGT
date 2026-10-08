import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError } from '../lib/errors';
import { onLiveEvent, type DbEvent } from '../lib/events';
import { logger } from '../lib/log';
import { requireAgent, requirePermission } from '../middleware/auth';
import { parseLimit } from './_helpers';

export const eventsRouter = Router();

const EVENT_COLUMNS =
  'id, type, actor_type, actor_id, task_id, deployment_id, host_id, payload, created_at';

// GET /v1/events?type=&since=&limit=&cursor= — cursor = last seen event id
// (ascending keyset pagination). Agent, read_status.
eventsRouter.get('/', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const { type, since, cursor } = req.query;
    const limit = parseLimit(req.query.limit);
    const conds: string[] = [];
    const params: unknown[] = [];

    if (typeof type === 'string' && type) {
      params.push(type);
      conds.push(`type = $${params.length}`);
    }
    if (typeof since === 'string' && since) {
      const d = new Date(since);
      if (Number.isNaN(d.getTime())) {
        sendError(res, 400, 'bad_request', 'since must be an ISO-8601 timestamp');
        return;
      }
      params.push(d.toISOString());
      conds.push(`created_at >= $${params.length}`);
    }
    if (cursor !== undefined) {
      const id = Number(cursor);
      if (!Number.isInteger(id) || id < 0) {
        sendError(res, 400, 'bad_request', 'cursor must be an event id');
        return;
      }
      params.push(id);
      conds.push(`id > $${params.length}`);
    }
    const where = conds.length ? 'WHERE ' + conds.join(' AND ') : '';
    params.push(limit + 1);

    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT ${EVENT_COLUMNS} FROM events ${where} ORDER BY id ASC LIMIT $${params.length}`,
      params,
    );
    const hasMore = rows.length > limit;
    const page = hasMore ? rows.slice(0, limit) : rows;
    res.json({
      events: page,
      next_cursor: hasMore ? String(page[page.length - 1].id) : null,
    });
  } catch (err) {
    next(err);
  }
});

// GET /v1/events/stream — SSE. Agent auth. Replays history (optionally
// filtered by ?since= ISO-8601 and ?limit=N), then live-pushes new ones
// ("data: {event}\n\n" per event).
eventsRouter.get('/stream', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const limit = Math.min(parseLimit(req.query.limit), 500);
    const pool = getPool();

    // Optional since filter for the replay, mirroring GET /v1/events.
    let sinceIso: string | null = null;
    const { since } = req.query;
    if (typeof since === 'string' && since) {
      const d = new Date(since);
      if (Number.isNaN(d.getTime())) {
        sendError(res, 400, 'bad_request', 'since must be an ISO-8601 timestamp');
        return;
      }
      sinceIso = d.toISOString();
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write(': connected\n\n');

    const send = (event: DbEvent) => {
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    };

    // Replay history first (filtered by since when provided).
    const conds: string[] = [];
    const params: unknown[] = [];
    if (sinceIso) {
      params.push(sinceIso);
      conds.push(`created_at >= $${params.length}`);
    }
    const where = conds.length ? 'WHERE ' + conds.join(' AND ') : '';
    params.push(limit);
    const { rows } = await pool.query(
      `SELECT ${EVENT_COLUMNS} FROM events ${where} ORDER BY id DESC LIMIT $${params.length}`,
      params,
    );
    for (const row of [...rows].reverse()) send(row as DbEvent);

    let closed = false;
    const unsubscribe = onLiveEvent((event) => {
      if (!closed) send(event);
    });

    // Keep-alive comment so proxies don't idle-close the stream.
    const keepalive = setInterval(() => {
      if (!closed) res.write(': ping\n\n');
    }, 15000);
    keepalive.unref?.();

    const cleanup = () => {
      if (closed) return;
      closed = true;
      clearInterval(keepalive);
      unsubscribe();
      logger.debug('sse stream closed', { agent: req.auth!.name });
    };
    req.on('close', cleanup);
    res.on('close', cleanup);
  } catch (err) {
    next(err);
  }
});
