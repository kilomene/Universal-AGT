import { describe, expect, it } from 'vitest';
import { readSweeperConfig, staleHostTransition } from '../src/lib/hostSweeper';

const NOW = new Date('2026-10-05T12:00:00Z');
const secsAgo = (s: number) => new Date(NOW.getTime() - s * 1000);

describe('staleHostTransition', () => {
  it('keeps a fresh online host as-is', () => {
    expect(staleHostTransition('online', secsAgo(10), NOW, 90, 300)).toBeNull();
  });
  it('marks online -> degraded past the degraded threshold', () => {
    expect(staleHostTransition('online', secsAgo(91), NOW, 90, 300)).toBe('degraded');
  });
  it('marks online -> offline past the offline threshold (not degraded)', () => {
    expect(staleHostTransition('online', secsAgo(301), NOW, 90, 300)).toBe('offline');
  });
  it('marks degraded -> offline past the offline threshold', () => {
    expect(staleHostTransition('degraded', secsAgo(400), NOW, 90, 300)).toBe('offline');
  });
  it('does not re-emit for an already-offline host', () => {
    expect(staleHostTransition('offline', secsAgo(9999), NOW, 90, 300)).toBeNull();
  });
  it('does not re-emit for an already-degraded host still within offline window', () => {
    expect(staleHostTransition('degraded', secsAgo(120), NOW, 90, 300)).toBeNull();
  });
  it('never touches a draining host, however stale', () => {
    expect(staleHostTransition('draining', secsAgo(9999), NOW, 90, 300)).toBeNull();
    expect(staleHostTransition('draining', secsAgo(10), NOW, 90, 300)).toBeNull();
  });
  it('uses custom thresholds', () => {
    expect(staleHostTransition('online', secsAgo(61), NOW, 60, 120)).toBe('degraded');
    expect(staleHostTransition('online', secsAgo(59), NOW, 60, 120)).toBeNull();
    expect(staleHostTransition('online', secsAgo(121), NOW, 60, 120)).toBe('offline');
  });
  it('boundary: exactly at the threshold counts as stale', () => {
    expect(staleHostTransition('online', secsAgo(90), NOW, 90, 300)).toBe('degraded');
    expect(staleHostTransition('online', secsAgo(300), NOW, 90, 300)).toBe('offline');
  });
});

describe('readSweeperConfig', () => {
  it('defaults to 30s interval, 90s degraded, 300s offline', () => {
    expect(readSweeperConfig({})).toEqual({ intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 });
  });
  it('reads env overrides', () => {
    expect(
      readSweeperConfig({
        HEARTBEAT_SWEEP_INTERVAL_S: '10',
        HEARTBEAT_DEGRADED_AFTER_S: '45',
        HEARTBEAT_OFFLINE_AFTER_S: '120',
      }),
    ).toEqual({ intervalS: 10, degradedAfterS: 45, offlineAfterS: 120 });
  });
  it('ignores invalid env values', () => {
    expect(
      readSweeperConfig({
        HEARTBEAT_DEGRADED_AFTER_S: 'not-a-number',
        HEARTBEAT_OFFLINE_AFTER_S: '-5',
      }),
    ).toEqual({ intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 });
  });
});

describe('sweepOnce — state transition and audit event are atomic (W-C)', () => {
  interface FakeClient {
    statements: string[];
    failInsert: boolean;
    query(sql: string, params?: unknown[]): Promise<{ rows: any[] }>;
    release(): void;
  }
  function fakePool(hosts: any[], failInsert = false) {
    const client: FakeClient = {
      statements: [],
      failInsert,
      async query(sql: string, params: unknown[] = []) {
        const verb = sql.trim().split(/\s+/)[0].toUpperCase();
        client.statements.push(verb === 'INSERT' ? 'INSERT events' : verb === 'UPDATE' ? 'UPDATE hosts' : verb);
        if (verb === 'BEGIN' || verb === 'COMMIT' || verb === 'ROLLBACK') return { rows: [] };
        if (verb === 'UPDATE') return { rows: [] };
        if (verb === 'INSERT') {
          if (client.failInsert) throw new Error('db down');
          return {
            rows: [
              {
                id: 7,
                type: params[0],
                actor_type: params[1],
                actor_id: params[2],
                payload: params[6],
                created_at: new Date().toISOString(),
              },
            ],
          };
        }
        throw new Error('unexpected client query: ' + sql);
      },
      release() {},
    };
    const pool = {
      async query(sql: string) {
        if (sql.includes('FROM hosts')) return { rows: hosts };
        throw new Error('unexpected pool query: ' + sql);
      },
      async connect() {
        return client;
      },
    };
    return { pool: pool as any, client };
  }

  const staleHost = (overrides: any = {}) => ({
    id: 'h1',
    name: 'stale-box',
    status: 'online',
    last_seen: new Date(Date.now() - 400_000).toISOString(),
    ...overrides,
  });

  it('commits UPDATE + event INSERT in one transaction, then publishes', async () => {
    const { sweepOnce } = await import('../src/lib/hostSweeper');
    const { pool, client } = fakePool([staleHost()]);
    await sweepOnce(pool, { intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 });
    expect(client.statements).toEqual(['BEGIN', 'UPDATE hosts', 'INSERT events', 'COMMIT']);
  });

  it('rolls back the status flip when the event INSERT fails (no divergent history)', async () => {
    const { sweepOnce } = await import('../src/lib/hostSweeper');
    const { pool, client } = fakePool([staleHost()], true);
    await expect(
      sweepOnce(pool, { intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 }),
    ).rejects.toThrow('db down');
    expect(client.statements).toEqual(['BEGIN', 'UPDATE hosts', 'INSERT events', 'ROLLBACK']);
    expect(client.statements).not.toContain('COMMIT');
  });

  it('does not open a transaction for hosts with no transition', async () => {
    const { sweepOnce } = await import('../src/lib/hostSweeper');
    const { pool, client } = fakePool([staleHost({ last_seen: new Date().toISOString() })]);
    await sweepOnce(pool, { intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 });
    expect(client.statements).toEqual([]);
  });
});
