import { afterEach, describe, expect, it, vi } from 'vitest';
import { HttpError } from '../src/lib/errors';
import {
  deleteDnsRecord,
  ensureCnameRecord,
  findDnsRecord,
  getCloudflareConfig,
  isValidHostname,
  type CfConfig,
} from '../src/lib/cloudflare';

const CFG: CfConfig = {
  token: 'test-token',
  zoneId: 'zone123',
  ingressHostname: 'ingress.example.net',
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
  it('returns null unless all three vars are set', () => {
    expect(getCloudflareConfig()).toBeNull();
    process.env.CLOUDFLARE_API_TOKEN = 't';
    expect(getCloudflareConfig()).toBeNull();
    process.env.CLOUDFLARE_ZONE_ID = 'z';
    expect(getCloudflareConfig()).toBeNull();
    process.env.PUBLIC_INGRESS_HOSTNAME = 'i.example.net';
    expect(getCloudflareConfig()).toEqual({ token: 't', zoneId: 'z', ingressHostname: 'i.example.net' });
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
