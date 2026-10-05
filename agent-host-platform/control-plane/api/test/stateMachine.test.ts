import { describe, expect, it } from 'vitest';
import {
  allowedTaskTransitions,
  canTransitionTask,
  isTaskStatus,
  isTerminalTaskStatus,
} from '../src/lib/stateMachine';

// Mirrors the check constraint in database/schema/schema.sql and PROTOCOL §3.2:
//   queued -> claimed | cancelled
//   claimed -> running | failed | retrying | cancelled
//   running -> completed | failed | retrying | awaiting_approval | cancelled
//   awaiting_approval -> queued | cancelled
//   retrying -> queued
//   completed | failed | cancelled are terminal

describe('allowedTaskTransitions', () => {
  it('queued can go to claimed or cancelled only', () => {
    expect(allowedTaskTransitions('queued').sort()).toEqual(['cancelled', 'claimed']);
  });
  it('claimed can go to running, failed, retrying, or cancelled', () => {
    expect(allowedTaskTransitions('claimed').sort()).toEqual(['cancelled', 'failed', 'retrying', 'running']);
  });
  it('running can complete, fail, retry, await approval, or cancel', () => {
    expect(allowedTaskTransitions('running').sort()).toEqual([
      'awaiting_approval',
      'cancelled',
      'completed',
      'failed',
      'retrying',
    ]);
  });
  it('awaiting_approval can be approved (queued) or rejected (cancelled)', () => {
    expect(allowedTaskTransitions('awaiting_approval').sort()).toEqual(['cancelled', 'queued']);
  });
  it('retrying goes back to queued', () => {
    expect(allowedTaskTransitions('retrying')).toEqual(['queued']);
  });
  it('terminal states have no outgoing transitions', () => {
    for (const s of ['completed', 'failed', 'cancelled']) {
      expect(allowedTaskTransitions(s)).toEqual([]);
    }
  });
  it('unknown status yields no transitions', () => {
    expect(allowedTaskTransitions('exploded')).toEqual([]);
    expect(allowedTaskTransitions('')).toEqual([]);
  });
  it('returns a fresh array each call', () => {
    const a = allowedTaskTransitions('queued');
    a.push('bogus');
    expect(allowedTaskTransitions('queued')).not.toContain('bogus');
  });
});

describe('canTransitionTask', () => {
  const allowed: Array<[string, string]> = [
    ['queued', 'claimed'],
    ['queued', 'cancelled'],
    ['claimed', 'running'],
    ['claimed', 'failed'],
    ['claimed', 'retrying'],
    ['claimed', 'cancelled'],
    ['running', 'completed'],
    ['running', 'failed'],
    ['running', 'retrying'],
    ['running', 'awaiting_approval'],
    ['running', 'cancelled'],
    ['awaiting_approval', 'queued'],
    ['awaiting_approval', 'cancelled'],
    ['retrying', 'queued'],
  ];
  for (const [from, to] of allowed) {
    it(`allows ${from} -> ${to}`, () => {
      expect(canTransitionTask(from, to)).toBe(true);
    });
  }

  const disallowed: Array<[string, string]> = [
    // backwards / skipped steps
    ['claimed', 'queued'],
    ['running', 'claimed'],
    ['running', 'queued'],
    ['completed', 'queued'],
    ['failed', 'retrying'],
    ['cancelled', 'queued'],
    // out of terminal states
    ['completed', 'cancelled'],
    ['failed', 'completed'],
    ['cancelled', 'failed'],
    // self-transitions are not transitions
    ['queued', 'queued'],
    ['running', 'running'],
    // nonsense
    ['queued', 'exploded'],
    ['exploded', 'queued'],
    // manual-mode approval path must go through queued first
    ['awaiting_approval', 'running'],
    ['awaiting_approval', 'completed'],
  ];
  for (const [from, to] of disallowed) {
    it(`rejects ${from} -> ${to}`, () => {
      expect(canTransitionTask(from, to)).toBe(false);
    });
  }
});

describe('status predicates', () => {
  it('recognizes all eight statuses', () => {
    for (const s of ['queued', 'claimed', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled', 'retrying']) {
      expect(isTaskStatus(s)).toBe(true);
    }
    expect(isTaskStatus('done')).toBe(false);
  });
  it('identifies terminal states', () => {
    expect(isTerminalTaskStatus('completed')).toBe(true);
    expect(isTerminalTaskStatus('failed')).toBe(true);
    expect(isTerminalTaskStatus('cancelled')).toBe(true);
    expect(isTerminalTaskStatus('running')).toBe(false);
    expect(isTerminalTaskStatus('awaiting_approval')).toBe(false);
    expect(isTerminalTaskStatus('queued')).toBe(false);
  });
});
