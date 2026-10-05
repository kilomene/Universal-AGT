"use strict";
// Shared route helpers.
Object.defineProperty(exports, "__esModule", { value: true });
exports.parseLimit = parseLimit;
exports.isUuid = isUuid;
exports.encodeCursor = encodeCursor;
exports.decodeCursor = decodeCursor;
exports.publicAgent = publicAgent;
exports.publicHost = publicHost;
function parseLimit(raw, def = 50, max = 500) {
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0)
        return def;
    return Math.min(Math.floor(n), max);
}
function isUuid(s) {
    return (typeof s === 'string' &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s));
}
// Opaque keyset cursor: base64url(JSON {created_at, id}).
function encodeCursor(createdAt, id) {
    return Buffer.from(JSON.stringify({ created_at: createdAt, id })).toString('base64url');
}
function decodeCursor(cursor) {
    if (typeof cursor !== 'string' || !cursor)
        return null;
    try {
        const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
        if (typeof parsed.created_at === 'string' && isUuid(parsed.id)) {
            return { created_at: parsed.created_at, id: parsed.id };
        }
        return null;
    }
    catch {
        return null;
    }
}
// Strip secret hash columns before serializing rows.
function publicAgent(row) {
    const { api_key_hash: _drop1, ...rest } = row;
    return rest;
}
function publicHost(row) {
    const { token_hash: _drop2, ...rest } = row;
    return rest;
}
