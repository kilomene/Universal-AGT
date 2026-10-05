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

export interface EnvLike {
  [key: string]: string | undefined;
}

export interface StartupConfigResult {
  /** Human-readable list of checks relaxed by the current mode (empty in production). */
  relaxed: string[];
}

const HEX_64 = /^[0-9a-fA-F]{64}$/;

export function isDevelopment(env: EnvLike = process.env): boolean {
  return env.NODE_ENV === 'development';
}

export function validateStartupConfig(env: EnvLike = process.env): StartupConfigResult {
  const dev = isDevelopment(env);
  const missing: string[] = [];
  const malformed: string[] = [];

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

  if (missing.length > 0 || malformed.length > 0) {
    const mode = dev ? 'development' : 'production';
    const parts: string[] = [];
    if (missing.length > 0) {
      parts.push(`missing required configuration: ${missing.join(', ')}`);
    }
    parts.push(...malformed);
    throw new Error(`refusing to start (${mode} mode): ${parts.join('; ')}`);
  }

  return { relaxed };
}
