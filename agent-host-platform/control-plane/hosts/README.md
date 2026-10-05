# hosts (module map)

Implemented in `api/src/routes/hosts.ts`, `api/src/routes/worker.ts`
(heartbeat), and the `hosts` table (`database/schema/schema.sql`).

- Hosts are abstract persistent compute nodes (e.g. `persistent-host-01`).
- No IP addresses are stored or required — hosts connect outbound only.
- Heartbeats update stats, `last_seen`, and online/offline status.
- See `agent-sdk/protocol/PROTOCOL.md` §3.3–§3.4.
