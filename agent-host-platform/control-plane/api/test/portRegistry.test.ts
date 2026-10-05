// Port registry tests.
// Pure parts: isValidServicePort. DB parts: allocatePort /
// releasePortAllocations against pg-mem with a MINIMAL port_allocations
// table (host_id, port, deployment_id + the composite primary key).
// pg-mem cannot load the real schema (pgcrypto, plpgsql), so this validates
// the query logic and the unique(host_id, port) collision behavior.
import { newDb } from 'pg-mem';
import { beforeAll, describe, expect, it } from 'vitest';
import {
  allocatePort,
  isValidServicePort,
  releasePortAllocations,
  TERMINAL_PORT_RELEASE_STATUSES,
} from '../src/lib/ports';

describe('isValidServicePort', () => {
  it('accepts integers 1-65535', () => {
    expect(isValidServicePort(80)).toBe(true);
    expect(isValidServicePort(65535)).toBe(true);
  });
  it('rejects out-of-range, non-integer, non-number', () => {
    expect(isValidServicePort(0)).toBe(false);
    expect(isValidServicePort(65536)).toBe(false);
    expect(isValidServicePort(80.5)).toBe(false);
    expect(isValidServicePort('8080')).toBe(false);
    expect(isValidServicePort(null)).toBe(false);
    expect(isValidServicePort(undefined)).toBe(false);
  });
});

describe('TERMINAL_PORT_RELEASE_STATUSES', () => {
  it('releases on failed/rolled_back/stopped, keeps running', () => {
    expect(TERMINAL_PORT_RELEASE_STATUSES.has('failed')).toBe(true);
    expect(TERMINAL_PORT_RELEASE_STATUSES.has('rolled_back')).toBe(true);
    expect(TERMINAL_PORT_RELEASE_STATUSES.has('stopped')).toBe(true);
    expect(TERMINAL_PORT_RELEASE_STATUSES.has('running')).toBe(false);
    expect(TERMINAL_PORT_RELEASE_STATUSES.has('requested')).toBe(false);
  });
});

describe('allocatePort / releasePortAllocations against pg-mem', () => {
  let pool: any;

  beforeAll(() => {
    const db = newDb();
    db.public.none(`
      CREATE TABLE port_allocations (
        host_id TEXT NOT NULL,
        port INTEGER NOT NULL,
        deployment_id TEXT NOT NULL,
        allocated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (host_id, port)
      );
    `);
    const pg = db.adapters.createPg();
    pool = new pg.Pool();
  });

  it('allocates a free port', async () => {
    await allocatePort(pool, 'host-1', 8080, 'dep-1');
    const { rows } = await pool.query('SELECT * FROM port_allocations');
    expect(rows).toHaveLength(1);
    expect(rows[0].port).toBe(8080);
  });

  it('second allocation of the same (host, port) fails (409 at the route)', async () => {
    await expect(allocatePort(pool, 'host-1', 8080, 'dep-2')).rejects.toThrow();
    // the original reservation is untouched
    const { rows } = await pool.query(
      'SELECT deployment_id FROM port_allocations WHERE host_id = $1 AND port = $2',
      ['host-1', 8080],
    );
    expect(rows[0].deployment_id).toBe('dep-1');
  });

  it('same port on a different host is fine', async () => {
    await allocatePort(pool, 'host-2', 8080, 'dep-3');
    const { rows } = await pool.query('SELECT count(*)::int AS n FROM port_allocations');
    expect(rows[0].n).toBe(2);
  });

  it('releasePortAllocations frees the reservation for re-allocation', async () => {
    expect(await releasePortAllocations(pool, 'dep-1')).toBe(1);
    await allocatePort(pool, 'host-1', 8080, 'dep-4'); // no longer collides
    expect(await releasePortAllocations(pool, 'dep-unknown')).toBe(0);
  });
});
