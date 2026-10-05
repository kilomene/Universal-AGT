// Rollback target selection — pure function, no database.
import { describe, expect, it } from 'vitest';
import { selectRollbackTarget } from '../src/lib/rollback';

const current = { id: 'dep-cur', project_id: 'proj-1', host_id: 'host-1' };

function dep(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    project_id: 'proj-1',
    host_id: 'host-1',
    version: '1.0.0',
    status: 'running',
    health_status: 'healthy',
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  };
}

describe('selectRollbackTarget', () => {
  it('picks the newest healthy running deployment of the same project+host', () => {
    const target = selectRollbackTarget(current, [
      dep('dep-old', { created_at: '2026-09-01T00:00:00Z' }),
      dep('dep-new', { created_at: '2026-10-02T00:00:00Z' }),
    ]);
    expect(target?.id).toBe('dep-new');
  });
  it('excludes the current deployment itself', () => {
    const target = selectRollbackTarget(current, [
      dep('dep-cur', { created_at: '2026-10-03T00:00:00Z' }),
      dep('dep-old', { created_at: '2026-09-01T00:00:00Z' }),
    ]);
    expect(target?.id).toBe('dep-old');
  });
  it('skips unhealthy, non-running, and other project/host rows', () => {
    const target = selectRollbackTarget(current, [
      dep('dep-unhealthy', { health_status: 'unhealthy', created_at: '2026-10-04T00:00:00Z' }),
      dep('dep-failed', { status: 'failed', created_at: '2026-10-04T00:00:00Z' }),
      dep('dep-other-proj', { project_id: 'proj-2', created_at: '2026-10-04T00:00:00Z' }),
      dep('dep-other-host', { host_id: 'host-9', created_at: '2026-10-04T00:00:00Z' }),
      dep('dep-ok', { created_at: '2026-10-02T00:00:00Z' }),
    ]);
    expect(target?.id).toBe('dep-ok');
  });
  it('returns null when nothing is eligible', () => {
    expect(selectRollbackTarget(current, [])).toBeNull();
    expect(
      selectRollbackTarget(current, [dep('dep-failed', { status: 'failed' })]),
    ).toBeNull();
  });
  it('accepts Date objects for created_at (pg timestamptz)', () => {
    const target = selectRollbackTarget(current, [
      dep('dep-a', { created_at: new Date('2026-10-02T00:00:00Z') }),
    ]);
    expect(target?.id).toBe('dep-a');
  });
});
