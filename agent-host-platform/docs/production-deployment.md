# Production deployment (2026-10-07)

Live production deployment of Universal-AGT at pinned commit
`c24db3be5236d864d8c2866442a930721540acd3`. This document records what
was deployed, the end-to-end verification results, and the operational
learnings from the install. For the §81 spec-completion report, see
`final-production-report.md`.

## Infrastructure

| Component | Status | Details |
|-----------|--------|---------|
| Supabase | ✅ | Project `uaht-production`, region us-east-1. Migrations 001–016 applied via SQL editor; 13 tables verified. `schema_migrations` backfilled so the control plane's migration runner skips them. Connection via the IPv4 pooler (`aws-0-us-east-1.pooler.supabase.com:6543`) — the direct `db.*.supabase.co` hostname is IPv6-only. |
| Cloudflare Tunnel | ✅ | Tunnel `uaht-production`; public hostname `uaht.novamail.store` → `http://127.0.0.1:3000` (Type **HTTP** — the control plane is HTTP-only; HTTPS here returns 502). |
| Public HTTPS | ✅ | `https://uaht.novamail.store/v1/health` → `{"ok":true,"version":"1.0.0"}` |
| Control plane | ✅ | Node.js API, running under supervisord |
| Host worker | ✅ | `grok-vm-prod-01`, heartbeat OK, capabilities `docker`, `docker-compose` |
| Docker | ✅ | 29.8.2 with the **vfs** storage driver (see ops notes) |
| cloudflared | ✅ | Running under supervisord, tunnel connected via QUIC |

## Process supervision

The host has no systemd (init is `tini`), so all services run under
**supervisord**: `control-plane`, `host-worker`, `cloudflared`, `dockerd`.
supervisord itself starts on boot via a `@reboot` cron entry (see ops
notes). Secrets live in `/etc/uagt/` (mode `0600`) and are never embedded
in configs — the tunnel token is read from `/etc/uagt/tunnel.token` at
process start.

## E2E verification (all PASS)

1. **Deployment lifecycle** — test agent with `deploy` permission created;
   project + artifact uploaded via `POST /v1/artifacts/init` +
   `PUT /v1/artifacts/:id/content` (Content-Type `application/octet-stream`);
   worker claimed the task, built the image, container healthy, app
   responding.
2. **V1→V2 supersede** — new version deployed; old deployment marked
   `superseded`, new one `running`; capacity reservations transferred.
3. **Rollback** — `POST /v1/deployments/:id/rollback`; rolled-back version
   → `rolled_back`, previous version restored to `running` and serving.
4. **Worker restart** — worker restarted via supervisord; heartbeat OK
   within 10s; running containers unaffected.
5. **Container recovery** — Docker `unless-stopped` restart policy verified:
   container auto-recovered after a Docker daemon restart; worker reconcile
   on startup detects already-running containers.

## Appendix: operational notes

These are environment-specific learnings from this install, recorded so
the next deployment doesn't rediscover them.

### Docker on an overlay rootfs needs the vfs driver

If the host's root filesystem is itself `overlay` (common on container-
based VMs), Docker's default `overlayfs` storage driver cannot create its
nested overlay mounts (`invalid argument` on build). Switch to `vfs`:

```json
// /etc/docker/daemon.json
{"storage-driver": "vfs"}
```

Slower, but works everywhere. Set this before the first `docker build`.

### No systemd → supervisord + cron @reboot

On hosts without systemd, run supervisord and give it a boot hook, or a
reboot leaves everything down:

```
@reboot /home/box/.local/bin/supervisord -c /opt/uaht/supervisor/supervisord.conf
```

supervisord programs needed: `control-plane` (node), `host-worker`
(python), `cloudflared` (tunnel client), `dockerd`. Note: if supervisord
runs as non-root, `dockerd` needs a sudo wrapper (`command=/usr/bin/sudo
/usr/bin/dockerd`) since the daemon requires root.

### Never embed the tunnel token in process configs

`cloudflared tunnel ... run --token <secret>` in a supervisord config (or
any committed file) leaks the secret. Load it from a file instead:

```
command=/bin/bash -c 'exec /usr/local/bin/cloudflared tunnel --config /opt/uaht/supervisor/cloudflared.yml --no-autoupdate run --token $(cat /etc/uagt/tunnel.token)'
```

with `/etc/uagt/tunnel.token` at mode `0600`.

### Tailscale-free operator SSH via the tunnel

Operator SSH does not need Tailscale (or any inbound port). Add a tunnel
public hostname of type **SSH** → `127.0.0.1:22` (e.g.
`ssh.example.com`), harden sshd to key-only auth first
(`PasswordAuthentication no`), then connect from anywhere with:

```
ssh -o ProxyCommand="/usr/local/bin/cloudflared access ssh --hostname %h" user@ssh.example.com
```

`$SSH_CONNECTION` on the host shows `127.0.0.1` (cloudflared local) —
no tailnet involvement. With this in place, nothing in the deployment
(product or operator access) depends on Tailscale.

### Artifact upload is a two-step API

`POST /v1/artifacts/init` (JSON: `project_id`, `filename`, `size`,
`checksum` as `sha256:<hex>`, `version`, optional validated `manifest`)
returns `upload_url`; then `PUT` the bytes to that URL with
`Content-Type: application/octet-stream`. The manifest uses
`service.port` / `service.healthcheck` — not `deploy.ports`.
