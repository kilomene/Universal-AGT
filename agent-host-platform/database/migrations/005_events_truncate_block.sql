-- 005_events_truncate_block.sql
--
-- migrate: no-transaction
--
-- (CREATE EVENT TRIGGER is forbidden inside a transaction block, so this
-- migration runs outside one. Every statement below is idempotent:
-- CREATE OR REPLACE FUNCTION, DROP EVENT TRIGGER IF EXISTS, then CREATE,
-- so a retry after a partial failure is safe.)
--
-- The events table is append-only (001: trg_events_no_update_delete blocks
-- UPDATE/DELETE), but a row-level trigger CANNOT block TRUNCATE — TRUNCATE
-- bypasses row triggers entirely. This migration closes that hole with an
-- EVENT TRIGGER that aborts any TRUNCATE touching public.events.
--
-- Chosen approach: event trigger (not REVOKE) because the application role
-- is normally also the table owner (it ran the migrations), and owners can
-- always TRUNCATE their own tables regardless of REVOKE. The event trigger
-- fires on ddl_command_end with tag 'TRUNCATE TABLE'; raising there aborts
-- the whole transaction, so the truncate never takes effect.
--
-- OPERATIONAL NOTE: CREATE EVENT TRIGGER requires superuser. If migrations
-- run as a non-superuser role this file fails — have the DBA run it once
-- manually as superuser, then mark it applied in schema_migrations.
-- (Event triggers are database-global, not schema objects: they are not
-- captured by pg_dump's schema-only dumps; keep this file as the source
-- of truth and re-apply after a restore.)

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
