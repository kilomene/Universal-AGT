"use strict";
// Canonical JSON: deterministic key-sorted serialization for
// idempotency payload comparison (order-insensitive deep equality).
Object.defineProperty(exports, "__esModule", { value: true });
exports.canonicalStringify = canonicalStringify;
exports.deepEqual = deepEqual;
function canonicalStringify(value) {
    if (value === null || value === undefined)
        return 'null';
    if (Array.isArray(value)) {
        return '[' + value.map(canonicalStringify).join(',') + ']';
    }
    if (typeof value === 'object') {
        const keys = Object.keys(value).sort();
        return ('{' +
            keys
                .map((k) => JSON.stringify(k) + ':' + canonicalStringify(value[k]))
                .join(',') +
            '}');
    }
    return JSON.stringify(value) ?? 'null';
}
function deepEqual(a, b) {
    return canonicalStringify(a) === canonicalStringify(b);
}
