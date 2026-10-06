import { describe, expect, it } from 'vitest';
import { isDevelopment, validateStartupConfig, type EnvLike } from '../src/lib/config';

// Startup configuration validator (W3): fail fast at boot with a precise
// error naming the missing variable; never silently fall back to insecure
// defaults. Production (NODE_ENV unset or anything but 'development') is
// strict; development relaxes UAHT_PROVISIONING_TOKEN only.

const GOOD: EnvLike = {
  NODE_ENV: 'production',
  DATABASE_URL: 'postgres://user:pass@localhost:5432/uagt',
  DATA_ENCRYPTION_KEY: 'a'.repeat(64),
  UAHT_PROVISIONING_TOKEN: 'prov-token',
};

function env(overrides: EnvLike): EnvLike {
  return { ...GOOD, ...overrides };
}

describe('validateStartupConfig', () => {
  it('boots clean when every required variable is present (production)', () => {
    const { relaxed } = validateStartupConfig(env({}));
    expect(relaxed).toEqual([]);
  });

  it('fails fast naming UAHT_PROVISIONING_TOKEN when it is missing in production', () => {
    const e = env({ UAHT_PROVISIONING_TOKEN: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/UAHT_PROVISIONING_TOKEN/);
  });

  it('fails fast naming DATABASE_URL when it is missing', () => {
    const e = env({ DATABASE_URL: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/DATABASE_URL/);
  });

  it('fails fast naming DATA_ENCRYPTION_KEY when it is missing', () => {
    const e = env({ DATA_ENCRYPTION_KEY: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/DATA_ENCRYPTION_KEY/);
  });

  it('names every missing variable at once, not just the first', () => {
    const e = env({
      DATABASE_URL: undefined,
      DATA_ENCRYPTION_KEY: undefined,
      UAHT_PROVISIONING_TOKEN: undefined,
    });
    let message = '';
    try {
      validateStartupConfig(e);
    } catch (err) {
      message = String(err);
    }
    expect(message).toContain('DATABASE_URL');
    expect(message).toContain('DATA_ENCRYPTION_KEY');
    expect(message).toContain('UAHT_PROVISIONING_TOKEN');
  });

  it('rejects a malformed DATA_ENCRYPTION_KEY (must be 64 hex chars)', () => {
    expect(() => validateStartupConfig(env({ DATA_ENCRYPTION_KEY: 'tooshort' }))).toThrow(
      /DATA_ENCRYPTION_KEY must be 64 hex chars/,
    );
    expect(() =>
      validateStartupConfig(env({ DATA_ENCRYPTION_KEY: 'z'.repeat(64) })),
    ).toThrow(/DATA_ENCRYPTION_KEY must be 64 hex chars/);
    // 64 hex chars, uppercase included, passes
    expect(() =>
      validateStartupConfig(env({ DATA_ENCRYPTION_KEY: 'Ab09'.repeat(16) })),
    ).not.toThrow();
  });

  it('treats an unset NODE_ENV as production (strict by default)', () => {
    const e = env({ NODE_ENV: undefined, UAHT_PROVISIONING_TOKEN: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/UAHT_PROVISIONING_TOKEN/);
  });

  it('treats an unknown NODE_ENV value as production (strict)', () => {
    const e = env({ NODE_ENV: 'staging', UAHT_PROVISIONING_TOKEN: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/UAHT_PROVISIONING_TOKEN/);
  });

  it('development relaxes UAHT_PROVISIONING_TOKEN and says so explicitly', () => {
    const e = env({ NODE_ENV: 'development', UAHT_PROVISIONING_TOKEN: undefined });
    const { relaxed } = validateStartupConfig(e);
    expect(relaxed.length).toBe(1);
    expect(relaxed[0]).toContain('UAHT_PROVISIONING_TOKEN');
    expect(relaxed[0]).toContain('bootstrap');
  });

  it('development still requires DATABASE_URL and DATA_ENCRYPTION_KEY', () => {
    const e = env({ NODE_ENV: 'development', DATABASE_URL: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/DATABASE_URL/);
    const e2 = env({ NODE_ENV: 'development', DATA_ENCRYPTION_KEY: undefined });
    expect(() => validateStartupConfig(e2)).toThrow(/DATA_ENCRYPTION_KEY/);
  });

  it('the error message states the mode', () => {
    const e = env({ DATABASE_URL: undefined });
    expect(() => validateStartupConfig(e)).toThrow(/production mode/);
    const e2 = env({ NODE_ENV: 'development', DATABASE_URL: undefined });
    expect(() => validateStartupConfig(e2)).toThrow(/development mode/);
  });

  it('never throws for optional variables alone', () => {
    // PORT, PG_POOL_MAX, CLOUDFLARE_* etc. all have defaults or are optional.
    const e: EnvLike = { ...GOOD };
    expect(() => validateStartupConfig(e)).not.toThrow();
  });
});

describe('isDevelopment', () => {
  it('is true only for NODE_ENV=development', () => {
    expect(isDevelopment({ NODE_ENV: 'development' })).toBe(true);
    expect(isDevelopment({ NODE_ENV: 'production' })).toBe(false);
    expect(isDevelopment({})).toBe(false);
    expect(isDevelopment({ NODE_ENV: 'test' })).toBe(false);
  });
});

describe('validateStartupConfig — Cloudflare / tunnel / numerics (W-C hardening)', () => {
  const TUNNEL_ID = '11111111-2222-4333-8444-555555555555';
  const TUNNEL_HOST = `${TUNNEL_ID}.cfargotunnel.com`;
  const fullTunnel = (overrides: EnvLike = {}): EnvLike =>
    env({
      TUNNEL_INGRESS_HOSTNAME: TUNNEL_HOST,
      CLOUDFLARE_TUNNEL_API_TOKEN: 'tunnel-token',
      CLOUDFLARE_ACCOUNT_ID: 'account-id',
      ...overrides,
    });

  it('accepts a fully-configured tunnel ingress', () => {
    const { warnings } = validateStartupConfig(fullTunnel());
    expect(warnings).toEqual([]);
  });

  it('rejects a malformed TUNNEL_INGRESS_HOSTNAME (invalid tunnel config)', () => {
    expect(() => validateStartupConfig(fullTunnel({ TUNNEL_INGRESS_HOSTNAME: 'not-a-tunnel-host' }))).toThrow(
      /TUNNEL_INGRESS_HOSTNAME must be shaped like/,
    );
  });

  it('names the missing tunnel credentials when ingress is enabled without them', () => {
    let message = '';
    try {
      validateStartupConfig(env({ TUNNEL_INGRESS_HOSTNAME: TUNNEL_HOST }));
    } catch (err) {
      message = String(err);
    }
    expect(message).toContain('CLOUDFLARE_TUNNEL_API_TOKEN');
    expect(message).toContain('CLOUDFLARE_ACCOUNT_ID');
  });

  it('falls back to CLOUDFLARE_API_TOKEN for the tunnel credential', () => {
    // CLOUDFLARE_API_TOKEN doubles as the tunnel credential here, so the
    // zone must travel with it (DNS half-config check).
    const e = fullTunnel({
      CLOUDFLARE_TUNNEL_API_TOKEN: undefined,
      CLOUDFLARE_API_TOKEN: 'dns-token',
      CLOUDFLARE_ZONE_ID: 'zone-id',
    });
    expect(() => validateStartupConfig(e)).not.toThrow();
  });

  it('rejects a half-configured Cloudflare DNS (token without zone)', () => {
    expect(() => validateStartupConfig(env({ CLOUDFLARE_API_TOKEN: 'tok' }))).toThrow(
      /CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID must be set together/,
    );
    expect(() => validateStartupConfig(env({ CLOUDFLARE_ZONE_ID: 'zone' }))).toThrow(
      /CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID must be set together/,
    );
  });

  it('warns (not fails) when DNS credentials have no ingress target', () => {
    const { warnings } = validateStartupConfig(
      env({ CLOUDFLARE_API_TOKEN: 'tok', CLOUDFLARE_ZONE_ID: 'zone' }),
    );
    expect(warnings.some((w) => w.includes('dns_pending'))).toBe(true);
  });

  it('rejects an invalid PUBLIC_INGRESS_HOSTNAME', () => {
    expect(() => validateStartupConfig(env({ PUBLIC_INGRESS_HOSTNAME: 'not a hostname!' }))).toThrow(
      /PUBLIC_INGRESS_HOSTNAME is not a valid hostname/,
    );
  });

  it('rejects invalid numerics (PORT, rate limits)', () => {
    expect(() => validateStartupConfig(env({ PORT: 'notaport' }))).toThrow(/PORT must be an integer/);
    expect(() => validateStartupConfig(env({ PORT: '70000' }))).toThrow(/PORT must be an integer/);
    expect(() => validateStartupConfig(env({ RATE_LIMIT_AGENT_PER_MIN: '0' }))).toThrow(
      /RATE_LIMIT_AGENT_PER_MIN must be a positive integer/,
    );
    expect(() => validateStartupConfig(env({ UAHT_ROTATE_RATE_PER_MIN: '-3' }))).toThrow(
      /UAHT_ROTATE_RATE_PER_MIN must be a positive integer/,
    );
  });

  it('rejects conflicting heartbeat thresholds (degraded >= offline)', () => {
    expect(() =>
      validateStartupConfig(env({ HEARTBEAT_DEGRADED_AFTER_S: '300', HEARTBEAT_OFFLINE_AFTER_S: '300' })),
    ).toThrow(/conflicting heartbeat thresholds/);
  });

  it('warns on LOG_LEVEL=debug in production (unsafe debug mode)', () => {
    const { warnings } = validateStartupConfig(env({ LOG_LEVEL: 'debug' }));
    expect(warnings.some((w) => w.includes('LOG_LEVEL=debug'))).toBe(true);
  });

  it('never prints secret values in error messages', () => {
    const secretValue = 's3cr3t-tunnel-token-value';
    let message = '';
    try {
      validateStartupConfig(
        env({ TUNNEL_INGRESS_HOSTNAME: TUNNEL_HOST, CLOUDFLARE_TUNNEL_API_TOKEN: secretValue }),
      );
    } catch {
      // configured fine — force a different failure instead
    }
    try {
      validateStartupConfig(
        env({
          TUNNEL_INGRESS_HOSTNAME: TUNNEL_HOST,
          CLOUDFLARE_ACCOUNT_ID: 'x',
          DATA_ENCRYPTION_KEY: secretValue,
        }),
      );
    } catch (err) {
      message = String(err);
    }
    expect(message).not.toContain(secretValue);
    expect(message).toContain('DATA_ENCRYPTION_KEY');
  });
});
