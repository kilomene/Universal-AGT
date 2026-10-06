-- ============================================================================
-- 011_project_ownership_acls.sql
--
-- Resource-level authorization for agent project / deployment ACLs
-- (spec §10) and the legacy owner-name migration (spec §11).
--
-- What this migration does:
--   1. Adds projects.owner_agent_id (uuid REFERENCES agents(id)) — the
--      explicit owner of a project. The legacy `owner` TEXT column
--      (agent NAME) is kept untouched for backwards compatibility.
--   2. Backfills owner_agent_id from the legacy owner name wherever the
--      name resolves to an existing agent row.
--   3. Records every project whose legacy owner could NOT be resolved in
--      migration_reports (it is NOT silently assigned to a random agent —
--      those projects stay ownerless and legacy-open; see below).
--   4. Creates project_members (project ACL table) and agent_host_access
--      (per-host targeting ACL for agents).
--
-- Authorization semantics (enforced by the API, src/lib/authz.ts):
--   * a project is accessible to its owner (owner_agent_id), to explicit
--     project_members rows, and — for backwards compatibility — to every
--     agent when owner_agent_id IS NULL (pre-ACL rows, including the
--     unresolvable legacy rows recorded below). Existing installations
--     keep working; assign an owner via POST /v1/projects/:id/owner
--     (operator auth) to close an ownerless project down.
--   * a host may be explicitly targeted by an agent when an
--     agent_host_access row exists for the pair, or — for backwards
--     compatibility — when the host has NO agent_host_access rows at all.
--     Adding the first row for a host switches that host to strict mode.
--
-- Idempotent: every statement is guarded (IF NOT EXISTS / guarded UPDATE /
-- guarded INSERT), so re-running this file is a safe no-op.
-- ============================================================================

-- 1. Explicit project ownership.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS owner_agent_id uuid REFERENCES agents(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_projects_owner_agent ON projects(owner_agent_id);

-- 4a. Project membership (ACL table): grants a non-owner agent access to a
--     project. The agent's own permission map still gates which actions it
--     may take — membership is about *which* projects, not *what* actions.
CREATE TABLE IF NOT EXISTS project_members (
    project_id uuid NOT NULL,
    agent_id   uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, agent_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_members_agent ON project_members(agent_id);

-- 4b. Per-host targeting ACL for agents.
CREATE TABLE IF NOT EXISTS agent_host_access (
    agent_id    uuid NOT NULL,
    host_id     uuid NOT NULL,
    permissions jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, host_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_host_access_host ON agent_host_access(host_id);

-- 3. Durable record of migration outcomes (spec §11: document, don't guess).
CREATE TABLE IF NOT EXISTS migration_reports (
    id          bigserial PRIMARY KEY,
    migration   text NOT NULL,
    project_id  uuid,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (migration, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_migration_reports_migration ON migration_reports(migration);

-- 2. Backfill owner_agent_id from the legacy owner NAME where it resolves
--    to a live agent row. Guarded so re-runs only fill still-empty rows.
UPDATE projects
SET owner_agent_id = agents.id
FROM agents
WHERE projects.owner_agent_id IS NULL
  AND projects.owner IS NOT NULL
  AND agents.name = projects.owner;

-- 3. Record the legacy rows that could NOT be resolved. They are deliberately
--    left ownerless (owner_agent_id stays NULL) — never silently assigned to
--    a random agent. Recovery path: POST /v1/projects/:id/owner with
--    operator authentication assigns the rightful owner.
--    ON CONFLICT DO NOTHING keeps re-runs from duplicating report rows.
INSERT INTO migration_reports (migration, project_id, detail)
SELECT '011_project_ownership_acls',
       id,
       jsonb_build_object(
           'legacy_owner', owner,
           'resolved', false,
           'note', 'legacy owner name did not match any agent; project left ownerless (legacy-open). Assign an owner via POST /v1/projects/:id/owner.'
       )
FROM projects
WHERE owner IS NOT NULL
  AND owner_agent_id IS NULL
ON CONFLICT (migration, project_id) DO NOTHING;
