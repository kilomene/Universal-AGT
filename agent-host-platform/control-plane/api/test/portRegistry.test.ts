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
  settleRollbackPorts,
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

  it('concurrent allocations of the same fixed port: exactly one succeeds', async () => {
    // The PRIMARY KEY (host_id, port) is the serialization point: two
    // simultaneous INSERTs cannot both win, so exactly one caller gets
    // the reservation and the other gets a unique violation (409 at the
    // route). This is the atomicity guarantee for fixed-port requests.
    const attempts = await Promise.allSettled([
      allocatePort(pool, 'host-9', 9090, 'dep-a'),
      allocatePort(pool, 'host-9', 9090, 'dep-b'),
    ]);
    const won = attempts.filter((a) => a.status === 'fulfilled');
    const lost = attempts.filter((a) => a.status === 'rejected');
    expect(won).toHaveLength(1);
    expect(lost).toHaveLength(1);
    // the winner holds the reservation; the loser changed nothing
    const { rows } = await pool.query(
      'SELECT deployment_id FROM port_allocations WHERE host_id = $1 AND port = $2',
      ['host-9', 9090],
    );
    expect(rows).toHaveLength(1);
    expect(['dep-a', 'dep-b']).toContain(rows[0].deployment_id);
  });

  it('concurrent allocations of different ports both succeed', async () => {
    const attempts = await Promise.allSettled([
      allocatePort(pool, 'host-9', 9091, 'dep-a'),
      allocatePort(pool, 'host-9', 9092, 'dep-b'),
    ]);
    expect(attempts.every((a) => a.status === 'fulfilled')).toBe(true);
  });
});

describe('settleRollbackPorts against pg-mem', () => {
  let pool: any;

  beforeAll(() => {
    const db = newDb();
    db.public.none(`
      CREATE TABLE deployments (
        id TEXT PRIMARY KEY,
        host_id TEXT,
        ports JSONB,
      reserved_cpu DOUBLE PRECISION, reserved_ram_mb DOUBLE PRECISION
      );
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

  async function seed() {
    await pool.query('DELETE FROM port_allocations');
    await pool.query('DELETE FROM deployments');
    // dep-old ran on 8080 (its reservation was dropped at supersede time);
    // dep-new (rolled back) holds the temp port 8081.
    await pool.query(
      `INSERT INTO deployments (id, host_id, ports) VALUES
       ('dep-old', 'host-1', '{"8080": 3000}'::jsonb),
       ('dep-new', 'host-1', '{"8081": 3000}'::jsonb)`,
    );
    await pool.query(
      `INSERT INTO port_allocations (host_id, port, deployment_id)
       VALUES ('host-1', 8081, 'dep-new')`,
    );
  }

  it('releases the temp port and restores the old port to the target', async () => {
    await seed();
    const settlement = await settleRollbackPorts(pool, 'dep-new', 'dep-old');
    expect(settlement).toEqual({ released: 1, restored: 1, skipped: 0, ports: [8080] });
    const { rows } = await pool.query(
      'SELECT port, deployment_id FROM port_allocations ORDER BY port',
    );
    expect(rows).toEqual([{ port: 8080, deployment_id: 'dep-old' }]);
  });

  it('a target port grabbed by someone else is skipped, not an error', async () => {
    await seed();
    // someone re-allocated 8080 between supersede and rollback
    await pool.query(
      `INSERT INTO port_allocations (host_id, port, deployment_id)
       VALUES ('host-1', 8080, 'dep-other')`,
    );
    const settlement = await settleRollbackPorts(pool, 'dep-new', 'dep-old');
    expect(settlement.released).toBe(1);
    expect(settlement.restored).toBe(0);
    expect(settlement.skipped).toBe(1);
    expect(settlement.ports).toEqual([]);
    // the conflict never clobbers the current holder and never duplicates
    const { rows } = await pool.query(
      'SELECT port, deployment_id FROM port_allocations ORDER BY port',
    );
    expect(rows).toEqual([{ port: 8080, deployment_id: 'dep-other' }]);
  });

  it('target with no recorded ports only releases', async () => {
    await pool.query('DELETE FROM port_allocations');
    await pool.query('DELETE FROM deployments');
    await pool.query(
      `INSERT INTO deployments (id, host_id, ports) VALUES
       ('dep-old', 'host-1', NULL),
       ('dep-new', 'host-1', '{"8081": 3000}'::jsonb)`,
    );
    await pool.query(
      `INSERT INTO port_allocations (host_id, port, deployment_id)
       VALUES ('host-1', 8081, 'dep-new')`,
    );
    const settlement = await settleRollbackPorts(pool, 'dep-new', 'dep-old');
    expect(settlement).toEqual({ released: 1, restored: 0, skipped: 0, ports: [] });
  });

  it('unknown target deployment throws', async () => {
    await expect(settleRollbackPorts(pool, 'dep-new', 'dep-missing')).rejects.toThrow(
      /not found/,
    );
  });
});
