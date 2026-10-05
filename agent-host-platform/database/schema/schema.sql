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
    last_seen     timestamptz,
    metadata      jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index idx_agents_status on agents(status);

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
    capabilities   jsonb not null default '[]'::jsonb,        -- e.g. ["docker","docker-compose","gpu"]
    worker_version text,
    token_hash     text not null,                             -- SHA-256 hex of the host bearer token
    cpu_pct        double precision,
    ram_pct        double precision,
    disk_pct       double precision,
    docker_status  text,                                      -- 'ok' | 'missing' | 'error: ...'
    running_apps   jsonb not null default '[]'::jsonb,        -- [{deployment_id, project, status}]
    total_cpu      double precision,                          -- cores available
    total_ram_mb   double precision,
    total_disk_gb  double precision,
    last_seen      timestamptz,
    metadata       jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index idx_hosts_status on hosts(status);

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
    type            text not null,                            -- deploy|restart|stop|start|remove|rollback|
                                                             -- logs|status|healthcheck|build|docker-build|
                                                             -- docker-run|docker-compose|environment-update|
                                                             -- artifact-download|artifact-upload|system-info
    status          text not null default 'queued'
                    check (status in ('queued','claimed','running','awaiting_approval',
                                      'completed','failed','cancelled','retrying')),
    priority        integer not null default 0,
    payload         jsonb not null default '{}'::jsonb,       -- task-type-specific input
    result          jsonb,                                    -- task-type-specific output
    error           text,
    attempts        integer not null default 0,
    max_attempts    integer not null default 3,
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
    repository        text,                                   -- optional source repo URL
    artifact_location text,
    runtime           text not null default 'docker',         -- 'docker' | 'docker-compose' | 'static'
    configuration     jsonb not null default '{}'::jsonb,     -- validated agent.deploy.json content
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

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
                                     'stopped','stopping')),
    container_ids  jsonb not null default '[]'::jsonb,
    ports          jsonb not null default '{}'::jsonb,        -- written from the deploy task's
                                                             -- result.ports on completion
    domains        jsonb not null default '[]'::jsonb,
    health_status  text,                                      -- 'healthy' | 'unhealthy' | 'unknown'
    rollback_of    uuid references deployments(id) on delete set null,
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
-- A row-level trigger cannot block TRUNCATE, so an event trigger aborts
-- any TRUNCATE touching public.events. NOTE: CREATE EVENT TRIGGER needs
-- superuser; if this schema is applied as a non-superuser, run this block
-- separately as superuser.
-- ----------------------------------------------------------------------------
create or replace function forbid_events_truncate() returns event_trigger as $$
declare
    obj record;
begin
    for obj in select * from pg_event_trigger_ddl_commands() loop
        if obj.command_tag = 'TRUNCATE TABLE'
           and obj.object_identity ilike 'public.events%' then
            raise exception 'events table is append-only: TRUNCATE not allowed';
        end if;
    end loop;
end;
$$ language plpgsql;

drop event trigger if exists trg_no_truncate_events;

create event trigger trg_no_truncate_events
    on ddl_command_end
    when tag in ('TRUNCATE TABLE')
    execute function forbid_events_truncate();
