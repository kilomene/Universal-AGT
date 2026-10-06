# W16 Final Security Audit — adversarial review

Date: 2026-10-05/06. Scope: control-plane API (`control-plane/api`) and host
worker (`host-worker`), per spec §54–§57. Method: attack-first — for each
area an exploit was attempted with a failing test BEFORE any fix; only
demonstrated vulnerabilities were fixed.

> **Note 2026-10-06 (WS-J):** the line below about GitHub Actions is stale.
> CI (`.github/workflows/ci.yml`, 12 jobs) now runs on hosted runners and
> is green — the security suites run both locally and in the `security`
> CI job, and the full migration chain + API smoke run against a real
> PostgreSQL 16 service container in the `e2e` job. The findings below
> stand as written.

## Results at a glance

| # | Area | Attack attempted | Verdict |
|---|------|------------------|---------|
| F1 | Cloudflare DNS | Adopt operator's CNAME, then DELETE it | **VULN → fixed** |
| F2 | Tunnel routing | Hijack operator's remote tunnel route | **VULN → fixed** |
| F3 | HTTPS probe | Redirect-following SSRF via agent hostname | Hardened |
| F4 | Error handling | Malformed JSON → 500 | **Fixed** (400/413) |
| W1 | Compose validation | `container:` namespace sharing | **VULN → fixed** |
| W2 | Manifest validation | 5000-digit memory / NaN cpu | **Fixed** |
| — | Auth | Timing, entropy, rotation, revocation | Held (tests added) |
| — | Transactions | Fault-injection atomicity | Discipline verified |
| — | Rate limits | Trigger on creation endpoints | Verified |
| — | IDOR / cross-host | Every host-identity boundary | Held (tests added) |
| — | SSRF | Outbound-call audit | Allowlisted; residual noted |
| — | Secrets | Logs/errors/DB/API re-audit | Clean |

## Findings and fixes

### F1 — DNS record adoption → operator record deletion (HIGH)
`src/lib/cloudflare.ts` `ensureCnameRecord()` adopted ANY pre-existing CNAME
for a claimed hostname without checking its target, storing the operator's
record id on the domain row. `removeDomain()` then deleted that id on
`DELETE /v1/domains` — an agent with `manage_domains` could delete DNS
records it never created.
Demonstrated: `test/securityAuditW16.test.ts` → "F1" (pre-fix: operator
record gone after the attacker's DELETE).
Fix: adoption now requires the existing record's content to match the
desired ingress target (case-insensitive, trailing-dot-insensitive);
mismatch → `HttpError 409`. `CfResult` gained the `content` field the real
Cloudflare API always returns.
Files: `control-plane/api/src/lib/cloudflare.ts`.

### F2 — Remote tunnel route hijack (HIGH)
`src/lib/cloudflare-tunnel.ts` `setRemoteTunnelIngress()` overwrote ANY
remote rule matching a desired hostname — including rules the operator
manages outside this system. An agent with `manage_domains` claiming
`blog.example.com` silently rewrote its route from `https://external-cms:443`
to `http://127.0.0.1:<attacker port>`: full traffic steal of an operator
hostname in the same tunnel.
Demonstrated: `test/securityAuditW16.test.ts` → "F2" (pre-fix: remote rule
rewritten to the attacker's loopback).
Fix: a desired hostname whose remote rule exists but is NOT in the system's
ownership set (`managed`, from the domains table) now throws `409` instead
of being overwritten. `collectDesiredRoutes()` deliberately EXCLUDES the
in-flight (`extra`) hostname from `managed` — a first-time claim must never
take over a foreign rule. Legit re-provisions of system-owned rules still
update in place (pinned by test).
Files: `control-plane/api/src/lib/cloudflare-tunnel.ts`;
`control-plane/api/test/cloudflareTunnel.test.ts` (one assertion updated to
the hardened contract), `control-plane/api/test/cloudflare.test.ts` (mock
made faithful + new 409 test).

### F3 — HTTPS reachability probe followed redirects (MEDIUM-LOW)
`src/routes/domains.ts` `probeHttps()` fetched `https://<agent-hostname>/`
with `redirect: 'follow'`. A redirect to an internal URL would make the
control plane request an attacker-chosen target (SSRF). Exploitability is
narrow (hostname allowlisted; the CNAME points at the operator ingress), but
following was never needed — the probe is informational.
Fix: `redirect: 'manual'`; a 3xx still counts as "reachable" with its
status recorded. `probeHttps` is now exported for the unit test.
Files: `control-plane/api/src/routes/domains.ts`.

### F4 — Malformed/oversized JSON → 500 (LOW)
`apiErrorHandler` (`src/middleware/auth.ts`) mapped body-parser failures to
generic 500s. Now: `entity.parse.failed` → `400 bad_request`, and
`entity.too.large` (over the 256kb JSON cap) → `413 payload_too_large`.
Files: `control-plane/api/src/middleware/auth.ts`.

### W1 — Compose `container:` namespace sharing (MEDIUM)
`host-worker/deployments/compose_validate.py` denied only the literal
`host` for `network_mode`/`pid`/`ipc`. `network_mode: "container:<any>"`,
`pid: "container:<any>"`, `ipc: "container:<any>"` — which join the
network/PID/IPC namespace of ANY container on the host (other deployments,
infrastructure) — passed validation.
Demonstrated: `host-worker/tests/test_w16_security_audit.py`
`TestNamespaceSharing` (pre-fix: all four passed validation).
Fix: values starting with `container:` denied on `network_mode`, `pid`,
`ipc`. `service:<name>` stays allowed (sibling service in the same compose
file — already fully agent-controlled). `x-` extension burial verified
INERT (compose ignores `x-` at runtime; validator correctly ignores them —
pinned so nobody "fixes" it into executing extension content).
Files: `host-worker/deployments/compose_validate.py`.

### W2 — Manifest validation gaps (LOW)
`host-worker/deployments/manifest.py`:
- `resources.memory` regex `^\d+[mMgG]$` accepted a 5000-digit value, which
  then crashed `int()` (Python 3.11+ caps str→int at 4300 digits) deep in
  the deploy pipeline. Now `^\d{1,6}[mMgG]$`, and `parse_memory_mb` guards
  the conversion defensively.
- `resources.cpu: NaN` (Python's `json` parses `NaN`) passed the "positive
  number" check. Now rejected via `math.isfinite`.
Files: `host-worker/deployments/manifest.py`.

## Areas verified WITHOUT finding vulnerabilities

- **Authentication (§54).** `tokens.ts`: SHA-256 of 256-bit `randomBytes(32)`
  (`uag_`/`uagh_` prefixes), constant-time `verifyToken` via
  `timingSafeEqual`, plaintext shown once / hash-only storage. Rotation
  invalidates immediately (agent: `UPDATE api_key_hash`; host: old hash
  cleared unless an explicit 1–3600s grace is requested). Revocation tested
  across heartbeat, claim, progress, secrets, and port-settlement — old
  token 401s everywhere the moment rotation commits. New: "host token
  revocation" + "token primitives" tests.
- **Authorization.** Flat permission model is by design (no per-agent
  project ownership — agents are untrusted clients, permissions are the
  boundary). Verified: host tokens 403 on every agent endpoint and vice
  versa; per-endpoint permission matrix; task-type→permission map stays
  closed; `docker-run` tasks reject `extra_args`; heartbeat/claim/progress/
  secrets/artifact-download/settle-rollback-ports all enforce host
  self-identity (`requireHost` + path/body id match). New: cross-host IDOR
  test on `settle-rollback-ports` (the newest host endpoint).
- **Dashboard auth.** Key in `sessionStorage` only, `Authorization: Bearer`
  header on every fetch; `?api_key=` honored ONLY on
  `GET /v1/events/stream` (EventSource can't set headers) — verified the
  server ignores it elsewhere. Request logs use `req.path` (no query
  string), so the key doesn't land in logs.
- **Docker.** `DockerClient.run()` has no `extra_args` and no privileged/
  mounts/socket/caps/security-opt parameters at all — the deny-list holds
  structurally, not by string matching. Compose path validates the
  NORMALIZED model (`docker compose config --format json`) against the
  explicit deny-list on BOTH entry points (deploy pipeline, `docker-compose`
  task handler) before any `up`; compose subprocesses run with a scrubbed
  env (`SAFE_COMPOSE_ENV_KEYS`) so `${WORKER_HOST_TOKEN}` interpolation
  can't exfiltrate the host token. Adversarial payloads tested (W1 above,
  plus caps/devices/socket/mount escapes in the existing suite).
- **Filesystem.** Artifact extraction: tar pre-scan (absolute/`..` fail
  fast) + `tarfile.data_filter` (symlink/hardlink escapes); zip pre-scan
  (zipfile never materializes symlinks). Tricky members re-verified:
  `....//`, backslash literals, unicode lookalikes, absolute+`..` combos —
  escapes rejected, literal names stay inside. `confined_under` /
  `_confined_path` on every manifest/handler path; artifact-upload refuses
  `<work_dir>/config` (host token lives there).
- **Network / SSRF (§).** Agent-influenced outbound calls enumerated:
  `probeHttps` (F3, hardened), `cfApi` (base URL is env-only
  `CLOUDFLARE_API_BASE`, no agent override path; per-call auth header, no
  token in errors/logs), worker healthchecks (loopback-locked
  `http://127.0.0.1:<port><path>`, path forced relative), artifact
  up/download (control-plane URL + id only), cloudflared download
  (hardcoded URL + SHA-256 pinned). `repository` is inert metadata (never
  fetched). Verified with a stubbed-fetch test that domain creation with
  Cloudflare unconfigured makes ZERO outbound calls.
- **Secrets.** Re-audited rotation grace, domain lifecycle, port
  settlement, and recovery paths: events carry names/ids/counts only;
  `secret.changed`/`secret.deleted`/`host.token_rotated`/
  `agent.key_rotated` payloads contain no values; error messages truncated
  and value-free; AES-256-GCM with random nonces at rest.
- **API (§55/§56/§57).** Rate limiting: global `rateLimit` on the whole
  `/v1` router (120/min agent, 600/min host, 10/min unauthenticated IP)
  plus a stricter `rotationRateLimit` on both rotation endpoints — trigger
  verified on project creation, task creation, and per-credential isolation.
  Payload validation: deployment-manifest validator fuzzed (never raises,
  rejects malformed); oversized JSON → 413; malformed JSON → 400.
  Transactions: fault-injection tests prove `POST /v1/deployments`
  (deployment+task+port) and `settleRollbackPorts` (release+restore) issue
  `BEGIN … ROLLBACK` with no `COMMIT` on mid-transaction failure — the
  exact statement sequences were asserted.

## Test counts

- API (vitest): baseline 291 passed → now **311 passed** (23 files).
  New: `test/securityAuditW16.test.ts` (19 tests). Updated:
  `test/cloudflare.test.ts` (+1), `test/cloudflareTunnel.test.ts`
  (hardened-contract assertion).
- Worker (pytest): baseline 398 passed → now **466 passed**.
  New: `tests/test_w16_security_audit.py` (68 tests).

## Residual risks (explicit)

1. **pg-mem cannot ROLLBACK.** The transaction-atomicity tests assert
   statement sequences (`BEGIN … ROLLBACK`, no `COMMIT`), not post-failure
   DB state — pg-mem 3.0.5's adapter does not implement rollback. True
   end-to-end atomicity needs a real PostgreSQL fault-injection run.
2. **Tunnel reconcile TOCTOU.** `setRemoteTunnelIngress` is
   read-modify-write; an operator rule created between our GET and PUT for
   the same hostname could still be overwritten. No versioning/ETag is
   available on the Cloudflare tunnel-config endpoint.
3. **DNS rebinding vs the HTTPS probe.** `isValidHostname` rejects IP
   literals and internal suffixes, but a claimed hostname's authoritative
   DNS could still resolve to internal space in a race window (the CNAME
   the system creates normally pins resolution to the ingress target).
   A resolving-then-filtering probe (reject RFC-1918/loopback/link-local
   post-resolution) would close this fully.
4. **Compose validation TOCTOU.** The file is validated via
   `compose config` then `compose up` reads the same path; an agent has no
   write access to the worker filesystem, so the window is not reachable
   through the API — but a compromised host could race it.
5. **Flat permission model.** Any agent with `manage_domains` can attach a
   hostname to ANY deployment and any agent with `deploy` can deploy to
   ANY project/host. This is the documented protocol design (agents are
   trusted per-permission, not per-tenant), not a bug — but it means a
   compromised agent key has broad blast radius. Per-agent scoping would be
   a protocol change.
6. **No live infra.** Cloudflare API behavior (record `content` on reads,
   409 vs 81044 semantics, tunnel-config concurrency) was verified against
   the in-repo fake, not the real API. The `ensureCnameRecord` target-match
   assumes `content` reflects the origin target for proxied records (true
   per Cloudflare docs; untested live).
7. **`?api_key=` on the SSE stream** ends up in browser history and any
   reverse-proxy logs that capture query strings — accepted trade-off
   (EventSource limitation), documented in code.
