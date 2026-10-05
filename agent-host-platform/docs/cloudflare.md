# Cloudflare DNS (optional, Phase 5)

Domain routing is **optional**. Deployments work fine without it; use it when
you want public hostnames like `api.example.com` for your apps.

## The ingress model

This system never stores host IP addresses — not in the database, not in the
API, not in the worker. That is deliberate (agents are temporary, hosts are
opaque). Consequence: **the control plane cannot create A-records pointing at
a host**, because it doesn't know any.

Instead, DNS is a CNAME to an ingress you operate:

```
api.example.com  --CNAME-->  ingress.example.net
                                  |
                     (your tunnel / reverse proxy / LB)
                                  |
                          persistent-host-01
```

Good ingress choices:

- `cloudflared` tunnel running on the host (no open ports at all)
- A reverse proxy (Caddy/Nginx) on a small VPS you control
- Your cloud load balancer

## Setup

1. Create a Cloudflare API token scoped to **Zone → DNS → Edit** on exactly
   the zone that holds your hostnames.
2. Set on the control plane:
   - `CLOUDFLARE_API_TOKEN` — the scoped token
   - `CLOUDFLARE_ZONE_ID` — the zone id
   - `PUBLIC_INGRESS_HOSTNAME` — e.g. `ingress.example.net`
3. Attach a domain (needs the `manage_domains` permission):

```bash
agent-host domains add --deployment <id> --hostname api.example.com
# or: curl -X POST $UAHT_BASE_URL/v1/domains \
#       -H "Authorization: Bearer $UAHT_API_KEY" \
#       -H 'Content-Type: application/json' \
#       -d '{"deployment_id":"<uuid>","hostname":"api.example.com"}'
```

The API creates a proxied (orange-cloud) CNAME `api.example.com → ingress`
with TTL 300. Re-adding is idempotent; removing the domain
(`agent-host domains rm` / `DELETE /v1/domains`) deletes the DNS record.

## Without Cloudflare configured

`POST /v1/domains` still works: the hostname is stored on the deployment as
metadata (`status: 'dns_pending'`) and an event records that DNS was skipped.
Point your DNS at the ingress manually.

## Security notes

- The Cloudflare token lives only in the control plane's environment —
  agents never see it, and it never appears in logs, events, or API responses.
- Only agents with `manage_domains` can add/remove hostnames.
- Hostname validation rejects anything that isn't a well-formed DNS name.
