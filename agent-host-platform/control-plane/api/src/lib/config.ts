// Startup configuration validation for the control-plane API.
//
// Run once at boot (main() in index.ts) BEFORE opening the DB pool or
// listening: fail fast with a precise error naming the missing variable,
// instead of crashing lazily on the first request that needs it.
//
// Modes:
// - production (default — NODE_ENV unset or anything except 'development'):
//   strict. Every required variable must be present and well-formed.
// - development (NODE_ENV=development): exactly ONE check is relaxed —
//   UAHT_PROVISIONING_TOKEN may be unset, which puts agent registration
//   into bootstrap mode (only the very first registration is open).
//   DATABASE_URL and DATA_ENCRYPTION_KEY stay required in development too:
//   the API cannot serve anything without a database, and secrets cannot
//   be encrypted without the key. There is no insecure fallback in either
//   mode.
//
// The validator also rejects malformed optional configuration (invalid
// tunnel config, half-configured Cloudflare DNS, bad numerics, conflicting
// heartbeat thresholds) and reports non-fatal misconfigurations as
// warnings. Secret VALUES are never printed — only variable names.

import { isValidHostname } from './cloudflare';
import { tunnelIdFromIngressHostname } from './cloudflare-tunnel';

export interface EnvLike {
  [key: string]: string | undefined;
}

export interface StartupConfigResult {
  /** Human-readable list of checks relaxed by the current mode (empty in production). */
  relaxed: string[];
  /** Non-fatal misconfigurations: logged as warnings at boot, never fatal. */
  warnings: string[];
}

const HEX_64 = /^[0-9a-fA-F]{64}$/;

export function isDevelopment(env: EnvLike = process.env): boolean {
  return env.NODE_ENV === 'development';
}

/** Positive integer env value, or null when unset/invalid. */
function positiveInt(env: EnvLike, name: string): number | null {
  const raw = env[name];
  if (raw === undefined || raw === '') return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n <= 0) return null;
  return n;
}

function validPort(raw: string | undefined): boolean {
  if (raw === undefined || raw === '') return true; // unset -> default
  const n = Number(raw);
  return Number.isInteger(n) && n >= 1 && n <= 65535;
}

export function validateStartupConfig(env: EnvLike = process.env): StartupConfigResult {
  const dev = isDevelopment(env);
  const missing: string[] = [];
  const malformed: string[] = [];
  const warnings: string[] = [];

  if (!env.DATABASE_URL) {
    missing.push('DATABASE_URL');
  }

  const dek = env.DATA_ENCRYPTION_KEY;
  if (!dek) {
    missing.push('DATA_ENCRYPTION_KEY');
  } else if (!HEX_64.test(dek)) {
    malformed.push(
      'DATA_ENCRYPTION_KEY must be 64 hex chars (32 bytes) — generate with: openssl rand -hex 32',
    );
  }

  const provisioning = env.UAHT_PROVISIONING_TOKEN;
  const relaxed: string[] = [];
  if (!provisioning) {
    if (dev) {
      relaxed.push(
        'UAHT_PROVISIONING_TOKEN is not set: agent registration is open only until the first agent registers (bootstrap mode)',
      );
    } else {
      missing.push('UAHT_PROVISIONING_TOKEN');
    }
  }

  // --- Tunnel ingress (§59: invalid tunnel config, missing credentials) ---
  const tunnelHostname = env.TUNNEL_INGRESS_HOSTNAME;
  if (tunnelHostname) {
    if (!tunnelIdFromIngressHostname(tunnelHostname)) {
      malformed.push(
        'TUNNEL_INGRESS_HOSTNAME must be shaped like <tunnel-id>.cfargotunnel.com',
      );
    }
    const tunnelToken = env.CLOUDFLARE_TUNNEL_API_TOKEN || env.CLOUDFLARE_API_TOKEN;
    if (!tunnelToken) {
      missing.push(
        'CLOUDFLARE_TUNNEL_API_TOKEN (or CLOUDFLARE_API_TOKEN with the tunnel permission) — required when TUNNEL_INGRESS_HOSTNAME is set',
      );
    }
    if (!env.CLOUDFLARE_ACCOUNT_ID) {
      missing.push('CLOUDFLARE_ACCOUNT_ID — required when TUNNEL_INGRESS_HOSTNAME is set');
    }
  }

  // --- Cloudflare DNS (§59: half-configured DNS, invalid URLs) ---
  const dnsToken = env.CLOUDFLARE_API_TOKEN;
  const zoneId = env.CLOUDFLARE_ZONE_ID;
  if ((dnsToken && !zoneId) || (!dnsToken && zoneId)) {
    malformed.push(
      'CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID must be set together for DNS management',
    );
  }
  if (dnsToken && zoneId && !tunnelHostname && !env.PUBLIC_INGRESS_HOSTNAME) {
    warnings.push(
      'CLOUDFLARE_API_TOKEN + CLOUDFLARE_ZONE_ID are set but no ingress target ' +
        '(PUBLIC_INGRESS_HOSTNAME / TUNNEL_INGRESS_HOSTNAME) is configured: ' +
        'domains will be stored as metadata only (status dns_pending)',
    );
  }
  if (env.PUBLIC_INGRESS_HOSTNAME && !isValidHostname(env.PUBLIC_INGRESS_HOSTNAME)) {
    malformed.push('PUBLIC_INGRESS_HOSTNAME is not a valid hostname');
  }

  // --- Numerics (§59: invalid URLs/ports, conflicting settings) ---
  if (!validPort(env.PORT)) {
    malformed.push('PORT must be an integer 1..65535');
  }
  for (const name of [
    'PG_POOL_MAX',
    'RATE_LIMIT_AGENT_PER_MIN',
    'RATE_LIMIT_HOST_PER_MIN',
    'RATE_LIMIT_UNAUTH_PER_MIN',
    'UAHT_ROTATE_RATE_PER_MIN',
  ]) {
    const raw = env[name];
    if (raw !== undefined && raw !== '' && positiveInt(env, name) === null) {
      malformed.push(`${name} must be a positive integer`);
    }
  }
  const degradedAfter = positiveInt(env, 'HEARTBEAT_DEGRADED_AFTER_S');
  const offlineAfter = positiveInt(env, 'HEARTBEAT_OFFLINE_AFTER_S');
  if (
    env.HEARTBEAT_DEGRADED_AFTER_S !== undefined &&
    env.HEARTBEAT_OFFLINE_AFTER_S !== undefined &&
    degradedAfter !== null &&
    offlineAfter !== null &&
    degradedAfter >= offlineAfter
  ) {
    malformed.push(
      'conflicting heartbeat thresholds: HEARTBEAT_DEGRADED_AFTER_S must be less than HEARTBEAT_OFFLINE_AFTER_S',
    );
  }

  // --- Debug mode (§59: unsafe debug mode) ---
  if (!dev && (env.LOG_LEVEL ?? '').toLowerCase() === 'debug') {
    warnings.push(
      'LOG_LEVEL=debug in production: debug output is verbose; keep it off unless actively troubleshooting',
    );
  }

  if (missing.length > 0 || malformed.length > 0) {
    const mode = dev ? 'development' : 'production';
    const parts: string[] = [];
    if (missing.length > 0) {
      parts.push(`missing required configuration: ${missing.join(', ')}`);
    }
    parts.push(...malformed);
    throw new Error(`refusing to start (${mode} mode): ${parts.join('; ')}`);
  }

  return { relaxed, warnings };
}
