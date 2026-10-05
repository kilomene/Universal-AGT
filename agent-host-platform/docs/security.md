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
- Registration endpoints are one-time bootstrap calls; how they are
  protected (operator-issued bootstrap secret, mTLS, admin console) is a
  control-plane deployment decision — they must not be left open in
  production.
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
  there is no push-to-host mechanism to hijack.

Containers themselves run under Docker's isolation with the resource
limits from `agent.deploy.json` (`resources.memory` / `resources.cpu`);
the worker sets those limits on every `docker run` it issues.

## Artifact integrity

- Upload: the agent declares `size` and `sha256:<hex>` at
  `POST /v1/artifacts/init`; the server verifies both on
  `PUT /v1/artifacts/:id/content` and rejects mismatches (the artifact is
  quarantined, `artifact.upload_failed` is emitted).
- Download: the worker verifies the SHA-256 **again** after downloading,
  before building. A tampered artifact in transit or at rest fails the
  task instead of being built.
- The worker validates `agent.deploy.json` against the PROTOCOL §4 rules
  before any build; invalid manifests fail fast with no partial state.

## Secret handling

- Secrets are stored **encrypted at rest** via `POST /v1/projects/:id/secrets`.
- They are **never returned** by any read endpoint: `GET .../secrets`
  lists names only.
- The worker receives decrypted values **only inside the task payload of a
  deployment it is actively executing**, over its authenticated channel.
- Secrets never appear in logs, events, or task results. The worker's log
  redaction strips known secret values from `log_chunk`s before upload.
- Managing secrets requires the `manage_secrets` permission — keep it off
  every key that doesn't strictly need it.

## Rate limits

- **120 req/min per agent API key** — enough for polling loops with sane
  backoff; aggressive polling gets `429 rate_limited` with the standard
  error shape.
- **600 req/min per host token** — heartbeats and task claims are chatty by
  design.

Exceeding the limit never corrupts state: mutating endpoints are
idempotent-keyed, so a client can back off and retry the identical request.

## Audit: the append-only event journal

Every security-relevant action emits an event:
`agent.connected/disconnected`, `task.*`, `artifact.*`,
`deployment.*`, `service.*`, `host.registered/online/offline`,
`secret.updated`, `worker.updated`. Events are **append-only** — there is
no delete or update endpoint — and are cursor-paginated, so an auditor can
replay the full history of who did what, when, from which actor.

## Operational checklist

- [ ] Control plane served over HTTPS with a valid certificate.
- [ ] Registration/bootstrap endpoints locked down after initial setup.
- [ ] Agent keys issued with minimal permissions; `manage_secrets` and
      `approve_deployments` granted sparingly.
- [ ] Host tokens live only in `/etc/uagt/worker.env` (mode `0600`).
- [ ] Dashboard users authenticate with agent keys (session-scoped); the
      control plane accepts `?api_key=` **only** on `/v1/events/stream`.
- [ ] Review the event journal periodically for `task.rejected`,
      `artifact.upload_failed`, and unexpected `host.offline` flaps.
