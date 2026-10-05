import { afterEach, describe, expect, it, vi } from 'vitest';
import { HttpError } from '../src/lib/errors';
import {
  cnameTargetFor,
  deleteDnsRecord,
  ensureCnameRecord,
  findDnsRecord,
  getCloudflareConfig,
  isValidHostname,
  requireTunnelHostname,
  resolveIngressMode,
  type CfConfig,
} from '../src/lib/cloudflare';

const CFG: CfConfig = {
  token: 'test-token',
  zoneId: 'zone123',
  ingressHostname: 'ingress.example.net',
  tunnelHostname: null,
};

const TUNNEL_CFG: CfConfig = {
  token: 'test-token',
  zoneId: 'zone123',
  ingressHostname: '',
  tunnelHostname: 'abc123.cfargotunnel.com',
};

function mockFetchOnce(json: unknown, ok = true, status = 200) {
  const fn = vi.fn().mockResolvedValue({
    ok,
    status,
    json: () => Promise.resolve(json),
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.CLOUDFLARE_API_TOKEN;
  delete process.env.CLOUDFLARE_ZONE_ID;
  delete process.env.PUBLIC_INGRESS_HOSTNAME;
  delete process.env.TUNNEL_INGRESS_HOSTNAME;
});

describe('isValidHostname', () => {
  it('accepts normal hostnames', () => {
    expect(isValidHostname('api.example.com')).toBe(true);
    expect(isValidHostname('a-b.c0.example.co')).toBe(true);
  });
  it('rejects garbage', () => {
    for (const bad of ['', 'a', 'no spaces.com', '-lead.example.com', 'trail-.example.com',
      'notld', 'double..dot.com', 'x'.repeat(300)]) {
      expect(isValidHostname(bad)).toBe(false);
    }
  });
});

describe('getCloudflareConfig', () => {
  it('returns null unless token + zone + at least one ingress hostname are set', () => {
    expect(getCloudflareConfig()).toBeNull();
    process.env.CLOUDFLARE_API_TOKEN = 't';
    expect(getCloudflareConfig()).toBeNull();
    process.env.CLOUDFLARE_ZONE_ID = 'z';
    expect(getCloudflareConfig()).toBeNull(); // no ingress hostname yet
    process.env.PUBLIC_INGRESS_HOSTNAME = 'i.example.net';
    expect(getCloudflareConfig()).toEqual({
      token: 't', zoneId: 'z', ingressHostname: 'i.example.net', tunnelHostname: null,
    });
  });
  it('works with only the tunnel hostname configured (no direct ingress)', () => {
    process.env.CLOUDFLARE_API_TOKEN = 't';
    process.env.CLOUDFLARE_ZONE_ID = 'z';
    process.env.TUNNEL_INGRESS_HOSTNAME = 'abc123.cfargotunnel.com';
    expect(getCloudflareConfig()).toEqual({
      token: 't', zoneId: 'z', ingressHostname: '', tunnelHostname: 'abc123.cfargotunnel.com',
    });
  });
});

describe('resolveIngressMode (Phase 7)', () => {
  it('defaults to tunnel when TUNNEL_INGRESS_HOSTNAME is set, else direct', () => {
    expect(resolveIngressMode()).toBe('direct');
    expect(resolveIngressMode(undefined)).toBe('direct');
    expect(resolveIngressMode('')).toBe('direct');
    process.env.TUNNEL_INGRESS_HOSTNAME = 'abc123.cfargotunnel.com';
    expect(resolveIngressMode()).toBe('tunnel');
  });
  it('honors an explicit mode', () => {
    expect(resolveIngressMode('tunnel')).toBe('tunnel');
    expect(resolveIngressMode('direct')).toBe('direct');
    // explicit direct wins even when tunnel is configured
    process.env.TUNNEL_INGRESS_HOSTNAME = 'abc123.cfargotunnel.com';
    expect(resolveIngressMode('direct')).toBe('direct');
  });
  it('rejects garbage with 400', () => {
    try {
      resolveIngressMode('cname-bomb');
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(HttpError);
      expect(err).toMatchObject({ status: 400, code: 'bad_request' });
    }
  });
});

describe('requireTunnelHostname / cnameTargetFor (Phase 7)', () => {
  it('tunnel mode resolves to the tunnel hostname', () => {
    process.env.TUNNEL_INGRESS_HOSTNAME = 'abc123.cfargotunnel.com';
    expect(cnameTargetFor(TUNNEL_CFG, 'tunnel')).toBe('abc123.cfargotunnel.com');
    expect(requireTunnelHostname()).toBe('abc123.cfargotunnel.com');
    expect(cnameTargetFor(CFG, 'direct')).toBe('ingress.example.net');
  });
  it('tunnel mode without TUNNEL_INGRESS_HOSTNAME is a 422 with a clear message', () => {
    // env is unset here (afterEach clears it)
    try {
      requireTunnelHostname();
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(HttpError);
      expect(err).toMatchObject({ status: 422 });
      expect((err as Error).message).toContain('TUNNEL_INGRESS_HOSTNAME');
    }
  });
  it('direct mode without PUBLIC_INGRESS_HOSTNAME is a 422', () => {
    expect(() => cnameTargetFor(TUNNEL_CFG, 'direct')).toThrowError(
      expect.objectContaining({ status: 422 }),
    );
  });
});

describe('ensureCnameRecord', () => {
  it('reuses an identical existing record without POSTing', async () => {
    const fetchFn = mockFetchOnce({
      success: true,
      result: [{ id: 'rec1', name: 'api.example.com', type: 'CNAME' }],
    });
    const id = await ensureCnameRecord(CFG, 'api.example.com');
    expect(id).toBe('rec1');
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][1].method).toBe('GET');
  });

  it('creates a proxied CNAME when none exists, with bearer auth', async () => {
    // first call = GET find (empty), second = POST create
    const getFn = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve({ success: true, result: [] }),
    });
    const postFn = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve({
        success: true,
        result: { id: 'rec9', name: 'api.example.com', type: 'CNAME' },
      }),
    });
    const fetchMock = vi.fn().mockImplementationOnce(getFn).mockImplementationOnce(postFn);
    vi.stubGlobal('fetch', fetchMock);

    const id = await ensureCnameRecord(CFG, 'api.example.com');
    expect(id).toBe('rec9');
    const [, postInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(postInit.method).toBe('POST');
    expect(postInit.headers.Authorization).toBe('Bearer test-token');
    const body = JSON.parse(postInit.body);
    expect(body).toMatchObject({
      type: 'CNAME',
      name: 'api.example.com',
      content: 'ingress.example.net',
      proxied: true,
    });
  });

  it('creates the CNAME at the tunnel hostname in tunnel mode', async () => {
    process.env.TUNNEL_INGRESS_HOSTNAME = 'abc123.cfargotunnel.com';
    const getFn = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve({ success: true, result: [] }),
    });
    const postFn = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve({
        success: true,
        result: { id: 'recT', name: 'api.example.com', type: 'CNAME' },
      }),
    });
    const fetchMock = vi.fn().mockImplementationOnce(getFn).mockImplementationOnce(postFn);
    vi.stubGlobal('fetch', fetchMock);

    const id = await ensureCnameRecord(
      TUNNEL_CFG, 'api.example.com', cnameTargetFor(TUNNEL_CFG, 'tunnel'));
    expect(id).toBe('recT');
    const [, postInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    const body = JSON.parse(postInit.body);
    expect(body).toMatchObject({
      type: 'CNAME',
      name: 'api.example.com',
      content: 'abc123.cfargotunnel.com',
      proxied: true,
    });
  });

  it('turns a Cloudflare API error into HttpError 502', async () => {
    mockFetchOnce({ success: false, errors: [{ code: 9103, message: 'Unknown zone' }] }, true, 200);
    await expect(ensureCnameRecord(CFG, 'api.example.com')).rejects.toMatchObject({
      status: 502,
    });
    try {
      await findDnsRecord(CFG, 'api.example.com');
    } catch (err) {
      expect(err).toBeInstanceOf(HttpError);
    }
  });
});

describe('deleteDnsRecord', () => {
  it('issues DELETE against the record id', async () => {
    const fetchFn = mockFetchOnce({ success: true, result: {} });
    await deleteDnsRecord(CFG, 'rec9');
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toContain('/zones/zone123/dns_records/rec9');
    expect(init.method).toBe('DELETE');
    expect(init.headers.Authorization).toBe('Bearer test-token');
  });

  it('treats "record already gone" (81044) as success', async () => {
    mockFetchOnce({ success: false, errors: [{ code: 81044, message: 'Record already deleted' }] });
    await expect(deleteDnsRecord(CFG, 'rec-gone')).resolves.toBeUndefined();
  });
});
