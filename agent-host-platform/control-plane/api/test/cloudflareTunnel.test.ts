// Unit tests for lib/cloudflare-tunnel.ts — the REMOTE routing authority.
//
// These test the read-modify-write reconcile against a mocked fetch
// (no network). The full flow against a fake HTTP Cloudflare is covered
// in test/domains.test.ts.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HttpError } from '../src/lib/errors';
import {
  collectDesiredRoutes,
  deploymentHostPort,
  getRemoteTunnelIngress,
  getTunnelApiConfig,
  originServiceFor,
  setRemoteTunnelIngress,
  tunnelIdFromIngressHostname,
  type TunnelApiConfig,
} from '../src/lib/cloudflare-tunnel';

const TUNNEL_ID = '11111111-2222-3333-4444-555555555555';
const TUNNEL_HOST = `${TUNNEL_ID}.cfargotunnel.com`;
const CFG: TunnelApiConfig = { token: 'tok', accountId: 'acct', tunnelId: TUNNEL_ID };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const k of [
    'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_TUNNEL_API_TOKEN',
    'CLOUDFLARE_ACCOUNT_ID', 'TUNNEL_INGRESS_HOSTNAME',
  ]) delete process.env[k];
});

function mockCfFetch(handler: (method: string, path: string, body?: unknown) => unknown) {
  const calls: Array<{ method: string; path: string; body?: unknown }> = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const path = String(url).replace('https://api.cloudflare.com/client/v4', '');
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return {
      ok: true,
      status: 200,
      json: () => Promise.resolve(handler(method, path, calls[calls.length - 1].body)),
    };
  });
  vi.stubGlobal('fetch', fn);
  return calls;
}

const ok = (result: unknown) => ({ success: true, result });

describe('tunnelIdFromIngressHostname', () => {
  it('parses the tunnel id out of the cfargotunnel hostname', () => {
    expect(tunnelIdFromIngressHostname(TUNNEL_HOST)).toBe(TUNNEL_ID);
    expect(tunnelIdFromIngressHostname(TUNNEL_HOST.toUpperCase())).toBe(TUNNEL_ID);
  });
  it('returns null for anything else', () => {
    expect(tunnelIdFromIngressHostname('ingress.example.net')).toBeNull();
    expect(tunnelIdFromIngressHostname('not-a-uuid.cfargotunnel.com')).toBeNull();
    expect(tunnelIdFromIngressHostname(null)).toBeNull();
  });
});

describe('getTunnelApiConfig', () => {
  it('returns null unless token + account + parseable tunnel hostname are all set', () => {
    expect(getTunnelApiConfig()).toBeNull();
    process.env.CLOUDFLARE_TUNNEL_API_TOKEN = 'tt';
    process.env.CLOUDFLARE_ACCOUNT_ID = 'a';
    expect(getTunnelApiConfig()).toBeNull(); // no tunnel hostname yet
    process.env.TUNNEL_INGRESS_HOSTNAME = 'ingress.example.net';
    expect(getTunnelApiConfig()).toBeNull(); // not a tunnel hostname
    process.env.TUNNEL_INGRESS_HOSTNAME = TUNNEL_HOST;
    expect(getTunnelApiConfig()).toEqual({ token: 'tt', accountId: 'a', tunnelId: TUNNEL_ID });
  });
  it('falls back to CLOUDFLARE_API_TOKEN when no dedicated tunnel token is set', () => {
    process.env.CLOUDFLARE_API_TOKEN = 'dns-token';
    process.env.CLOUDFLARE_ACCOUNT_ID = 'a';
    process.env.TUNNEL_INGRESS_HOSTNAME = TUNNEL_HOST;
    expect(getTunnelApiConfig()).toEqual({ token: 'dns-token', accountId: 'a', tunnelId: TUNNEL_ID });
  });
});

describe('originServiceFor', () => {
  it('builds a loopback-only origin URL', () => {
    expect(originServiceFor(18001)).toBe('http://127.0.0.1:18001');
  });
  it('refuses bad ports instead of building a URL', () => {
    for (const bad of [0, 65536, -1, 3.5, NaN]) {
      expect(() => originServiceFor(bad)).toThrow(HttpError);
    }
  });
});

describe('getRemoteTunnelIngress', () => {
  it('GETs the configurations endpoint and returns the ingress array', async () => {
    const calls = mockCfFetch((method, path) => {
      expect(method).toBe('GET');
      expect(path).toBe(`/accounts/acct/cfd_tunnel/${TUNNEL_ID}/configurations`);
      return ok({ config: { ingress: [{ hostname: 'a.example.com', service: 'http://127.0.0.1:1' }] } });
    });
    const ingress = await getRemoteTunnelIngress(CFG);
    expect(ingress).toHaveLength(1);
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toContain('/cfd_tunnel/');
  });
  it('502s when the response has no ingress array', async () => {
    mockCfFetch(() => ok({ config: {} }));
    await expect(getRemoteTunnelIngress(CFG)).rejects.toMatchObject({ status: 502 });
  });
});

describe('setRemoteTunnelIngress', () => {
  // In-memory remote state the mock serves.
  function remoteState(initial: Array<{ hostname?: string; service: string }>) {
    let ingress = initial.map((r) => ({ ...r }));
    return {
      handler: (method: string, _path: string, body?: any) => {
        if (method === 'GET') return ok({ config: { ingress } });
        ingress = body.config.ingress.map((r: any) => ({ ...r }));
        return ok({ config: { ingress } });
      },
      get: () => ingress,
    };
  }

  it('upserts managed hostnames, preserves operator rules, drops stale managed rules, keeps catch-all last', async () => {
    const remote = remoteState([
      { hostname: 'keep.example.com', service: 'http://127.0.0.1:9000' }, // operator's
      { hostname: 'old.example.com', service: 'http://127.0.0.1:9001' }, // ours, going away
      { hostname: 'api.example.com', service: 'http://127.0.0.1:1111' }, // ours, port changes
      { service: 'http_status:404' },
    ]);
    const calls = mockCfFetch(remote.handler);

    const verified = await setRemoteTunnelIngress(
      CFG,
      [
        { hostname: 'api.example.com', port: 18001 },
        { hostname: 'new.example.com', port: 18002 },
      ],
      new Set(['api.example.com', 'old.example.com', 'new.example.com']),
    );

    const put = calls.find((c) => c.method === 'PUT');
    expect(put).toBeDefined();
    const sent = (put!.body as any).config.ingress as Array<{ hostname?: string; service: string }>;
    // operator rule preserved verbatim
    expect(sent.find((r) => r.hostname === 'keep.example.com')).toMatchObject({
      service: 'http://127.0.0.1:9000',
    });
    // managed rule updated to the new origin port
    expect(sent.find((r) => r.hostname === 'api.example.com')).toMatchObject({
      service: 'http://127.0.0.1:18001',
    });
    // new managed rule added
    expect(sent.find((r) => r.hostname === 'new.example.com')).toMatchObject({
      service: 'http://127.0.0.1:18002',
    });
    // stale managed rule dropped
    expect(sent.find((r) => r.hostname === 'old.example.com')).toBeUndefined();
    // catch-all last
    expect(sent[sent.length - 1]).toMatchObject({ service: 'http_status:404' });

    // The verifying re-read saw the same table.
    expect(verified.find((r) => r.hostname === 'new.example.com')).toBeDefined();
  });

  it('refuses an invalid hostname instead of writing it to the remote config', async () => {
    const remote = remoteState([{ service: 'http_status:404' }]);
    mockCfFetch(remote.handler);
    await expect(
      setRemoteTunnelIngress(CFG, [{ hostname: '*.example.com', port: 1 }]),
    ).rejects.toMatchObject({ status: 400 });
    expect(remote.get()).toHaveLength(1); // untouched
  });

  it('502s when the verifying re-read does not show a desired rule', async () => {
    // A remote that silently drops the PUT (stale read after write).
    let firstGet = true;
    mockCfFetch((method) => {
      if (method === 'GET' && firstGet) {
        firstGet = false;
        return ok({ config: { ingress: [{ service: 'http_status:404' }] } });
      }
      if (method === 'PUT') return ok({ config: { ingress: [{ service: 'http_status:404' }] } });
      return ok({ config: { ingress: [{ service: 'http_status:404' }] } });
    });
    await expect(
      setRemoteTunnelIngress(CFG, [{ hostname: 'api.example.com', port: 18001 }]),
    ).rejects.toMatchObject({ status: 502 });
  });
});

describe('deploymentHostPort', () => {
  function db(allocs: Record<string, number[]>, ports: Record<string, Record<string, number>>) {
    return {
      query: async (sql: string, params?: unknown[]) => {
        if (sql.includes('port_allocations')) {
          const list = (allocs[params![0] as string] ?? []).sort((a, b) => a - b);
          return { rows: list.length > 0 ? [{ port: list[0] }] : [] };
        }
        const p = ports[params![0] as string] ?? {};
        return { rows: [{ ports: p }] };
      },
    };
  }

  it('prefers the port registry, lowest first', async () => {
    const d = db({ dep1: [19000, 18001] }, { dep1: { '17000': 80 } });
    expect(await deploymentHostPort(d as any, 'dep1')).toBe(18001);
  });
  it('falls back to deployments.ports (lowest host port)', async () => {
    const d = db({}, { dep1: { '19000': 80, '18001': 3000 } });
    expect(await deploymentHostPort(d as any, 'dep1')).toBe(18001);
  });
  it('returns null when nothing is published (caller must refuse, not guess)', async () => {
    const d = db({}, { dep1: {} });
    expect(await deploymentHostPort(d as any, 'dep1')).toBeNull();
    const d2 = db({}, {});
    expect(await deploymentHostPort(d2 as any, 'nope')).toBeNull();
  });
});

describe('collectDesiredRoutes', () => {
  function db(domains: Array<{ hostname: string; deployment_id: string; status: string; ingress: string }>) {
    return {
      query: async (sql: string, params?: unknown[]) => {
        if (sql.includes('DISTINCT lower(hostname)')) {
          return {
            rows: domains
              .filter((d) => d.ingress === 'tunnel' && d.status !== 'removed')
              .map((d) => ({ hostname: d.hostname.toLowerCase() })),
          };
        }
        if (sql.includes('port_allocations')) return { rows: [{ port: 18001 }] };
        if (sql.includes('FROM domains d')) {
          return {
            rows: domains
              .filter((d) => d.status === 'active' && d.ingress === 'tunnel')
              .map((d) => ({ hostname: d.hostname, deployment_id: d.deployment_id })),
          };
        }
        return { rows: [{ ports: {} }] };
      },
    };
  }

  it('collects active tunnel domains and the managed set', async () => {
    const d = db([
      { hostname: 'a.example.com', deployment_id: 'd1', status: 'active', ingress: 'tunnel' },
      { hostname: 'b.example.com', deployment_id: 'd2', status: 'failed', ingress: 'tunnel' },
      { hostname: 'c.example.com', deployment_id: 'd3', status: 'active', ingress: 'direct' },
    ]);
    const { desired, managed } = await collectDesiredRoutes(d as any);
    expect(desired).toEqual([{ hostname: 'a.example.com', port: 18001 }]);
    expect(managed).toEqual(new Set(['a.example.com', 'b.example.com']));
  });

  it('includes an in-flight (configuring) domain via extra, but does NOT grant it ownership of remote rules', async () => {
    // W16 hardening: a first-time claim must not silently take over a
    // remote rule the operator manages outside this system — so `extra`
    // lands in `desired` but stays OUT of `managed`. setRemoteTunnelIngress
    // 409s when a desired-but-unmanaged hostname already has a remote rule.
    const d = db([]);
    const { desired, managed } = await collectDesiredRoutes(d as any, {
      hostname: 'new.example.com',
      port: 19000,
    });
    expect(desired).toEqual([{ hostname: 'new.example.com', port: 19000 }]);
    expect(managed.has('new.example.com')).toBe(false);
  });
});
