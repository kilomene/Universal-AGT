import type { Pool } from 'pg';
import { canonicalStringify } from './canonical';

// Idempotency decision logic (PROTOCOL §2), kept as pure functions so the
// replay/conflict decision is unit-testable without a database.
//
// Rule: re-sending the same idempotency_key with an EQUAL body -> replay the
// original object (HTTP 200 + idempotent_replay:true).
// Same key with a DIFFERENT body -> HTTP 409 conflict.

export type IdempotencyDecision = 'create' | 'replay' | 'conflict';

export function decideIdempotency(
  existing: { body: unknown } | null,
  incomingBody: unknown,
): IdempotencyDecision {
  if (!existing) return 'create';
  return canonicalStringify(existing.body) === canonicalStringify(incomingBody)
    ? 'replay'
    : 'conflict';
}

// The "body" compared for task idempotency: type + payload (the fields that
// define what work was requested; routing/priority metadata excluded).
export function taskIdempotencyBody(input: { type: string; payload: unknown }): unknown {
  return { type: input.type, payload: input.payload ?? {} };
}

// The "body" compared for deployment idempotency: the requested deployment
// parameters (dedupes across BOTH the deployment row and its deploy task).
export function deploymentIdempotencyBody(input: {
  project_id: string;
  host_id?: string | null;
  version: string;
  artifact_id?: string | null;
  mode?: string;
}): unknown {
  return {
    project_id: input.project_id,
    host_id: input.host_id ?? null,
    version: input.version,
    artifact_id: input.artifact_id ?? null,
    mode: input.mode ?? 'automatic',
  };
}

export interface TaskIdempotencyRow {
  id: string;
  type: string;
  payload: unknown;
}

export async function findTaskByIdempotencyKey(
  pool: Pool,
  key: string,
): Promise<TaskIdempotencyRow | null> {
  const { rows } = await pool.query(
    'SELECT id, type, payload FROM tasks WHERE idempotency_key = $1',
    [key],
  );
  return rows[0] ?? null;
}
