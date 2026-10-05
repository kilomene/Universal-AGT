"use strict";
// Structured JSON logging: exactly one JSON object per line.
// Shape: { ts, level, msg, ...fields }
Object.defineProperty(exports, "__esModule", { value: true });
exports.logger = void 0;
const LEVEL_ORDER = { debug: 0, info: 1, warn: 2, error: 3 };
const MIN_LEVEL = process.env.LOG_LEVEL || 'info';
function log(level, msg, fields = {}) {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[MIN_LEVEL])
        return;
    const line = JSON.stringify({ ts: new Date().toISOString(), level, msg, ...fields });
    // eslint-disable-next-line no-console
    console.log(line);
}
exports.logger = {
    debug: (msg, fields) => log('debug', msg, fields),
    info: (msg, fields) => log('info', msg, fields),
    warn: (msg, fields) => log('warn', msg, fields),
    error: (msg, fields) => log('error', msg, fields),
};
