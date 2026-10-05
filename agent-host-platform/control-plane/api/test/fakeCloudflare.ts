// test/fakeCloudflare.ts
// ============================================================================
// FAKE Cloudflare API — TEST INFRASTRUCTURE ONLY. NOT production code.
//
// A local stub HTTP server implementing just the Cloudflare endpoints the
// control plane calls:
//
//   DNS:   GET/POST /client/v4/zones/:zoneId/dns_records
//          DELETE /client/v4/zones/:zoneId/dns_records/:id
//   Tunnel: GET/PUT /client/v4/accounts/:accountId/cfd_tunnel/:tunnelId/configurations
//
// Point the code at it with CLOUDFLARE_API_BASE=http://127.0.0.1:<port>/client/v4
// (see lib/cloudflare.ts cloudflareApiBase()). The fake keeps all state in
// memory and exposes it for assertions. It emulates two real Cloudflare
// behaviors: deleting a missing DNS record returns error 81044 (which the
// code treats as success), and PUTting an empty ingress array is rejected
// with error 1056 (Cloudflare requires at least one rule).
// ============================================================================
import http from 'http';
import { randomUUID } from 'crypto';

export interface FakeDnsRecord {
  id: string;
  zone_id: string;
  type: string;
  name: string;
  content: string;
  ttl: number;
  proxied: boolean;
}

export interface FakeTunnelRule {
  hostname?: string;
  service: string;
  originRequest?: Record<string, unknown>;
}

export interface RecordedRequest {
  method: string;
  path: string;
  body: unknown;
}

export interface FakeCloudflare {
  url: string;
  requests: RecordedRequest[];
  dnsRecords: FakeDnsRecord[];
  tunnelIngress: FakeTunnelRule[];
  tunnelVersion: number;
  reset(): void;
  close(): Promise<void>;
}

function readBody(req: http.IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      if (chunks.length === 0) return resolve(undefined);
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

export async function startFakeCloudflare(): Promise<FakeCloudflare> {
  const requests: RecordedRequest[] = [];
  const dnsRecords: FakeDnsRecord[] = [];
  let tunnelIngress: FakeTunnelRule[] = [{ service: 'http_status:404' }];
  let tunnelVersion = 1;

  const server = http.createServer(async (req, res) => {
    const send = (status: number, payload: unknown) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(payload));
    };
    const fail = (status: number, code: number, message: string) =>
      send(status, { success: false, errors: [{ code, message }], result: null });

    try {
      const url = new URL(req.url ?? '/', 'http://fake');
      const body = await readBody(req);
      requests.push({ method: req.method ?? '', path: url.pathname, body });

      const auth = req.headers.authorization ?? '';
      if (!auth.startsWith('Bearer ') || auth.length < 10) {
        return fail(403, 9103, 'Invalid access token');
      }

      // -- DNS -----------------------------------------------------------
      let m = url.pathname.match(/^\/client\/v4\/zones\/([^/]+)\/dns_records$/);
      if (m) {
        const zoneId = m[1];
        if (req.method === 'GET') {
          const type = url.searchParams.get('type');
          const name = url.searchParams.get('name');
          const result = dnsRecords.filter(
            (r) =>
              r.zone_id === zoneId &&
              (!type || r.type === type) &&
              (!name || r.name.toLowerCase() === name.toLowerCase()),
          );
          return send(200, { success: true, result });
        }
        if (req.method === 'POST') {
          const b = (body ?? {}) as Record<string, unknown>;
          const rec: FakeDnsRecord = {
            id: `rec-${randomUUID().slice(0, 8)}`,
            zone_id: zoneId,
            type: String(b.type ?? 'CNAME'),
            name: String(b.name ?? ''),
            content: String(b.content ?? ''),
            ttl: Number(b.ttl ?? 300),
            proxied: Boolean(b.proxied ?? false),
          };
          dnsRecords.push(rec);
          return send(200, { success: true, result: rec });
        }
      }
      m = url.pathname.match(/^\/client\/v4\/zones\/([^/]+)\/dns_records\/([^/]+)$/);
      if (m && req.method === 'DELETE') {
        const idx = dnsRecords.findIndex((r) => r.zone_id === m![1] && r.id === m![2]);
        if (idx === -1) return fail(200, 81044, 'Record already deleted');
        dnsRecords.splice(idx, 1);
        return send(200, { success: true, result: { id: m[2] } });
      }

      // -- Tunnel configurations -----------------------------------------
      m = url.pathname.match(
        /^\/client\/v4\/accounts\/([^/]+)\/cfd_tunnel\/([^/]+)\/configurations$/,
      );
      if (m) {
        if (req.method === 'GET') {
          return send(200, {
            success: true,
            result: { config: { ingress: tunnelIngress }, version: tunnelVersion },
          });
        }
        if (req.method === 'PUT') {
          const ingress = (body as { config?: { ingress?: FakeTunnelRule[] } })?.config?.ingress;
          if (!Array.isArray(ingress) || ingress.length === 0) {
            // Emulates real Cloudflare: an empty ingress array is rejected.
            return fail(400, 1056, 'At least one ingress rule is required');
          }
          tunnelIngress = ingress.map((r) => ({ ...r }));
          tunnelVersion += 1;
          return send(200, {
            success: true,
            result: { config: { ingress: tunnelIngress }, version: tunnelVersion },
          });
        }
      }

      return fail(404, 7000, `fake has no route for ${req.method} ${url.pathname}`);
    } catch (err) {
      return fail(500, 7000, `fake handler error: ${(err as Error).message}`);
    }
  });

  await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;

  return {
    url: `http://127.0.0.1:${port}/client/v4`,
    requests,
    dnsRecords,
    get tunnelIngress() {
      return tunnelIngress;
    },
    get tunnelVersion() {
      return tunnelVersion;
    },
    reset() {
      requests.length = 0;
      dnsRecords.length = 0;
      tunnelIngress = [{ service: 'http_status:404' }];
      tunnelVersion = 1;
    },
    close: () => new Promise<void>((r) => server.close(() => r())),
  };
}
