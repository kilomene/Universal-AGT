# Public ingress & Cloudflare DNS

## The one thing you must understand first

**DNS does not make an outbound-only host reachable.**

The persistent host dials *out* to the control plane and never listens for
inbound connections — nothing outside can initiate a connection *to* it.
A CNAME record is just a name pointing at another name. If nothing at the
other end can carry traffic back down to the host, the domain resolves and
the connection goes nowhere.

So there are two ingress modes, and you must pick one deliberately:

| Mode | What it is | When traffic actually reaches the host |
|---|---|---|
| **tunnel** *(recommended for the private-host case)* | CNAME `hostname → <tunnel-id>.cfargotunnel.com`, and the **worker itself** runs a supervised `cloudflared tunnel --token <TOKEN> run` — an outbound-only connection to Cloudflare's edge that carries inbound HTTP back to the host's loopback container ports. | **Yes** — once the tunnel is up and the route is in the remote tunnel configuration (see below). This is the only mode where this system owns the whole path. |
| **direct** | CNAME `hostname → PUBLIC_INGRESS_HOSTNAME` (proxied, TTL 300). | Only if **you** run an ingress in front of the host — a reverse proxy, load balancer, or tunnel you manage — and it can reach the host's container ports. That ingress is **your responsibility**; this system never sees host IPs and cannot check it for you. |

**Without one of these, domains resolve but traffic cannot reach the host.**
That is not a bug or a missing feature — it is the architecture (spec
§28). Do not pretend otherwise in runbooks or status pages.

## Control authority: the remote tunnel configuration is the source of truth

For token-based (remotely-managed) tunnels — the only kind this system runs
(`cloudflared tunnel --token <TOKEN> run`) — **routes are controlled
remotely through Cloudflare, not by any local config file.**

Concretely: when `cloudflared` is started with `--token`, it ignores the
ingress rules in any local `config.yml` and pulls its configuration from
the Cloudflare edge. Editing or rewriting a local `config.yml` does **not**
change where traffic goes. The control plane therefore manages routes
exclusively through the Cloudflare API:

- `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` — read the live ingress table
- `PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` — full-replace the ingress table, then re-read to verify

Remote config changes propagate to a running `cloudflared` within seconds —
**no restart is needed** to change routes. The worker still keeps a local
`config.yml` on disk, but it is a **non-authoritative diagnostic mirror**
(the file says so in its header comment): it lets an operator eyeball the
intended routes on the host, but it routes nothing. Any documentation,
status page, or runbook that implies a local config file controls routing
is wrong and must be fixed.

The remote ingress table is shared: the reconciler only touches hostnames
the system manages (the active tunnel domains of live deployments plus
operator hostnames it has seen before as managed). Ingress rules for
hostnames the system does not know about are **preserved verbatim** — the
system will not delete an operator's dashboard-created routes.

## Mode: direct (your own ingress)

1. Run your ingress (Caddy/Nginx/LB/anything) where it can reach the host's
   published container ports.
2. Create a Cloudflare API token scoped to **Zone → DNS → Edit** on exactly
   the zone that holds your hostnames.
3. Set on the control plane:
   - `CLOUDFLARE_API_TOKEN` — the scoped token
   - `CLOUDFLARE_ZONE_ID` — the zone id
   - `PUBLIC_INGRESS_HOSTNAME` — e.g. `ingress.example.net`
4. Attach a domain (needs the `manage_domains` permission):

```bash
agent-host domains add --deployment <id> --hostname api.example.com --ingress direct
# or: curl -X POST $UAHT_BASE_URL/v1/domains \
#       -H "Authorization: Bearer $UAHT_API_KEY" \
#       -H 'Content-Type: application/json' \
#       -d '{"deployment_id":"<uuid>","hostname":"api.example.com","ingress":"direct"}'
```

The API creates a proxied (orange-cloud) CNAME `api.example.com →
ingress.example.net` with TTL 300. Removing the domain
(`agent-host domains rm` / `DELETE /v1/domains`) deletes the DNS record and
verifies it is gone. The worker is not involved: your ingress must already
route to the host's container ports.

## Mode: cloudflare-tunnel (worker-managed, outbound-only)

The worker supervises `cloudflared tunnel run` with the tunnel token handed
to the child through the `TUNNEL_TOKEN` environment variable — never on the
command line (argv is world-readable via `ps`; the child environment is
only readable by the same UID). The tunnel dials **out** to Cloudflare; no
inbound ports are opened on the host.

### Setup — the live-credential step (done once, by the operator)

These live in your Cloudflare account and **cannot be automated**; no
credentials are ever stored in the repo:

1. In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels →
   Create a tunnel** (Cloudflared type). Name it, e.g. `agent-host-ingress`.
2. Copy the **tunnel token** shown on the install step (a JWT starting with
   `eyJ…`). Treat it like a password.
3. Find the tunnel hostname: on the tunnel's overview it looks like
   `<tunnel-id>.cfargotunnel.com`. Copy it.
4. Create an API token with **Account → Cloudflare Tunnel → Edit** on the
   account (for route management) — it may be the same token as the DNS one
   if you give it both scopes — and note your **Account ID** (dashboard
   URL or the account home page).

**Control plane environment:**

```bash
CLOUDFLARE_API_TOKEN=<scoped token>        # Zone → DNS → Edit (DNS records)
CLOUDFLARE_ZONE_ID=<zone id>
CLOUDFLARE_TUNNEL_API_TOKEN=<token>        # Account → Tunnel → Edit (may reuse API_TOKEN)
CLOUDFLARE_ACCOUNT_ID=<account id>
TUNNEL_INGRESS_HOSTNAME=<tunnel-id>.cfargotunnel.com   # from step 3
```

**Host** (`/opt/agent-host/config/worker.env`, mode `0600`):

```bash
WORKER_INGRESS_ENABLED=true
WORKER_INGRESS_PROVIDER=cloudflare-tunnel
WORKER_TUNNEL_TOKEN=<redacted>
```

On startup the worker downloads `cloudflared` (pinned release, SHA-256
verified — no auto-trust of "latest") if it isn't on `PATH`, then starts the
supervised tunnel. If the download fails or the token is missing, the worker
**logs clearly and stays with ingress disabled** — deployments keep working.

### The domain lifecycle

Domains are first-class rows in the `domains` table (migration
`007_domains_lifecycle.sql`), not a JSON blob:

```
requested → configuring → active → failed
                            ↘ degraded → active (recovered) | failed
                          ↘ removing → removed
```

An `active` domain whose public route stops verifying is moved to
`degraded` (event `domain.degraded`); the periodic domain reconciler
retries degraded rows until they converge back to `active` (event
`domain.recovered`) or to `failed`. Every reconciler pass that changes
anything emits a summary event `domain.reconcile`.

`POST /v1/domains` runs the full provisioning flow:

1. **Validate** the hostname (well-formed DNS name; no wildcards, no
   `localhost`/`.local`/`.internal`/`.invalid`/`.example`/`.test`/`.onion`,
   no IP literals) and the CNAME target.
2. **Attach** the domain to the deployment (`requested`).
3. **DNS**: create (or reuse) a proxied CNAME
   `hostname → <tunnel-id>.cfargotunnel.com`, then re-read the record to
   confirm it exists (`dns_configured`).
4. **Tunnel route**: read the remote ingress table, upsert
   `hostname → http://127.0.0.1:<container-port>` (the deployment's own
   published host port — never an arbitrary URL), re-read the table to
   confirm the rule is present (`tunnel_configured`). Rules for hostnames the
   system doesn't manage are left untouched.
5. **HTTPS probe**: a plain GET to `https://hostname` from the control
   plane records reachability (`https_reachable`, `https_status`) —
   informational only, it never blocks activation (DNS/TLS propagation can
   lag).
6. The domain becomes `active` (`verified_at` set) — or `failed` with the
   exact reason in `error`. A failure leaves the domain in `failed`, not
   half-wired: DNS records and tunnel rules created before the failure stay
   where they are (the retry/removal path cleans them), and the status and
   error are queryable via `GET /v1/domains/:hostname`.

`DELETE /v1/domains` detaches cleanly (`removing` → `removed`): the tunnel
route is dropped from the remote configuration and the DNS record is
deleted, each **verified gone by re-reading** before the domain is marked
`removed`. If verification fails the domain stays `removing` with an error
— never silently half-deleted.

**A domain never points at a dead deployment without the control plane
knowing.** When a deployment reaches a terminal state (`stopped`, `failed`,
`rolled_back`), its tunnel domains are marked `failed` (with an event on
the deployment's event stream), their tunnel routes are dropped from the
remote configuration, and DNS records are removed; `deployments.domains`
(the old JSONB column) is kept as a derived summary mirror only. When the
deployment recovers (a new live deployment supersedes it), its tunnel
domains are re-provisioned.

Request a domain:

```bash
agent-host domains add --deployment <id> --hostname api.example.com --ingress tunnel
# or POST /v1/domains {"deployment_id":"<uuid>","hostname":"api.example.com","ingress":"tunnel"}
```

The API returns `422` if you request tunnel mode without
`TUNNEL_INGRESS_HOSTNAME` configured or against a non-live deployment, and
`409` if the hostname is already attached anywhere live — the error says so
plainly. Then open `https://api.example.com` — traffic flows: client →
Cloudflare edge → (outbound tunnel) → `cloudflared` on the host →
`127.0.0.1:<port>`.

### Tunnel behavior notes

- Crash supervision: if `cloudflared` exits unexpectedly, the worker
  restarts it with exponential backoff. Route changes propagate through the
  remote configuration — **no tunnel restart is needed for route changes**
  (the local `config.yml` mirror is rewritten for diagnostics only).
- The token is passed as a process argument; it is visible in the host's
  process table to local root only, never in logs, events, task results,
  or API responses.
- To rotate the token: create a new token in the dashboard, update
  `WORKER_TUNNEL_TOKEN` (installer input: `UAHT_TUNNEL_TOKEN`), and restart
  the worker.
- The worker's tunnel provider only ever routes to `127.0.0.1` — it cannot
  be steered at arbitrary network targets (SSRF-safe by construction).
  Origins are always the deployment's own published container host port.

## Choosing

- Host is private / behind NAT / no public IP → **tunnel**.
- You already run edge infrastructure in front of the host → **direct**.

## Security notes

- The Cloudflare API token lives only in the control plane's environment;
  the tunnel token lives only in the host's environment — agents never see
  either, and neither appears in logs, events, task results, or API
  responses.
- Only agents with `manage_domains` can add/remove hostnames; only agents
  with `deploy` can queue `ingress-sync` tasks.
- Hostname validation rejects wildcards, localhost, internal-only names,
  and IP literals — on the API and again on the worker before anything is
  written to the remote tunnel configuration.
- Ingress targets are always `http://127.0.0.1:<deployment port>` — the
  deployment's own local endpoint. Arbitrary URLs are never accepted.
- `cloudflared` is only ever executed from a SHA-256-pinned official
  release (or a copy you installed yourself on `PATH`).

## Testing without live credentials

There are no Cloudflare credentials in this repository or its test
environment. Everything except the single live-credential step above is
tested against a **fake Cloudflare API** — a local stub HTTP server in the
test suite (`control-plane/api/test/fakeCloudflare.ts`) implementing the
token/DNS/tunnel endpoints the code calls. It is test infrastructure,
clearly labeled as such, and is never used by production code (the API
defaults to `https://api.cloudflare.com/client/v4`; the override
`CLOUDFLARE_API_BASE` exists only so tests can point the client at the
stub).
