"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.staleHostTransition = staleHostTransition;
exports.readSweeperConfig = readSweeperConfig;
exports.startHostSweeper = startHostSweeper;
exports.sweepOnce = sweepOnce;
const events_1 = require("./events");
const log_1 = require("./log");
function staleHostTransition(status, lastSeen, now, degradedAfterS, offlineAfterS) {
    if (status === 'draining')
        return null; // operator-owned, never auto-flipped
    const ageS = (now.getTime() - lastSeen.getTime()) / 1000;
    if (ageS >= offlineAfterS) {
        return status === 'offline' ? null : 'offline';
    }
    if (ageS >= degradedAfterS) {
        return status === 'online' ? 'degraded' : null;
    }
    return null;
}
function readSweeperConfig(env = process.env) {
    const num = (v, def) => {
        const n = Number(v);
        return Number.isFinite(n) && n > 0 ? n : def;
    };
    return {
        intervalS: num(env.HEARTBEAT_SWEEP_INTERVAL_S, 30),
        degradedAfterS: num(env.HEARTBEAT_DEGRADED_AFTER_S, 90),
        offlineAfterS: num(env.HEARTBEAT_OFFLINE_AFTER_S, 300),
    };
}
async function sweepOnce(pool, cfg) {
    const now = new Date();
    const { rows } = await pool.query(`SELECT id, name, status, last_seen FROM hosts
     WHERE status IN ('online', 'degraded', 'offline')`);
    for (const host of rows) {
        if (!host.last_seen)
            continue;
        const transition = staleHostTransition(host.status, new Date(host.last_seen), now, cfg.degradedAfterS, cfg.offlineAfterS);
        if (!transition)
            continue;
        await pool.query(`UPDATE hosts SET status = $2, updated_at = now() WHERE id = $1`, [host.id, transition]);
        await (0, events_1.appendEvent)(pool, {
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
        log_1.logger.info('host marked stale', {
            host: host.name,
            from: host.status,
            to: transition,
        });
    }
}
/** Start the periodic stale-host sweep. Returns a stop function. */
function startHostSweeper(pool, cfg = readSweeperConfig()) {
    const timer = setInterval(() => {
        sweepOnce(pool, cfg).catch((err) => {
            log_1.logger.warn('host sweeper pass failed', { err: String(err) });
        });
    }, cfg.intervalS * 1000);
    timer.unref?.();
    log_1.logger.info('host sweeper started', { ...cfg });
    return () => clearInterval(timer);
}
