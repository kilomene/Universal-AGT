-- ============================================================================
-- Universal Agent-to-Persistent-Host Deployment System
-- Canonical database schema (PostgreSQL / Supabase)
--
-- This file is the single source of truth for the data model.
-- Migration 001_initial.sql applies this schema from scratch.
-- Later migrations live in database/migrations/ and only ADD changes.
--
-- Design rules (from the system spec):
--   * agents are TEMPORARY workers — rows here are identity + permissions only
--   * the control plane (this database) is the PERSISTENT source of truth
--   * the events table is APPEND-ONLY — enforced by trigger, never UPDATE/DELETE
--   * task creation is IDEMPOTENT via idempotency_key (unique constraint)
--   * no host IP addresses are stored anywhere — hosts are reached only
--     through their own outbound connection to the control plane
-- ============================================================================

-- Enable pgcrypto for UUID generation
create extension if not exists "pgcrypto";

-- ----------------------------------------------------------------------------
-- agents: any compatible autonomous agent (Muse, Instinct, custom scripts, CLI)
-- Agent-neutral: every agent is just a row with identity + scoped permissions.
-- ----------------------------------------------------------------------------
create table agents (
    id            uuid primary key default gen_random_uuid(),
    name          text not null unique,                       -- e.g. 'muse', 'instinct', 'ci-bot'
    type          text not null default 'generic',            -- 'muse' | 'instinct' | 'custom' | 'cli' ...
    capabilities  jsonb not null default '[]'::jsonb,         -- e.g. ["deploy","build_docker"]
    permissions   jsonb not null default '{}'::jsonb,         -- e.g. {"deploy":true,"restart":true,"destroy":false}
    status        text not null default 'active'
                  check (status in ('active','suspended','revoked')),
    api_key_hash  text not null,                              -- SHA-256 hex of the bearer token
    idempotency_key text,                                     -- §7: registration idempotency (migration 010)
    last_seen     timestamptz,
    metadata      jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index idx_agents_status on agents(status);
create unique index idx_agents_idempotency_key on agents (idempotency_key) where idempotency_key is not null;

-- ----------------------------------------------------------------------------
-- hosts: persistent compute nodes. The name is abstract on purpose
-- (e.g. 'persistent-host-01'). NEVER store public/private IPs here.
-- ----------------------------------------------------------------------------
create table hosts (
    id             uuid primary key default gen_random_uuid(),
    name           text not null unique,                      -- e.g. 'persistent-host-01'
    host_type      text not null default 'persistent-linux-host',
    status         text not null default 'offline'
                   check (status in ('online','offline','degraded','draining')),
    capabilities   jsonb not null default '[]'::jsonb,        -- e.g. ["docker","docker-compose","gpu"];
                                                             -- refreshed from the heartbeat payload
    worker_version text,                                      -- reported by the heartbeat
    token_hash     text not null,                             -- SHA-256 hex of the host bearer token
    previous_token_hash text,                            -- SHA-256 hex of the pre-rotation host token
                                                        -- (valid only while previous_token_expires_at
                                                        -- is in the future — rotation grace window)
    previous_token_expires_at timestamptz,               -- grace expiry for previous_token_hash
    cpu_pct        double precision,
    ram_pct        double precision,
    disk_pct       double precision,
    docker_status  text,                                      -- 'ok' | 'missing' | 'error: ...'
    running_apps   jsonb not null default '[]'::jsonb,        -- [{deployment_id, project, status}]
    total_cpu      double precision,                          -- cores available
    total_ram_mb   double precision,
    total_disk_gb  double precision,
    worker_draining boolean not null default false,           -- worker's self-reported drain flag
    worker_status  text,                                      -- 'running' | 'draining', as reported
    ingress        jsonb not null default '{}'::jsonb,        -- {enabled, provider} descriptor
    reported_host_name text,                                  -- worker's configured host name
                                                             -- (hosts.name stays the identity)
    idempotency_key text,                                     -- §7: registration idempotency (migration 010)
    last_seen      timestamptz,
    metadata       jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index idx_hosts_status on hosts(status);
create unique index idx_hosts_idempotency_key on hosts (idempotency_key) where idempotency_key is not null;

-- ----------------------------------------------------------------------------
-- tasks: the durable work queue. This is what survives agents disappearing.
-- Status machine (enforced in API code + check constraint):
--   queued -> claimed | cancelled
--   claimed -> running | failed | retrying | cancelled
--   running -> completed | failed | retrying | awaiting_approval | cancelled
--   awaiting_approval -> queued (approved) | cancelled (rejected)
--   retrying -> queued            (sweeper moves it back after a retryable failure)
--   completed | failed | cancelled are terminal
-- ----------------------------------------------------------------------------
create table tasks (
    id              uuid primary key default gen_random_uuid(),
    idempotency_key text unique,                              -- client-supplied; repeat => same task
    created_by      uuid references agents(id) on delete set null,
    assigned_to     uuid references hosts(id) on delete set null,
    type            text not null
                    check (type in ('deploy','restart','stop','start','remove','rollback',
                                    'logs','status','healthcheck','build','docker-build',
                                    'docker-run','docker-compose','environment-update',
                                    'artifact-download','artifact-upload','system-info',
                                    'ingress-sync')),         -- PROTOCOL §3.2: the 18 task types
    status          text not null default 'queued'
                    check (status in ('queued','claimed','running','awaiting_approval',
                                      'completed','failed','cancelled','retrying')),
    priority        integer not null default 0,
    payload         jsonb not null default '{}'::jsonb,       -- task-type-specific input
    result          jsonb,                                    -- task-type-specific output
    error           text,
    attempts        integer not null default 0 check (attempts >= 0),
    max_attempts    integer not null default 3 check (max_attempts >= 1),
    claimed_by      uuid references hosts(id) on delete set null,
    lease_expires_at timestamptz,                             -- claim lease; the sweeper requeues
                                                             -- claimed/running tasks past their lease
    last_progress_at timestamptz,                             -- last claim or progress report
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    completed_at    timestamptz,
    updated_at      timestamptz not null default now()
);
create index idx_tasks_status_priority on tasks(status, priority desc, created_at);
create index idx_tasks_claimable on tasks(priority desc, created_at asc) where status = 'queued';
create index idx_tasks_claimed_by on tasks(claimed_by) where claimed_by is not null;
create index idx_tasks_payload_deployment_id on tasks((payload ->> 'deployment_id'));
create index idx_tasks_assigned_to on tasks(assigned_to);
create index idx_tasks_created_by on tasks(created_by);
create index idx_tasks_type on tasks(type);
create index idx_tasks_lease on tasks(lease_expires_at)
  where status in ('claimed','running');

-- ----------------------------------------------------------------------------
-- projects: deployable applications owned by agents
-- ----------------------------------------------------------------------------
create table projects (
    id                uuid primary key default gen_random_uuid(),
    name              text not null unique,
    owner             text,                                   -- agent name that owns it
    owner_agent_id    uuid references agents(id) on delete set null,  -- explicit owner (migration 011);
                                                             -- null = legacy-open: accessible to all agents
    repository        text,                                   -- optional source repo URL
    artifact_location text,
    runtime           text not null default 'docker',         -- 'docker' | 'docker-compose' | 'static'
    configuration     jsonb not null default '{}'::jsonb,     -- validated agent.deploy.json content
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index idx_projects_owner_agent on projects(owner_agent_id);

-- ----------------------------------------------------------------------------
-- project_members: project ACL (migration 011). Grants a non-owner agent
-- access to a project. The agent's own permission map still gates which
-- actions it may take — membership is about *which* projects, not *what*
-- actions.
-- ----------------------------------------------------------------------------
create table project_members (
    project_id uuid not null references projects(id) on delete cascade,
    agent_id   uuid not null references agents(id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (project_id, agent_id)
);
create index idx_project_members_agent on project_members(agent_id);

-- ----------------------------------------------------------------------------
-- agent_host_access: per-host targeting ACL for agents (migration 011).
-- An agent may explicitly target a host (POST /v1/deployments host_id)
-- only when a row exists for the agent+host pair — or, for backwards
-- compatibility, when the host has NO access rows at all (the first row
-- for a host switches it to strict mode).
-- ----------------------------------------------------------------------------
create table agent_host_access (
    agent_id    uuid not null references agents(id) on delete cascade,
    host_id     uuid not null references hosts(id) on delete cascade,
    permissions jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    primary key (agent_id, host_id)
);
create index idx_agent_host_access_host on agent_host_access(host_id);

-- ----------------------------------------------------------------------------
-- migration_reports: durable record of migration outcomes (migration 011).
-- Every project whose legacy owner name could not be resolved to an agent
-- is recorded here (never silently assigned).
-- ----------------------------------------------------------------------------
create table migration_reports (
    id          bigserial primary key,
    migration   text not null,
    project_id  uuid references projects(id) on delete set null,
    detail      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    unique (migration, project_id)
);
create index idx_migration_reports_migration on migration_reports(migration);

-- ----------------------------------------------------------------------------
-- artifacts: immutable build outputs, content-addressed by SHA-256.
-- The worker MUST verify checksum after download before use.
-- ----------------------------------------------------------------------------
create table artifacts (
    id           uuid primary key default gen_random_uuid(),
    project_id   uuid not null references projects(id) on delete cascade,
    filename     text not null,
    storage_path text not null,                              -- relative path inside artifact store
    checksum     text not null,                               -- 'sha256:<hex>'
    size         bigint not null check (size >= 0),
    version      text not null,
    created_at   timestamptz not null default now(),
    manifest     jsonb,                                       -- validated agent.deploy.json (migration 014); NULL for legacy artifacts
    updated_at   timestamptz not null default now(),          -- migration 016: the touch_updated_at() trigger (001) requires this column
    unique (project_id, version)
);
create index idx_artifacts_project on artifacts(project_id);

-- ----------------------------------------------------------------------------
-- deployments: one deployment of a project version to a host.
-- A deployment is created by a task of type 'deploy' (or manually).
-- ----------------------------------------------------------------------------
create table deployments (
    id             uuid primary key default gen_random_uuid(),
    idempotency_key text unique,
    task_id        uuid references tasks(id) on delete set null,
    project_id     uuid not null references projects(id) on delete cascade,
    host_id        uuid not null references hosts(id) on delete cascade,
    version        text not null,
    mode           text not null default 'automatic'          -- 'automatic' | 'manual'
                   check (mode in ('automatic','manual')),
    artifact_id    uuid references artifacts(id) on delete set null,
    status         text not null default 'requested'
                   check (status in ('requested','approved','building','starting',
                                     'healthcheck','running','failed','rolled_back',
                                     'rollback_failed','stopped','stopping',
                                     'superseded')),  -- 'superseded' added by migration 015:
                                                      -- previous deployment of the same
                                                      -- project+host; reservation released,
                                                      -- kept as a rollback target
    container_ids  jsonb not null default '[]'::jsonb,
    ports          jsonb not null default '{}'::jsonb,        -- written from the deploy task's
                                                             -- result.ports on completion
    domains        jsonb not null default '[]'::jsonb,        -- DERIVED summary mirror of the
                                                             -- domains table (migration 007);
                                                             -- the domains table is authoritative
    health_status  text                                  -- 'healthy' | 'unhealthy' | 'unknown'
                   check (health_status is null or
                          health_status in ('healthy','unhealthy','unknown')),
    rollback_of    uuid references deployments(id) on delete set null,
    reserved_cpu   double precision,                          -- normalized CPU cores
                                                             -- reserved by this deployment
                                                             -- (migration 013); NULL = no
                                                             -- requirement
    reserved_ram_mb double precision,                         -- normalized RAM megabytes
                                                             -- reserved by this deployment
                                                             -- (migration 013); NULL = no
                                                             -- requirement
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (project_id, host_id, version)
);
create index idx_deployments_host on deployments(host_id);
create index idx_deployments_project on deployments(project_id);
create index idx_deployments_status on deployments(status);

-- ----------------------------------------------------------------------------
-- port_allocations: fixed host ports reserved for deployments.
-- unique(host_id, port) so two deployments on the same host can never claim
-- the same port. Allocations are created in the POST /v1/deployments
-- transaction when a host_port is requested, and released when the
-- deployment reaches a terminal state (failed | rolled_back | stopped) or
-- is superseded by a newer deployment of the same project+host.
-- ----------------------------------------------------------------------------
create table port_allocations (
    host_id       uuid not null references hosts(id) on delete cascade,
    port          integer not null check (port > 0 and port < 65536),
    deployment_id uuid not null references deployments(id) on delete cascade,
    allocated_at  timestamptz not null default now(),
    primary key (host_id, port)
);
create index idx_port_allocations_deployment on port_allocations(deployment_id);

-- ----------------------------------------------------------------------------
-- domains: public hostnames attached to deployments (migration 007).
-- The SOURCE OF TRUTH for the domain lifecycle; deployments.domains (jsonb)
-- is a derived summary mirror rebuilt from this table on every transition.
-- Lifecycle: requested -> configuring -> active -> failed -> removing ->
-- removed. `active` can also go through `degraded` (the route became
-- unverifiable; the reconciler retries degraded rows back to configuring
-- or failed). One live row per hostname (partial unique index) so a
-- hostname can never be attached to two live deployments at once.
-- ----------------------------------------------------------------------------
create table domains (
    id                uuid primary key default gen_random_uuid(),
    idempotency_key   text unique,                           -- client-supplied; repeat => same domain
    hostname          text not null,
    deployment_id     uuid not null references deployments(id) on delete cascade,
    host_id           uuid not null references hosts(id) on delete cascade,
    ingress           text not null default 'tunnel'
                      check (ingress in ('tunnel', 'direct')),
    status            text not null default 'requested'
                      check (status in ('requested', 'configuring', 'active',
                                        'degraded', 'failed', 'removing',
                                        'removed')),
    cf_record_id      text,                                  -- Cloudflare DNS record id, when DNS is managed
    dns_configured    boolean not null default false,        -- CNAME hostname -> ingress target exists
    tunnel_configured boolean not null default false,        -- remote tunnel ingress rule exists (tunnel mode)
    https_reachable   boolean not null default false,        -- last HTTPS probe result (informational)
    https_status      integer,                               -- last probe HTTP status (null when no response)
    https_checked_at  timestamptz,
    verified_at       timestamptz,                           -- last time route+DNS were confirmed present
    error             text,                                  -- last failure reason, cleared on success
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create unique index idx_domains_hostname_live
    on domains (lower(hostname))
    where status <> 'removed';
create index idx_domains_deployment on domains (deployment_id);
create index idx_domains_host on domains (host_id);
create index idx_domains_status on domains (status);

-- ----------------------------------------------------------------------------
-- events: APPEND-ONLY journal. This is the system's memory.
-- If every agent disappears, this table still tells the full story.
-- Rows may be INSERTed and SELECTed only — UPDATE/DELETE are blocked
-- by the trg_events_no_update_delete trigger below.
-- ----------------------------------------------------------------------------
create table events (
    id            bigserial primary key,
    type          text not null,          -- e.g. 'agent.connected', 'deployment.completed'
    actor_type    text,                   -- 'agent' | 'host' | 'system' | 'user'
    actor_id      text,                   -- agent name or host name (denormalized on purpose)
    task_id       uuid references tasks(id) on delete set null,
    deployment_id uuid references deployments(id) on delete set null,
    host_id       uuid references hosts(id) on delete set null,
    payload       jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);
create index idx_events_type on events(type);
create index idx_events_created on events(created_at desc);
create index idx_events_task on events(task_id) where task_id is not null;
create index idx_events_deployment on events(deployment_id) where deployment_id is not null;
create index idx_events_host on events(host_id) where host_id is not null;

create or replace function forbid_events_mutation() returns trigger as $$
begin
    raise exception 'events table is append-only: % not allowed', TG_OP;
    return null;
end;
$$ language plpgsql;

create trigger trg_events_no_update_delete
    before update or delete on events
    for each row execute function forbid_events_mutation();

-- ----------------------------------------------------------------------------
-- secrets: per-project secrets, encrypted at rest by the API layer
-- (AES-256-GCM with DATA_ENCRYPTION_KEY). Plaintext NEVER hits this table,
-- NEVER appears in logs, task payloads, or API responses (names only on read).
-- ----------------------------------------------------------------------------
create table secrets (
    id              uuid primary key default gen_random_uuid(),
    project_id      uuid not null references projects(id) on delete cascade,
    name            text not null,
    encrypted_value bytea not null,       -- AES-256-GCM: nonce(12) || ciphertext || tag(16)
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (project_id, name)
);

-- ----------------------------------------------------------------------------
-- updated_at maintenance
-- ----------------------------------------------------------------------------
create or replace function touch_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

do $$
declare t text;
begin
    foreach t in array array['agents','hosts','tasks','projects','artifacts','deployments','secrets']
    loop
        execute format('drop trigger if exists trg_touch_%s on %I', t, t);
        execute format('create trigger trg_touch_%s before update on %I
                        for each row execute function touch_updated_at()', t, t);
    end loop;
end $$;

-- ----------------------------------------------------------------------------
-- events TRUNCATE block (see migrations/005_events_truncate_block.sql).
-- A row-level trigger cannot block TRUNCATE, and PostgreSQL event
-- triggers do not support TRUNCATE at all (verified on PG 16), so there
-- is no trigger-based block. Defense in depth: revoke TRUNCATE from
-- PUBLIC (stops every non-owner role). Full protection needs the table
-- owned by a dedicated role distinct from the application role — see
-- docs/security.md "events table ownership".
-- ----------------------------------------------------------------------------
revoke truncate on public.events from public;
