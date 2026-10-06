import { describe, expect, it, vi, beforeEach } from 'vitest';

// Migration runner: no-transaction support.
//
// PostgreSQL forbids CREATE EVENT TRIGGER (migration 005) inside a
// transaction block. A migration whose header contains
// `-- migrate: no-transaction` must run WITHOUT the BEGIN/COMMIT wrapper;
// everything else keeps per-migration transactions.

vi.mock('fs', () => ({
  readdirSync: vi.fn(),
  readFileSync: vi.fn(),
}));

import { readdirSync, readFileSync } from 'fs';
import { runMigrations, requiresNoTransaction } from '../src/db/migrate';

const NO_TX_SQL = `-- 009_example.sql
--
-- migrate: no-transaction
--
-- Synthetic example: PostgreSQL forbids some statements (e.g. CREATE EVENT
-- TRIGGER) inside a transaction block, so the runner supports a
-- no-transaction marker. (Migration 005 previously used this for an event
-- trigger; that approach was removed when PG 16 proved event triggers do
-- not support TRUNCATE at all.)
create index concurrently if not exists idx_example on events(type);
`;

const NO_TX_FILES = { '009_example.sql': NO_TX_SQL };

const NORMAL_SQL = `-- 006_token_rotation_grace.sql
alter table agents add column if not exists foo text;
`;

function mockFs(files: Record<string, string>) {
  vi.mocked(readdirSync).mockImplementation(((p: unknown) => {
    const s = String(p);
    // findMigrationsDir probes <dir>/database/migrations — always "found".
    if (s.endsWith('migrations')) return Object.keys(files) as never;
    return ['migrations'] as never;
  }) as never);
  vi.mocked(readFileSync).mockImplementation(((p: unknown) => {
    const s = String(p);
    for (const [name, sql] of Object.entries(files)) {
      if (s.endsWith(name)) return sql as never;
    }
    throw new Error('unexpected file read: ' + s);
  }) as never);
}

function mockPool() {
  const queries: string[] = [];
  const client = {
    query: vi.fn(async (text: string) => {
      queries.push(String(text).split('\n')[0].slice(0, 80));
      return { rows: [] };
    }),
    release: vi.fn(),
  };
  const pool = {
    query: vi.fn(async (text: string) => {
      queries.push('pool: ' + String(text).split('\n')[0].slice(0, 80));
      if (String(text).includes('is_superuser')) return { rows: [{ super: true }] };
      if (String(text).includes('pg_extension')) return { rows: [{ installed: true }] };
      if (String(text).includes('schema_migrations') && String(text).includes('SELECT'))
        return { rows: [] };
      return { rows: [] };
    }),
    connect: vi.fn(async () => client),
  };
  return { pool: pool as never, queries, client };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('requiresNoTransaction', () => {
  it('detects the marker in the file header', () => {
    expect(requiresNoTransaction(NO_TX_SQL)).toBe(true);
  });

  it('ignores the marker when it appears after the header', () => {
    const sql = Array.from({ length: 12 }, (_, i) => `-- line ${i}`).join('\n') +
      '\n-- migrate: no-transaction\n';
    expect(requiresNoTransaction(sql)).toBe(false);
  });

  it('returns false for ordinary migrations', () => {
    expect(requiresNoTransaction(NORMAL_SQL)).toBe(false);
  });
});

describe('runMigrations no-transaction path', () => {
  it('runs a marked migration WITHOUT BEGIN/COMMIT/ROLLBACK', async () => {
    mockFs(NO_TX_FILES);
    const { pool, queries } = mockPool();
    const applied = await runMigrations(pool as never);
    expect(applied).toEqual(['009_example.sql']);
    const kinds = queries.join('|');
    expect(kinds).not.toMatch(/(^|\|)BEGIN(\||$)/);
    expect(kinds).not.toMatch(/(^|\|)COMMIT(\||$)/);
    expect(kinds).not.toMatch(/(^|\|)ROLLBACK(\||$)/);
    // the migration SQL itself ran, and it was recorded as applied
    expect(queries.some((q) => q.includes('009_example.sql'))).toBe(true);
    expect(queries.some((q) => q.includes('INSERT INTO schema_migrations'))).toBe(true);
  });

  it('keeps the transactional wrapper for ordinary migrations', async () => {
    mockFs({ '006_token_rotation_grace.sql': NORMAL_SQL });
    const { pool, queries } = mockPool();
    const applied = await runMigrations(pool as never);
    expect(applied).toEqual(['006_token_rotation_grace.sql']);
    const kinds = queries.join('|');
    expect(kinds).toMatch(/(^|\|)BEGIN(\||$)/);
    expect(kinds).toMatch(/(^|\|)COMMIT(\||$)/);
    expect(kinds).not.toMatch(/(^|\|)ROLLBACK(\||$)/);
  });

  it('rolls back an ordinary migration on failure and reports the file', async () => {
    mockFs({ '006_token_rotation_grace.sql': NORMAL_SQL });
    const { pool, queries, client } = mockPool();
    client.query.mockImplementationOnce(async () => ({ rows: [] })) // BEGIN
      .mockImplementationOnce(async () => { throw new Error('boom'); }); // migration SQL
    await expect(runMigrations(pool as never)).rejects.toThrow(/migration 006_token_rotation_grace\.sql failed/);
    const kinds = queries.join('|');
    expect(kinds).toMatch(/(^|\|)ROLLBACK(\||$)/);
    expect(kinds).not.toMatch(/INSERT INTO schema_migrations/);
  });
});
