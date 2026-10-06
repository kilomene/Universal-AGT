// SSRF regression tests for the HTTPS reachability probe (§5 follow-up,
// src/routes/domains.ts).
//
// Attack covered: an agent-controlled hostname that resolves (or rebinds)
// to an internal address must never make the CONTROL PLANE issue an HTTPS
// request to it. The probe resolves A+AAAA itself, requires EVERY address
// to be public, and pins the validated IP at the socket layer.
//
// node:dns/promises and node:https are mocked here — no real DNS or
// network leaves the test process.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const dnsMock = vi.hoisted(() => {
  let v4: string[] = [];
  let v6: string[] = [];
  return {
    resolve4: vi.fn(async (): Promise<string[]> => v4),
    resolve6: vi.fn(async (): Promise<string[]> => v6),
    set: (a: string[], aaaa: string[]) => {
      v4 = a;
      v6 = aaaa;
    },
    reset: () => {
      v4 = [];
      v6 = [];
      dnsMock.resolve4.mockClear();
      dnsMock.resolve6.mockClear();
    },
  };
});
vi.mock('node:dns/promises', () => ({
  resolve4: dnsMock.resolve4,
  resolve6: dnsMock.resolve6,
}));

const httpsMock = vi.hoisted(() => {
  const calls: Array<{ url: string; opts: any }> = [];
  let respond: number | 'throw' = 200;
  const get = vi.fn((url: string, opts: any, cb: (res: any) => void) => {
    calls.push({ url, opts });
    const listeners: Record<string, Array<() => void>> = {};
    const req = {
      on: (ev: string, fn: () => void) => {
        (listeners[ev] ??= []).push(fn);
        return req;
      },
      destroy: vi.fn(),
    };
    if (respond === 'throw') {
      queueMicrotask(() => (listeners['error'] ?? []).forEach((fn) => fn()));
    } else {
      queueMicrotask(() => cb({ statusCode: respond, resume: () => {} }));
    }
    return req;
  });
  return {
    calls,
    get,
    setRespond: (r: number | 'throw') => {
      respond = r;
    },
    reset: () => {
      calls.length = 0;
      respond = 200;
      get.mockClear();
    },
  };
});
vi.mock('node:https', () => ({
  default: { get: httpsMock.get },
  get: httpsMock.get,
}));

import {
  isPublicIpAddress,
  probeHttps,
  resolveProbeAddresses,
} from '../src/routes/domains';

beforeEach(() => {
  dnsMock.reset();
  httpsMock.reset();
});

function pinnedIpOf(callIndex = 0): Promise<{ address: string; family: number }> {
  const { opts } = httpsMock.calls[callIndex];
  expect(typeof opts.lookup).toBe('function');
  return new Promise((resolve, reject) => {
    opts.lookup(
      'irrelevant.example.com',
      {},
      (err: unknown, address: string, family: number) =>
        err ? reject(err) : resolve({ address, family }),
    );
  });
}

// ---------------------------------------------------------------------------
// isPublicIpAddress — address classification
// ---------------------------------------------------------------------------
describe('isPublicIpAddress', () => {
  it.each([
    '8.8.8.8',
    '93.184.216.34',
    '1.1.1.1',
    '11.0.0.1',
    '126.255.255.255',
    '128.0.0.1',
    '172.15.255.255',
    '172.32.0.1',
    '169.253.255.255',
    '169.255.0.1',
    '192.167.255.255',
    '192.169.0.1',
    '223.255.255.255',
    '::ffff:8.8.8.8', // IPv4-mapped, embedded address is public
    '::ffff:808:808', // same address in hex form — still public
    '2606:4700:4700::1111',
    '2001:4860:4860::8888',
  ])('allows public address %s', (ip) => {
    expect(isPublicIpAddress(ip)).toBe(true);
  });

  it.each([
    // private IPv4
    '10.0.0.1',
    '10.255.255.255',
    '172.16.0.1',
    '172.31.255.255',
    '192.168.0.1',
    '192.168.1.1',
    '192.168.255.255',
    // loopback
    '127.0.0.1',
    '127.0.0.2',
    '127.255.255.255',
    // link-local (cloud metadata lives here)
    '169.254.0.1',
    '169.254.169.254',
    // other reserved / special-use
    '0.0.0.0',
    '100.64.0.1',
    '192.0.0.1',
    '192.0.2.1',
    '192.88.99.1',
    '198.18.0.1',
    '198.51.100.1',
    '203.0.113.1',
    '224.0.0.1',
    '240.0.0.1',
    // IPv6 special-use
    '::',
    '::1',
    'fc00::1',
    'fd12:3456::1',
    'fe80::1',
    'ff02::1',
    '2001:db8::1',
    '64:ff9b::808:808',
    '64:ff9b:1::808:808',
    '100::1',
    '2001::1',
    // IPv4-mapped IPv6 with a private embedded address
    '::ffff:127.0.0.1',
    '::ffff:10.0.0.1',
    '::ffff:172.16.0.1',
    '::ffff:192.168.1.1',
    '::ffff:169.254.169.254',
    // the same mapped addresses in hex form — classification must not
    // depend on textual representation
    '::ffff:7f00:1', // == ::ffff:127.0.0.1
    '::ffff:a00:1', // == ::ffff:10.0.0.1
    '::ffff:ac10:1', // == ::ffff:172.16.0.1
    '::ffff:c0a8:101', // == ::ffff:192.168.1.1
    '::ffff:a9fe:a9fe', // == ::ffff:169.254.169.254
    '::FFFF:10.0.0.1', // uppercase hex prefix
    '0:0:0:0:0:ffff:10.0.0.1', // full-length mapped form
    // 6to4 embeds an arbitrary IPv4 address (same SSRF class as NAT64)
    '2002:a9fe:a9fe::1', // embeds 169.254.169.254
    '2002:0a00:0001::', // embeds 10.0.0.1
    // not an IP at all: fail closed
    '',
    'not-an-ip',
    '999.1.1.1',
    '1.2.3.4.5',
    'gggg::1',
  ])('rejects non-public address %s', (ip) => {
    expect(isPublicIpAddress(ip)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// resolveProbeAddresses — DNS validation gate
// ---------------------------------------------------------------------------
describe('resolveProbeAddresses', () => {
  it('returns addresses for a public hostname', async () => {
    dnsMock.set(['93.184.216.34'], ['2606:2800:220:1:248:1893:25c8:1946']);
    await expect(resolveProbeAddresses('public.example.com')).resolves.toEqual([
      '93.184.216.34',
      '2606:2800:220:1:248:1893:25c8:1946',
    ]);
  });

  it.each([
    ['private IPv4', ['10.1.2.3'], []],
    ['loopback', ['127.0.0.1'], []],
    ['link-local (metadata)', ['169.254.169.254'], []],
    ['IPv6 unique-local', [], ['fc00::1234']],
    ['IPv6 loopback', [], ['::1']],
    ['IPv6 link-local', [], ['fe80::1']],
    ['mixed public + private', ['93.184.216.34', '192.168.1.10'], []],
    ['mixed A public + AAAA private', ['93.184.216.34'], ['fd00::1']],
  ])('refuses DNS resolving to %s', async (_label, v4, v6) => {
    dnsMock.set(v4, v6);
    await expect(
      resolveProbeAddresses('evil.example.com'),
    ).rejects.toThrow(/probe refused/);
  });

  it('refuses hostnames that do not resolve', async () => {
    dnsMock.set([], []);
    await expect(
      resolveProbeAddresses('nxdomain.example.com'),
    ).rejects.toThrow(/probe refused/);
  });

  it('refuses syntactically invalid hostnames without touching DNS', async () => {
    await expect(resolveProbeAddresses('127.0.0.1')).rejects.toThrow(
      /probe refused/,
    );
    await expect(resolveProbeAddresses('not a hostname!!')).rejects.toThrow(
      /probe refused/,
    );
    expect(dnsMock.resolve4).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// probeHttps — end-to-end through the mocked transport
// ---------------------------------------------------------------------------
describe('probeHttps (SSRF)', () => {
  it('probes a legitimate public domain normally', async () => {
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond(200);
    await expect(probeHttps('public.example.com')).resolves.toEqual({
      reachable: true,
      status: 200,
    });
    expect(httpsMock.get).toHaveBeenCalledTimes(1);
    expect(httpsMock.calls[0].url).toBe('https://public.example.com/');
  });

  it('records 3xx without following the redirect', async () => {
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond(302);
    await expect(probeHttps('public.example.com')).resolves.toEqual({
      reachable: true,
      status: 302,
    });
    // Exactly one request: node:https never follows redirects.
    expect(httpsMock.get).toHaveBeenCalledTimes(1);
  });

  it('pins the validated IP: the socket ignores the hostname entirely', async () => {
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond(200);
    await probeHttps('rebind.example.com');
    // The custom lookup answers with the validated address no matter what
    // hostname it is asked about — a DNS flip after validation cannot
    // redirect the connection.
    await expect(pinnedIpOf()).resolves.toEqual({
      address: '93.184.216.34',
      family: 4,
    });
  });

  it('pins IPv6 addresses with the right family', async () => {
    dnsMock.set([], ['2606:4700:4700::1111']);
    httpsMock.setRespond(200);
    await probeHttps('v6.example.com');
    await expect(pinnedIpOf()).resolves.toEqual({
      address: '2606:4700:4700::1111',
      family: 6,
    });
  });

  it('makes NO outbound connection when DNS resolves to a private address', async () => {
    dnsMock.set(['10.9.8.7'], []);
    await expect(probeHttps('evil.example.com')).resolves.toEqual({
      reachable: false,
      status: null,
    });
    expect(httpsMock.get).not.toHaveBeenCalled();
  });

  it('makes NO outbound connection when DNS resolves to link-local', async () => {
    dnsMock.set(['169.254.169.254'], []);
    await expect(probeHttps('metadata.example.com')).resolves.toEqual({
      reachable: false,
      status: null,
    });
    expect(httpsMock.get).not.toHaveBeenCalled();
  });

  it('reports unreachable when the connection fails', async () => {
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond('throw');
    await expect(probeHttps('down.example.com')).resolves.toEqual({
      reachable: false,
      status: null,
    });
  });
});

// ---------------------------------------------------------------------------
// DNS rebinding model (§3): the connection must use the validated/pinned
// IP even when DNS changes after validation.
// ---------------------------------------------------------------------------
describe('DNS rebinding model', () => {
  it('a DNS flip after validation cannot redirect the pinned connection', async () => {
    // Validation sees a public address; the probe opens its (mocked)
    // connection against that validation result.
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond(200);
    await probeHttps('rebind.example.com');
    expect(httpsMock.get).toHaveBeenCalledTimes(1);
    // The attacker's DNS now flips to an internal address — after our
    // validation ran. The socket-level lookup must still answer with the
    // address we validated, never the new answer.
    dnsMock.set(['169.254.169.254'], []);
    await expect(pinnedIpOf()).resolves.toEqual({
      address: '93.184.216.34',
      family: 4,
    });
  });

  it('a rebinding flip to another public IP is still ignored', async () => {
    dnsMock.set(['93.184.216.34'], []);
    httpsMock.setRespond(200);
    await probeHttps('rebind2.example.com');
    dnsMock.set(['1.1.1.1'], []);
    await expect(pinnedIpOf()).resolves.toEqual({
      address: '93.184.216.34',
      family: 4,
    });
  });
});
