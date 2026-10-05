"use strict";
// Retry policy for failed / stuck tasks — PURE functions, zero DB.
//
// Classification of the 17 PROTOCOL §3.2 task types:
//
//   safe        — idempotent reads/status: always safe to retry.
//   conditional — mutating but bounded: retryable ONLY when the previous
//                 attempt never reached `running` (failed in `claimed`).
//                 A task that reached `running` may have partially applied
//                 its mutation, so a blind retry is unsafe.
//   never       — destructive or approval-gated flows: remove, rollback.
//                 Never auto-retried; a human re-issues them.
//
// On a worker-reported `failed`, the control plane computes
// decideRetryOutcome(): `retry` moves the task to `retrying` (the sweeper
// later returns it to `queued`); `fail` moves it to terminal `failed`.
// `attempts` is incremented on every claim, so the check `attempts >=
// max_attempts` is the hard stop in both cases.
Object.defineProperty(exports, "__esModule", { value: true });
exports.classifyTaskType = classifyTaskType;
exports.decideRetryOutcome = decideRetryOutcome;
// Idempotent reads / status queries — re-running them is always harmless.
const SAFE_TYPES = new Set([
    'logs',
    'status',
    'healthcheck',
    'system-info',
    'artifact-download',
    // Phase 7: reconciles the tunnel route table to the desired state —
    // re-running it is a no-op when nothing changed.
    'ingress-sync',
]);
// Destructive or operator-gated flows — never auto-retried.
const NEVER_TYPES = new Set(['remove', 'rollback']);
function classifyTaskType(type) {
    if (SAFE_TYPES.has(type))
        return 'safe';
    if (NEVER_TYPES.has(type))
        return 'never';
    return 'conditional';
}
function decideRetryOutcome(input) {
    const { type, failedFrom, attempts, maxAttempts } = input;
    if (attempts >= maxAttempts)
        return 'fail';
    const cls = classifyTaskType(type);
    if (cls === 'safe')
        return 'retry';
    if (cls === 'conditional') {
        // Retryable only if the previous attempt never reached `running` —
        // i.e. it failed while still `claimed`, before any mutation ran.
        return failedFrom === 'claimed' ? 'retry' : 'fail';
    }
    return 'fail';
}
