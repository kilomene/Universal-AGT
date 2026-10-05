"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.getPool = getPool;
exports.closePool = closePool;
const pg_1 = require("pg");
const log_1 = require("../lib/log");
let pool = null;
function getPool() {
    if (pool)
        return pool;
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) {
        throw new Error('DATABASE_URL is not set');
    }
    pool = new pg_1.Pool({
        connectionString,
        max: Number(process.env.PG_POOL_MAX ?? 10),
    });
    pool.on('error', (err) => {
        log_1.logger.error('pg pool error', { err: String(err) });
    });
    return pool;
}
async function closePool() {
    if (pool) {
        await pool.end();
        pool = null;
    }
}
