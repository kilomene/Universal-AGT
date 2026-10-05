// Stuck-task sweeper tests — pure decision core, no database.
import { describe, expect, it } from 'vitest';
import {
  readTaskSweeperConfig,
  sweepTaskDecision,
  type SweepTaskRow,
} from '../src/lib/taskSweeper';

function row(overrides: Partial<SweepTaskRow> = {}): SweepTaskRow {
  return {
    id: 'task-1',
    type: 'deploy',
    status: 'claimed',
    attempts: 1,
    max_attempts: 3,
    lease_expires_at: new Date(Date.now() - 60_000), // expired by default
    claimed_by: 'host-1',
    assigned_to: 'host-1',
    ...overrides,
  };
}

describe('readTaskSweeperConfig', () => {
  it('defaults: 60s interval, 600s lease', () => {
    expect(readTaskSweeperConfig({})).toEqual({ intervalS: 60, leaseS: 600 });
  });
  it('reads env overrides', () => {
    expect(
      readTaskSweeperConfig({ TASK_SWEEP_INTERVAL_S: '15', TASK_CLAIM_LEASE_S: '120' }),
    ).toEqual({ intervalS: 15, leaseS: 120 });
  });
  it('ignores non-positive garbage', () => {
    expect(readTaskSweeperConfig({ TASK_CLAIM_LEASE_S: 'nope' }).leaseS).toBe(600);
  });
});

describe('sweepTaskDecision', () => {
  it('retrying -> requeue without consuming an attempt', () => {
    expect(sweepTaskDecision(row({ status: 'retrying' }))).toEqual({
      action: 'requeue',
      incrementAttempts: false,
    });
  });
  it('claimed with expired lease -> requeue, attempt counted', () => {
    expect(sweepTaskDecision(row({ type: 'healthcheck' }))).toEqual({
      action: 'requeue',
      incrementAttempts: true,
    });
  });
  it('claimed with live lease -> untouched', () => {
    const r = row({ lease_expires_at: new Date(Date.now() + 300_000) });
    expect(sweepTaskDecision(r)).toEqual({ action: 'none' });
  });
  it('no lease (pre-migration rows) -> untouched', () => {
    expect(sweepTaskDecision(row({ lease_expires_at: null }))).toEqual({ action: 'none' });
  });
  it('exhausted budget -> fail even for safe types', () => {
    const r = row({ type: 'healthcheck', attempts: 2, max_attempts: 3 });
    // attempts becomes 3 on requeue bookkeeping, which hits the cap -> fail
    expect(sweepTaskDecision(r)).toEqual({ action: 'fail', incrementAttempts: true });
  });
  it('conditional stuck in running -> fail (it may have partially mutated)', () => {
    const r = row({ type: 'deploy', status: 'running', attempts: 0 });
    expect(sweepTaskDecision(r)).toEqual({ action: 'fail', incrementAttempts: true });
  });
  it('conditional stuck in claimed -> requeue (never reached running)', () => {
    const r = row({ type: 'deploy', status: 'claimed', attempts: 0 });
    expect(sweepTaskDecision(r)).toEqual({ action: 'requeue', incrementAttempts: true });
  });
  it('never-class stuck task -> fail, never requeued', () => {
    const r = row({ type: 'remove', status: 'claimed', attempts: 0 });
    expect(sweepTaskDecision(r)).toEqual({ action: 'fail', incrementAttempts: true });
  });
  it('safe type stuck in running -> requeue', () => {
    const r = row({ type: 'logs', status: 'running', attempts: 0 });
    expect(sweepTaskDecision(r)).toEqual({ action: 'requeue', incrementAttempts: true });
  });
  it('terminal/other statuses -> untouched', () => {
    for (const status of ['queued', 'completed', 'failed', 'cancelled', 'awaiting_approval']) {
      expect(sweepTaskDecision(row({ status }))).toEqual({ action: 'none' });
    }
  });
  it('lease_expires_at accepts ISO strings (pg timestamptz)', () => {
    const r = row({ lease_expires_at: new Date(Date.now() - 1000).toISOString() });
    expect(sweepTaskDecision(r).action).toBe('requeue');
  });
});
