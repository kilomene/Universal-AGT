"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.decideIdempotency = decideIdempotency;
exports.taskIdempotencyBody = taskIdempotencyBody;
exports.deploymentIdempotencyBody = deploymentIdempotencyBody;
exports.findTaskByIdempotencyKey = findTaskByIdempotencyKey;
const canonical_1 = require("./canonical");
function decideIdempotency(existing, incomingBody) {
    if (!existing)
        return 'create';
    return (0, canonical_1.canonicalStringify)(existing.body) === (0, canonical_1.canonicalStringify)(incomingBody)
        ? 'replay'
        : 'conflict';
}
// The "body" compared for task idempotency: type + payload (the fields that
// define what work was requested; routing/priority metadata excluded).
function taskIdempotencyBody(input) {
    return { type: input.type, payload: input.payload ?? {} };
}
// The "body" compared for deployment idempotency: the requested deployment
// parameters (dedupes across BOTH the deployment row and its deploy task).
function deploymentIdempotencyBody(input) {
    return {
        project_id: input.project_id,
        host_id: input.host_id ?? null,
        version: input.version,
        artifact_id: input.artifact_id ?? null,
        mode: input.mode ?? 'automatic',
    };
}
async function findTaskByIdempotencyKey(pool, key) {
    const { rows } = await pool.query('SELECT id, type, payload FROM tasks WHERE idempotency_key = $1', [key]);
    return rows[0] ?? null;
}
