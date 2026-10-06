// §11 owner migration: 011_project_ownership_acls.sql.
//
// Runs the REAL migration file against pg-mem in two scenarios:
//   * upgrade: a legacy database (agents + projects with owner NAMES)
//     -> owner_agent_id backfilled where resolvable; unresolvable owners
//     recorded in migration_reports and left ownerless (never silently
//     assigned); re-running is a no-op (idempotent).
//   * clean: an empty database -> all objects created, no report rows.
import { readFileSync } from 'fs';
import { join } from 'path';
import { randomUUID } from 'crypto';
import { newDb } from 'pg-mem';
import { describe, expect, it } from 'vitest';

const MIGRATION_FILE = join(
  __dirname,
  '..',
  '..',
  '..',
  'database',
  'migrations',
  '011_project_ownership_acls.sql',
);

// Quote-aware statement splitter: splits on semicolons that are not
// inside single-quoted string literals (the migration has one in a
// 'note' string). Full-line comments are stripped first.
function migrationStatements(): string[] {
  const sql = readFileSync(MIGRATION_FILE, 'utf8');
  const stripped = sql
    .split('\n')
    .filter((line) => !/^\s*--/.test(line))
    .join('\n');
  const out: string[] = [];
  let cur = '';
  let inStr = false;
  for (let i = 0; i < stripped.length; i++) {
    const ch = stripped[i];
    if (ch === "'") {
      if (inStr && stripped[i + 1] === "'") {
        cur += "''";
        i++;
        continue;
      }
      inStr = !inStr;
      cur += ch;
      continue;
    }
    if (ch === ';' && !inStr) {
      if (cur.trim()) out.push(cur.trim());
      cur = '';
      continue;
    }
    cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

// Minimal legacy schema: what an installation looks like BEFORE 011
// (uuid PKs, like the real 001_initial.sql).
function legacySchema(db: ReturnType<typeof newDb>) {
  db.public.none(`
    CREATE TABLE agents (
      id uuid PRIMARY KEY, name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE hosts (
      id uuid PRIMARY KEY, name TEXT NOT NULL
    );
    CREATE TABLE projects (
      id uuid PRIMARY KEY, name TEXT NOT NULL UNIQUE,
      owner TEXT
    );
  `);
  // pg-mem does not implement jsonb_build_object (real PostgreSQL does).
  // Shim the exact 6-argument call shape the migration uses, test-only.
  db.public.registerFunction({
    name: 'jsonb_build_object',
    args: ['text', 'text', 'text', 'bool', 'text', 'text'],
    returns: 'jsonb',
    implementation: (k1: string, v1: string, k2: string, v2: boolean, k3: string, v3: string) =>
      ({ [k1]: v1, [k2]: v2, [k3]: v3 }) as never,
  });
}

async function runStatements(db: ReturnType<typeof newDb>, stmts: string[]) {
  const pg = db.adapters.createPg();
  const pool = new pg.Pool();
  try {
    for (const stmt of stmts) {
      await pool.query(stmt);
    }
  } finally {
    await pool.end();
  }
}

function applyMigration(db: ReturnType<typeof newDb>) {
  return runStatements(db, migrationStatements());
}

// Idempotency re-run, pg-mem edition: re-running CREATE TABLE IF NOT EXISTS
// for a table that already exists AND carries FOREIGN KEY constraints hits a
// pg-mem planner limitation ("Not supported") — real PostgreSQL simply
// skips it. The semantically meaningful idempotency (no duplicate report
// rows, stable backfill) lives in the DML tail, so the re-run check
// exercises exactly those statements.
function dmlStatements(): string[] {
  return migrationStatements().filter((s) => /^(UPDATE|INSERT)\b/i.test(s));
}

describe('§11 migration 011_project_ownership_acls', () => {
  it('backfills resolvable owners, records unresolvable ones, and is idempotent', async () => {
    const db = newDb();
    legacySchema(db);

    // Seed legacy rows via the direct API (literals, no parameters).
    const alice = randomUUID();
    const bob = randomUUID();
    const p1 = randomUUID();
    const p2 = randomUUID();
    const p3 = randomUUID();
    db.public.none(
      `INSERT INTO agents (id, name) VALUES ('${alice}','alice'),('${bob}','bob')`,
    );
    db.public.none(
      `INSERT INTO projects (id, name, owner) VALUES ` +
        `('${p1}','proj-one','alice'),('${p2}','proj-two','ghost'),('${p3}','proj-three',NULL)`,
    );

    await applyMigration(db);

    const check = db.adapters.createPg();
    const cpool = new check.Pool();
    try {
      const owners = await cpool.query(
        'SELECT id, owner_agent_id FROM projects ORDER BY name',
      );
      const byId = Object.fromEntries(owners.rows.map((r: any) => [r.id, r.owner_agent_id]));
      // Resolvable legacy name -> backfilled.
      expect(byId[p1]).toBe(alice);
      // Unresolvable legacy name -> left ownerless, NEVER silently assigned.
      expect(byId[p2]).toBeNull();
      expect(byId[p2]).not.toBe(bob);
      // No legacy owner -> untouched.
      expect(byId[p3]).toBeNull();

      // The unresolvable row is documented in migration_reports.
      const reports = await cpool.query(
        `SELECT project_id, detail FROM migration_reports
         WHERE migration = '011_project_ownership_acls'`,
      );
      expect(reports.rows.length).toBe(1);
      expect(reports.rows[0].project_id).toBe(p2);
      expect(reports.rows[0].detail.legacy_owner).toBe('ghost');
      expect(reports.rows[0].detail.resolved).toBe(false);

      // ACL tables exist.
      await cpool.query('SELECT 1 FROM project_members LIMIT 0');
      await cpool.query('SELECT 1 FROM agent_host_access LIMIT 0');
    } finally {
      await cpool.end();
    }

    // Idempotent: a second run changes nothing and duplicates no report rows.
    // (DML tail only — see dmlStatements for why the DDL head is excluded
    // under pg-mem; on real PostgreSQL the whole file re-runs cleanly.)
    await runStatements(db, dmlStatements());
    const check2 = db.adapters.createPg();
    const cpool2 = new check2.Pool();
    try {
      const reports2 = await cpool2.query(
        `SELECT count(*)::int AS n FROM migration_reports
         WHERE migration = '011_project_ownership_acls'`,
      );
      expect(reports2.rows[0].n).toBe(1);
      const owners2 = await cpool2.query('SELECT owner_agent_id FROM projects WHERE id = $1', [p1]);
      expect(owners2.rows[0].owner_agent_id).toBe(alice);
    } finally {
      await cpool2.end();
    }
  });

  it('applies cleanly on an empty database', async () => {
    const db = newDb();
    legacySchema(db);
    await applyMigration(db);
    const pg = db.adapters.createPg();
    const pool = new pg.Pool();
    try {
      const cols = await pool.query(
        `SELECT column_name FROM information_schema.columns
         WHERE table_name = 'projects' AND column_name = 'owner_agent_id'`,
      );
      expect(cols.rows.length).toBe(1);
      const reports = await pool.query('SELECT count(*)::int AS n FROM migration_reports');
      expect(reports.rows[0].n).toBe(0);
      await pool.query('SELECT 1 FROM project_members LIMIT 0');
      await pool.query('SELECT 1 FROM agent_host_access LIMIT 0');
    } finally {
      await pool.end();
    }
  });
});
