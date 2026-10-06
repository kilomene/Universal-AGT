# Universal AGT — Security

Threat model in one line: **agents are untrusted clients, the network is
hostile, the host runs arbitrary agent-supplied containers** — and the
system must still be safe to operate. The controls below are enforced in
code, not just documented.

## Authentication

Two bearer-token credential kinds, both sent as
`Authorization: Bearer <token>` over HTTPS:

| Credential | Issued by | Used by | Scope |
|---|---|---|---|
| Agent API key | `POST /v1/agents/register` (one-time, shown once) | agents, SDKs, CLI, dashboard | agent row + scoped permissions |
| Host token | `POST /v1/hosts/register` (one-time, shown once) | host worker | host row only |

- Tokens are stored **server-side as SHA-256 hashes**. A database read
  never yields a usable token.
- Agent and host tokens are **not interchangeable**: a host token can never
  call agent endpoints and vice versa. The token kind is bound at issuance.
- Registration endpoints are one-time bootstrap calls; agent registration
  is additionally gated (2026-10-05): `POST /v1/agents/register` requires
  header `X-Provisioning-Token: <UAHT_PROVISIONING_TOKEN>` (constant-time
  compare). With the env var unset, only the very first registration is
  open (bootstrap mode); afterwards registration returns 403 until the
  operator sets the token. In production (`NODE_ENV=production`) the
  server refuses to start without `UAHT_PROVISIONING_TOKEN`.
  One provisioning token, two uses — don't confuse the *uses* with two
  *tokens*: the single `UAHT_PROVISIONING_TOKEN` gates *agent* registration
  (`X-Provisioning-Token` header) **and** *host* bootstrap registration
  (`Authorization: Bearer` on `POST /v1/hosts/register`, hands-off first
  boot). Alternatively, an agent token with the `deploy` permission can
  register hosts.
- Credentials rotate without downtime: `POST /v1/agents/me/rotate` and
  `POST /v1/hosts/:id/rotate-token` issue a new credential, invalidate the
  old one immediately, and emit `agent.key_rotated` / `host.token_rotated`.
  Rotate on any suspected compromise.
- HTTPS only in production. Bearer tokens over plain HTTP are compromised
  by definition.

## Permissions (least privilege)

Agent API keys carry a scoped permission set, enforced per endpoint:

`deploy`, `read_status`, `read_logs`, `restart`, `stop`, `remove`,
`manage_domains`, `approve_deployments`, `manage_secrets`

Register agents with the minimal set for their job. A deploy-only CI agent
gets `deploy` + `read_status` — it cannot restart services, approve other
agents' manual deployments, or touch secrets. Approval for manual-mode
deploys is itself a permission (`approve_deployments`), so the "second pair
of eyes" can't be the same key that requested the deploy unless you grant
it both.

## Host worker containment

The worker is the most exposed component — it executes agent-supplied
workloads. Its defenses:

- **Command allowlist.** The worker only ever runs Docker and a fixed set
  of inspection commands derived from task fields. It never executes a raw
  shell string received over the network. A compromised or malicious task
  payload cannot become host shell access through the executor.
- **`extra_args` removed (2026-10-05, critical).** `docker-run` used to
  accept caller-supplied docker flags verbatim — `--privileged`,
  `-v /:/host` straight to host root. The field is gone from the protocol:
  the worker's policy layer rejects any `docker-run` task carrying it
  (`TaskRejected`, task reported `failed`, nothing executed), and
  `DockerClient.run()` no longer has the parameter at all.
- **Archive extraction.** Tar archives extract with `tarfile.data_filter`
  (blocks absolute paths, `..` escapes, and symlink/hardlink members that
  resolve outside the destination — the old pre-scan loop could be bypassed
  by a symlink member followed by a file *through* it). Zip members are
  still checked individually (zip has no filter API).
- **Manifest path confinement.** `build.dockerfile`, `build.context`, and
  `compose_file` from an artifact's `agent.deploy.json` (or task payload)
  are resolved strictly inside the extracted artifact tree — `..` and
  absolute paths are rejected before any `docker build` / `compose up`.
- **No config exfiltration.** `artifact-upload` refuses to read anything at
  or under the worker's `config/` directory (where `worker.env` — host
  token, control-plane URL — lives), in addition to the work_dir
  confinement that already applies.
- **Outbound-only networking.** The worker dials out to the control plane;
  it opens no inbound ports, so there is no listening service to attack and
  no callback for the control plane to be tricked into calling.
- **No host identity in the API.** Hosts are referenced by `host_id` /
  `host_name` only. IP addresses never appear in requests or responses, so
  network topology doesn't leak through the API.
- **systemd hardening.** The reference unit runs with `NoNewPrivileges`,
  `ProtectSystem=strict`, and a minimal writable path set.
- **Self-update channel.** Worker updates are pulled by the worker from the
  control plane over its authenticated channel (emits `worker.updated`);
  there is no push-to-host mechanism to hijack. The release tarball's
  SHA-256 is verified before install, the new tree must byte-compile and
  import cleanly, and — since 2026-10-05 — the updater health-gates the
  restart: the new worker writes a boot marker (`<work_dir>/worker.status.json`
  with version + boot timestamp, also checkable via
  `agent-host-worker --self-check`), and the updater rolls the `current`
  symlink back and restarts the previous release if the new version is not
  healthy within `WORKER_UPDATE_HEALTH_TIMEOUT_S` (default 120s).
  - **Explicit trust-model note:** update authorization is "the control
    plane said so, and the SHA-256 matches what the control plane said".
    A compromised control plane can therefore push arbitrary worker code —
    this is accepted and by design (the control plane is already the
    trusted orchestrator: it issues every task the worker runs). Code
    signing with an offline key is future work, not a current control.

Containers themselves run under Docker's isolation with the resource
limits from `agent.deploy.json` (`resources.memory` / `resources.cpu`);
the worker sets those limits on every `docker run` it issues.

## Artifact integrity

- Upload: the agent declares `size` and `sha256:<hex>` at
  `POST /v1/artifacts/init`; the server verifies both on
  `PUT /v1/artifacts/:id/content` and rejects mismatches (the artifact is
  quarantined, `artifact.upload_failed` is emitted).
- Uploads are wire-capped (2026-10-05): `ARTIFACT_MAX_BYTES` (default 500MB)
  is enforced on `Content-Length` before anything is written, per-chunk
  while streaming (a lying or absent `Content-Length` can't bypass it),
  and on the declared `size` at init — all 413 `payload_too_large`.
- Download: the worker verifies the SHA-256 **again** after downloading,
  before building. A tampered artifact in transit or at rest fails the
  task instead of being built.
- Download scoping (2026-10-05): a host token may only download artifacts
  referenced by tasks that host claimed (`payload.artifact_id`); any other
  host gets 403. Agents keep `read_status` downloads.
- The worker validates `agent.deploy.json` against the PROTOCOL §4 rules
  before any build; invalid manifests fail fast with no partial state.

## Secret handling

- Project secrets are stored **encrypted at rest** via
  `POST /v1/projects/:id/secrets` (upsert): AES-256-GCM with the
  `DATA_ENCRYPTION_KEY` (64 hex chars = 32 bytes); the stored blob is
  `nonce(12) || ciphertext || authTag(16)`. Plaintext never hits the
  `secrets` table.
- Secret **values are never returned** by any agent-facing endpoint:
  `GET /v1/projects/:id/secrets` lists names + `created_at`/`updated_at`
  only, the POST response returns metadata only, and DELETE returns 204.
- Rotation/change emits **`secret.changed`**, deletion emits
  **`secret.deleted`** — payloads carry `project_id` + `name` only, never
  values.
- The worker pulls decrypted values over its authenticated channel via
  `GET /v1/worker/projects/:project_id/secrets` (**host token only**; agent
  tokens get 403). The server answers only when that host has a
  claimed/running/awaiting_approval task or a live
  (requested/approved/building/starting/healthcheck/running) deployment for
  the project — otherwise 403. At deploy time the worker injects them as
  container env (payload-carried `secrets` win on key collision);
  `environment-update` merges payload secrets the same way.
- Secrets never appear in logs, events, or task results. The dispatcher's
  per-task scrubber redacts payload-carried secret values and the host
  token from log lines, `log_chunk`s and progress errors; `deploy()` chains
  the API-fetched values into the active scrubber as well (a failing
  `docker run` embeds the full `-e KEY=value` argv in its error), and secret
  env is never persisted to the worker's `state.json`.
- Managing secrets requires the `manage_secrets` permission — keep it off
  every key that doesn't strictly need it.

Accepted limitations (secrets):

- Payload-carried secrets (`payload.secrets` on docker-run /
  environment-update tasks) are agent-supplied and live in **plaintext in
  the task row** — the encrypted store + worker pull is the preferred path
  for real credentials.
- `docker run -e KEY=value` passes values as process argv, briefly visible
  in the host's process list while docker starts the container.
- Scrubbers skip values shorter than 4 characters (redacting 1–3 char
  strings would mangle ordinary log text with false positives).

## Rate limits

- **120 req/min per agent API key** — enough for polling loops with sane
  backoff; aggressive polling gets `429 rate_limited` with the standard
  error shape.
- **600 req/min per host token** — heartbeats and task claims are chatty by
  design.
- **10 req/min per IP for unauthenticated requests**
  (`RATE_LIMIT_UNAUTH_PER_MIN`, 2026-10-05) — registration and other open
  endpoints are throttled against enumeration floods. `/v1/health` is mounted
  before the limiter and stays cheap.

Exceeding the limit never corrupts state: mutating endpoints are
idempotent-keyed, so a client can back off and retry the identical request.

## Worker privilege model

The worker is deliberately powerful — it has to be, to run Docker
workloads. This section states exactly what it can do after the Phase 6
fixes, and where the trust boundary sits.

**What the worker holds (and why):**
- **Docker group membership + RW on the Docker socket.** Required to
  `docker run/build/compose` agent workloads at all. This is
  root-equivalent on the host by Docker's own design — there is no
  unprivileged way to manage containers. The systemd unit mitigates with
  `NoNewPrivileges` / `ProtectSystem=strict`, but the socket itself is the
  crown jewel: anyone who can speak to it owns the host.
- **RW on `/srv/agent-apps`** (`apps_dir`). Deployment working state lives
  here; the worker writes extracted artifacts and build contexts nowhere
  else.
- **RW on `/opt/agent-host`** (`work_dir`). Task logs, deployment
  `state.json`, downloaded artifacts, update releases. `config/worker.env`
  (host token, control-plane URL) lives here too — readable by the worker
  process (it must be), but never servable: `artifact-upload` refuses any
  path at/under `config/`.
- **Outbound HTTPS to the control plane only.** No inbound ports, no
  listening sockets.

**What the worker can still do by design (operator trust boundary):**
- `deploy`, `build`/`docker-build`, `docker-run`, `docker-compose`, and
  `environment-update` tasks execute **arbitrary agent-supplied container
  code**. That is the product, not a bug: these task types are fully
  privileged *inside containers* by design. The Phase 6 fixes removed the
  paths that escaped the container boundary without going through the
  image/container abstraction (`extra_args` flag injection, tar symlink
  escapes, manifest path traversal). What remains is Docker's own isolation
  plus the resource limits from `agent.deploy.json`.
- The worker will never execute a host shell string from the network, read
  host files outside its two directories on an agent's behalf, or exfiltrate
  its own credentials.

**In short:** trust the worker with containers, because containers are the
job; trust Docker's isolation for the boundary; and treat the control plane
as the trusted orchestrator (see the self-update trust note above) —
a compromised control plane means compromised workers, full stop.

## Accepted limitations

- **No replay protection on bearer tokens.** Requests are authenticated by
  a static bearer token over TLS; there is no per-request nonce/signature,
  so a captured request could be replayed within the token's lifetime.
  Mitigations in place: TLS-everywhere, credential rotation endpoints, and
  short-lived operational use of keys. HMAC request signing is tracked as
  future work — deliberately not invented mid-phase.
- **Self-update trusts the control plane's SHA-256** (see above): a
  compromised control plane can push worker code. Accepted as the
  orchestrator trust boundary.

## Audit: the append-only event journal

Every security-relevant action emits an event:
`agent.connected/disconnected`, `task.*`, `artifact.*`,
`deployment.*`, `service.*`, `host.registered/online/offline`,
`secret.changed`, `secret.deleted`, `worker.updated`, `agent.key_rotated`,
`host.token_rotated`. Events are **append-only** — there is no delete or
update endpoint — and are cursor-paginated, so an auditor can replay the
full history of who did what, when, from which actor.

`UPDATE`/`DELETE` are blocked by the `trg_events_no_update_delete` row
trigger. `TRUNCATE` bypasses row triggers, and PostgreSQL event triggers do
not support `TRUNCATE` at all (verified on PG 16), so there is no
trigger-based block: migration `005_events_truncate_block.sql` applies
defense-in-depth (`REVOKE TRUNCATE ON events FROM PUBLIC`, stopping every
non-owner role). Complete protection needs the table owned by a dedicated
role distinct from the application role — see "events table ownership"
below.

### Events table ownership (complete TRUNCATE protection)

The migration-level `REVOKE TRUNCATE` stops non-owner roles, but the table
owner can always `TRUNCATE` regardless of `REVOKE`. For installations where
the audit journal must survive even a compromised application credential,
transfer ownership to a dedicated role (as superuser, e.g. the Supabase SQL
editor):

```sql
CREATE ROLE uagt_events_owner NOLOGIN;
ALTER TABLE public.events OWNER TO uagt_events_owner;
REVOKE ALL ON public.events FROM PUBLIC, <app_role>;
GRANT INSERT, SELECT ON public.events TO <app_role>;
GRANT USAGE, SELECT ON SEQUENCE public.events_id_seq TO <app_role>;
```

After this, the application role can append to and read the journal but
cannot `TRUNCATE`, `UPDATE`, or `DELETE` it under any circumstance. The
row-level `trg_events_no_update_delete` trigger remains as a second layer.
This step is optional and operator-owned; the migration chain does not do
it automatically because the application role name varies by deployment.

## Operational checklist

- [ ] Control plane served over HTTPS with a valid certificate.
- [ ] `UAHT_PROVISIONING_TOKEN` set (server refuses to boot in production
      without it); `DATA_ENCRYPTION_KEY` set (64 hex chars).
- [ ] Registration/bootstrap endpoints locked down after initial setup.
- [ ] Agent keys issued with minimal permissions; `manage_secrets` and
      `approve_deployments` granted sparingly.
- [ ] Host tokens live only in the host's own env file —
      `/opt/agent-host/config/worker.env` (mode `0600`, agenthost-owned) when
      installed with `scripts/install-host.sh`; never in the repo or logs.
- [ ] Dashboard users authenticate with agent keys (session-scoped); the
      control plane accepts `?api_key=` **only** on `/v1/events/stream`.
- [ ] `ARTIFACT_MAX_BYTES` sized for the workload (default 500MB).
- [ ] Migration `005_events_truncate_block.sql` applied as superuser
      (re-apply after any database restore).
- [ ] Review the event journal periodically for `task.rejected`,
      `artifact.upload_failed`, `agent.key_rotated` / `host.token_rotated`,
      and unexpected `host.offline` flaps.
