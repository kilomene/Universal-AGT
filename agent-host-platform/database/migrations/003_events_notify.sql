-- 003: NOTIFY the control plane on every event INSERT so the
-- GET /v1/events/stream SSE endpoint can live-push new events
-- (LISTEN uag_events). Payload is the new event's id.
CREATE OR REPLACE FUNCTION notify_event_inserted() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('uag_events', NEW.id::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_events_notify ON events;
CREATE TRIGGER trg_events_notify
  AFTER INSERT ON events
  FOR EACH ROW EXECUTE FUNCTION notify_event_inserted();
