"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.eventsRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const auth_1 = require("../middleware/auth");
const _helpers_1 = require("./_helpers");
exports.eventsRouter = (0, express_1.Router)();
const EVENT_COLUMNS = 'id, type, actor_type, actor_id, task_id, deployment_id, host_id, payload, created_at';
// GET /v1/events?type=&since=&limit=&cursor= — cursor = last seen event id
// (ascending keyset pagination). Agent, read_status.
exports.eventsRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        const { type, since, cursor } = req.query;
        const limit = (0, _helpers_1.parseLimit)(req.query.limit);
        const conds = [];
        const params = [];
        if (typeof type === 'string' && type) {
            params.push(type);
            conds.push(`type = $${params.length}`);
        }
        if (typeof since === 'string' && since) {
            const d = new Date(since);
            if (Number.isNaN(d.getTime())) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'since must be an ISO-8601 timestamp');
                return;
            }
            params.push(d.toISOString());
            conds.push(`created_at >= $${params.length}`);
        }
        if (cursor !== undefined) {
            const id = Number(cursor);
            if (!Number.isInteger(id) || id < 0) {
                (0, errors_1.sendError)(res, 400, 'bad_request', 'cursor must be an event id');
                return;
            }
            params.push(id);
            conds.push(`id > $${params.length}`);
        }
        const where = conds.length ? 'WHERE ' + conds.join(' AND ') : '';
        params.push(limit + 1);
        const pool = (0, pool_1.getPool)();
        const { rows } = await pool.query(`SELECT ${EVENT_COLUMNS} FROM events ${where} ORDER BY id ASC LIMIT $${params.length}`, params);
        const hasMore = rows.length > limit;
        const page = hasMore ? rows.slice(0, limit) : rows;
        res.json({
            events: page,
            next_cursor: hasMore ? String(page[page.length - 1].id) : null,
        });
    }
    catch (err) {
        next(err);
    }
});
// GET /v1/events/stream — SSE. Agent auth. Replays the last ?limit=N events,
// then live-pushes new ones ("data: {event}\n\n" per event).
exports.eventsRouter.get('/stream', auth_1.requireAgent, (0, auth_1.requirePermission)('read_status'), async (req, res, next) => {
    try {
        const limit = Math.min((0, _helpers_1.parseLimit)(req.query.limit), 500);
        const pool = (0, pool_1.getPool)();
        res.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            Connection: 'keep-alive',
            'X-Accel-Buffering': 'no',
        });
        res.write(': connected\n\n');
        const send = (event) => {
            res.write(`data: ${JSON.stringify(event)}\n\n`);
        };
        // Replay history first.
        const { rows } = await pool.query(`SELECT ${EVENT_COLUMNS} FROM events ORDER BY id DESC LIMIT $1`, [limit]);
        for (const row of [...rows].reverse())
            send(row);
        let closed = false;
        const unsubscribe = (0, events_1.onLiveEvent)((event) => {
            if (!closed)
                send(event);
        });
        // Keep-alive comment so proxies don't idle-close the stream.
        const keepalive = setInterval(() => {
            if (!closed)
                res.write(': ping\n\n');
        }, 15000);
        keepalive.unref?.();
        const cleanup = () => {
            if (closed)
                return;
            closed = true;
            clearInterval(keepalive);
            unsubscribe();
            log_1.logger.debug('sse stream closed', { agent: req.auth.name });
        };
        req.on('close', cleanup);
        res.on('close', cleanup);
    }
    catch (err) {
        next(err);
    }
});
