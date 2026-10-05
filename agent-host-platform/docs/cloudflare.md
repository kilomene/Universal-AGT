# Public ingress & Cloudflare DNS (Phase 7)

## The one thing you must understand first

**DNS does not make an outbound-only host reachable.**

The persistent host dials *out* to the control plane and never listens for
inbound connections — nothing outside can initiate a connection *to* it.
A CNAME record is just a name pointing at another name. If nothing at the
other end can carry traffic back down to the host, the domain resolves and
the connection goes nowhere.

So there are three modes, and you must pick one deliberately:

| Mode | What it is | When traffic actually reaches the host |
|---|---|---|
| (a) **metadata-only** (default) | `POST /v1/domains` records the hostname on the deployment; no DNS is touched (`status: 'dns_pending'`). | **Never.** The hostname is bookkeeping only. |
| (b) **direct** | CNAME `hostname → PUBLIC_INGRESS_HOSTNAME` (proxied, TTL 300). | Only if **you** run an ingress in front of the host — a reverse proxy, load balancer, or tunnel you manage — and it can reach the host's container ports. That ingress is **your responsibility**; this system never sees host IPs and cannot check it for you. |
| (c) **cloudflare-tunnel** *(recommended for the private-host case)* | CNAME `hostname → TUNNEL_INGRESS_HOSTNAME` (e.g. `<tunnel-id>.cfargotunnel.com`), and the **worker itself** runs a supervised `cloudflared` tunnel — an outbound-only connection to Cloudflare's edge that carries inbound HTTP back to the host's loopback container ports. | **Yes** — once the tunnel is up and the route is synced (see below). This is the only mode where this system owns the whole path. |

**Without (b) or (c), domains resolve but traffic cannot reach the host.**
That is not a bug or a missing feature — it is the architecture (spec
§28). Do not pretend otherwise in runbooks or status pages.

## Mode (a): metadata-only

Configure nothing. `POST /v1/domains` stores the hostname; point your DNS
at your own ingress manually. The entry keeps `status: 'dns_pending'` until
you manage DNS yourself.

## Mode (b): direct (your own ingress)

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
ingress.example.net` with TTL 300. Re-adding is idempotent; removing the
domain (`agent-host domains rm` / `DELETE /v1/domains`) deletes the DNS
record. The worker is not involved: your ingress must already route to the
host's container ports.

## Mode (c): cloudflare-tunnel (worker-managed, outbound-only)

The worker supervises `cloudflared tunnel --token <TOKEN> run`. The tunnel
dials **out** to Cloudflare; no inbound ports are opened on the host. For
each tunnel-mode domain on a live deployment, the worker maps
`hostname → http://127.0.0.1:<container-port>` and rewrites the tunnel's
`config.yml` route table.

### Setup

**Human steps (cannot be automated — they live in your Cloudflare account):**

1. In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels →
   Create a tunnel** (Cloudflared type). Name it, e.g. `agent-host-ingress`.
2. Copy the **tunnel token** shown on the install step (a JWT starting with
   `eyJ…`). Treat it like a password.
3. Find the tunnel hostname: on the tunnel's overview it looks like
   `<tunnel-id>.cfargotunnel.com`. Copy it.
4. In the tunnel's **Public hostnames** tab, add one entry per app hostname
   you will serve (e.g. `api.example.com` → service `http://localhost:8080`
   — the port is a placeholder; the worker's route table is the source of
   truth for which local port each hostname reaches). **This dashboard step
   is required**: for dashboard-created (token) tunnels, Cloudflare reads
   public-hostname routes from the tunnel's dashboard configuration, not
   from the worker-generated `config.yml`.

**Control plane:**

```bash
CLOUDFLARE_API_TOKEN=<scoped token>   # Zone → DNS → Edit on your zone
CLOUDFLARE_ZONE_ID=<zone id>
TUNNEL_INGRESS_HOSTNAME=<tunnel-id>.cfargotunnel.com   # from step 3
```

**Host** (`/etc/uagt/worker.env`, mode `0600`):

```bash
WORKER_INGRESS_ENABLED=true
WORKER_INGRESS_PROVIDER=cloudflare-tunnel
UAHT_TUNNEL_TOKEN=<tunnel token from step 2>   # never in the repo, never logged
```

On startup the worker downloads `cloudflared` (pinned release, SHA-256
verified — no auto-trust of "latest") if it isn't on `PATH`, then starts the
supervised tunnel. If the download fails or the token is missing, the worker
**logs clearly and stays with ingress disabled** — deployments keep working.

**Request domains:**

```bash
agent-host domains add --deployment <id> --hostname api.example.com --ingress tunnel
# or POST /v1/domains {"deployment_id":"<uuid>","hostname":"api.example.com","ingress":"tunnel"}
```

What happens:

1. The API creates the proxied CNAME `api.example.com →
   <tunnel-id>.cfargotunnel.com` (422 if you request tunnel mode without
   `TUNNEL_INGRESS_HOSTNAME` configured — the error says so plainly).
2. The domain entry is stored with `ingress: 'tunnel'` and the tunnel host.
3. The API queues an `ingress-sync` task for the host; the worker rebuilds
   its route table from all current tunnel-mode domains and rewrites
   `config.yml`. (Deploys/removes also trigger a best-effort in-process
   sync; `ingress-sync` tasks are idempotent and safe to re-run or trigger
   manually.)

Verify:

```bash
agent-host tasks --status completed   # the ingress-sync task completed
# on the host:
cat /opt/agent-host/ingress/cloudflared/config.yml   # hostname -> 127.0.0.1:port
```

Then open `https://api.example.com` — traffic flows: client → Cloudflare
edge → (outbound tunnel) → `cloudflared` on the host → `127.0.0.1:<port>`.

### Tunnel behavior notes

- Crash supervision: if `cloudflared` exits unexpectedly, the worker
  restarts it with exponential backoff. Route changes rewrite `config.yml`
  and restart the tunnel (cloudflared has no SIGHUP config reload; restart
  is the real mechanism) — intentional restarts don't count as crashes.
- The token is passed as a process argument; it is visible in the host's
  process table to local root only, never in logs, events, task results,
  or API responses.
- To rotate the token: create a new token in the dashboard, update
  `UAHT_TUNNEL_TOKEN`, and either restart the worker or queue an
  `ingress-sync` task (the restart picks up the new token).

## Choosing

- Host is private / behind NAT / no public IP → **(c) cloudflare-tunnel**.
- You already run edge infrastructure in front of the host → **(b) direct**.
- You only need the hostname recorded for later → **(a) metadata-only**.

## Security notes

- The Cloudflare API token lives only in the control plane's environment;
  the tunnel token lives only in the host's environment — agents never see
  either, and neither appears in logs, events, or API responses.
- Only agents with `manage_domains` can add/remove hostnames; only agents
  with `deploy` can queue `ingress-sync` tasks.
- Hostname validation rejects anything that isn't a well-formed DNS name,
  on the API and again on the worker before it reaches `config.yml`.
- The worker's tunnel provider only ever routes to `127.0.0.1` — it cannot
  be steered at arbitrary network targets.
- `cloudflared` is only ever executed from a SHA-256-pinned official
  release (or a copy you installed yourself on `PATH`).
