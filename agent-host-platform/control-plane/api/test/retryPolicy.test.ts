// Retry policy tests — pure functions, no database.
import { describe, expect, it } from 'vitest';
import { classifyTaskType, decideRetryOutcome } from '../src/lib/retryPolicy';
import { TASK_TYPES } from '../src/routes/tasks';

// The 17 PROTOCOL §3.2 task types must ALL be classified (no silent gaps).
describe('classifyTaskType — full 17-type matrix', () => {
  it('covers every protocol task type', () => {
    for (const t of TASK_TYPES) {
      expect(['safe', 'conditional', 'never']).toContain(classifyTaskType(t));
    }
  });
  it('safe: idempotent reads/status', () => {
    for (const t of ['logs', 'status', 'healthcheck', 'system-info', 'artifact-download']) {
      expect(classifyTaskType(t)).toBe('safe');
    }
  });
  it('never: destructive / operator-gated flows', () => {
    expect(classifyTaskType('remove')).toBe('never');
    expect(classifyTaskType('rollback')).toBe('never');
  });
  it('conditional: everything mutating but bounded', () => {
    for (const t of [
      'deploy', 'restart', 'stop', 'start', 'build', 'docker-build',
      'docker-run', 'docker-compose', 'environment-update', 'artifact-upload',
    ]) {
      expect(classifyTaskType(t)).toBe('conditional');
    }
  });
  it('unknown types default to conditional (fail-closed, never silent-safe)', () => {
    expect(classifyTaskType('some-future-type')).toBe('conditional');
  });
});

describe('decideRetryOutcome — max_attempts is the hard stop', () => {
  it('attempts >= max_attempts -> fail for every class', () => {
    for (const type of ['healthcheck', 'deploy', 'remove']) {
      expect(
        decideRetryOutcome({ type, failedFrom: 'claimed', attempts: 3, maxAttempts: 3 }),
      ).toBe('fail');
      expect(
        decideRetryOutcome({ type, failedFrom: 'claimed', attempts: 9, maxAttempts: 3 }),
      ).toBe('fail');
    }
  });
  it('safe types retry from any failure point while budget remains', () => {
    for (const failedFrom of ['claimed', 'running']) {
      expect(
        decideRetryOutcome({ type: 'healthcheck', failedFrom, attempts: 1, maxAttempts: 3 }),
      ).toBe('retry');
    }
  });
  it('conditional retries only when the attempt never reached running', () => {
    expect(
      decideRetryOutcome({ type: 'deploy', failedFrom: 'claimed', attempts: 1, maxAttempts: 3 }),
    ).toBe('retry');
    expect(
      decideRetryOutcome({ type: 'deploy', failedFrom: 'running', attempts: 1, maxAttempts: 3 }),
    ).toBe('fail');
    expect(
      decideRetryOutcome({ type: 'restart', failedFrom: 'claimed', attempts: 2, maxAttempts: 3 }),
    ).toBe('retry');
    expect(
      decideRetryOutcome({ type: 'stop', failedFrom: 'running', attempts: 0, maxAttempts: 3 }),
    ).toBe('fail');
  });
  it('never types never retry, even with budget and a claimed failure', () => {
    for (const type of ['remove', 'rollback']) {
      expect(
        decideRetryOutcome({ type, failedFrom: 'claimed', attempts: 0, maxAttempts: 3 }),
      ).toBe('fail');
    }
  });
});
