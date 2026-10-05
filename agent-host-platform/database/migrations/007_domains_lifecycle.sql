-- 007_domains_lifecycle.sql
--
-- Domains move from a JSONB blob on deployments.domains to a first-class
-- table with an explicit lifecycle:
--
--   requested -> configuring -> active -> failed -> removing -> removed
--                                   \-> failed (deployment died; reprovisionable)
--
-- The `deployments.domains` JSONB column is KEPT as a derived summary
-- mirror (it feeds GET /v1/services and the dashboard without extra
-- joins); the `domains` table is the source of truth and the domains API
-- rebuilds the mirror from it on every transition. The mirror never
-- contains 'removed' rows.
--
-- Backfill: every entry in deployments.domains becomes a row. Legacy
-- statuses map as: 'active' -> 'active', 'error' -> 'failed', anything
-- else (including 'dns_pending') -> 'requested'. Legacy entries have no
-- recorded deployment liveness beyond the deployment row itself, so rows
-- whose deployment is already terminal (failed/rolled_back/stopped) are
-- backfilled as 'failed' with an explanatory error — a domain must never
-- silently point at a dead deployment.
--
-- hostname uniqueness: one live row per hostname (partial unique index
-- excluding 'removed'), so a hostname can never be attached to two live
-- deployments at once.

create table if not exists domains (
    id                uuid primary key default gen_random_uuid(),
    hostname          text not null,
    deployment_id     uuid not null references deployments(id) on delete cascade,
    host_id           uuid not null references hosts(id) on delete cascade,
    ingress           text not null default 'tunnel'
                      check (ingress in ('tunnel', 'direct')),
    status            text not null default 'requested'
                      check (status in ('requested', 'configuring', 'active',
                                        'failed', 'removing', 'removed')),
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

create unique index if not exists idx_domains_hostname_live
    on domains (lower(hostname))
    where status <> 'removed';
create index if not exists idx_domains_deployment on domains (deployment_id);
create index if not exists idx_domains_host on domains (host_id);
create index if not exists idx_domains_status on domains (status);

-- keep updated_at fresh (same touch_updated_at() helper as 001)
drop trigger if exists trg_domains_touch_updated_at on domains;
create trigger trg_domains_touch_updated_at
    before update on domains
    for each row execute function touch_updated_at();

-- -- backfill from deployments.domains --------------------------------------
-- Only for rows that have no domains rows yet (migration is re-runnable).
insert into domains (hostname, deployment_id, host_id, ingress, status,
                     cf_record_id, dns_configured, error, created_at)
select
    e.hostname,
    d.id,
    d.host_id,
    case when e.ingress = 'tunnel' then 'tunnel' else 'direct' end,
    case
        when d.status in ('failed', 'rolled_back', 'stopped') then 'failed'
        when e.status = 'active' then 'active'
        when e.status = 'error' then 'failed'
        else 'requested'
    end,
    e.cf_record_id,
    (e.status = 'active' and e.cf_record_id is not null),
    case
        when d.status in ('failed', 'rolled_back', 'stopped')
            then 'backfill: deployment was already ' || d.status
        when e.status = 'error' then e.error
        else null
    end,
    coalesce(
        case when e.added_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
             then (e.added_at)::timestamptz end,
        now())
from deployments d
cross join lateral (
    select
        (elem->>'hostname')        as hostname,
        (elem->>'ingress')        as ingress,
        (elem->>'status')         as status,
        (elem->>'cf_record_id')   as cf_record_id,
        (elem->>'error')          as error,
        (elem->>'added_at')       as added_at
    from jsonb_array_elements(coalesce(d.domains, '[]'::jsonb)) as elem
) e
where e.hostname is not null
  and not exists (select 1 from domains x where x.deployment_id = d.id);
