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
