'use strict';
// Minimal URL shortener — zero dependencies, Node's built-in http only.
//   POST /shorten  { "url": "https://example.com/..." } -> { "code": "a1b2c3" }
//   GET  /:code    -> 302 redirect to the stored URL (404 if unknown)
//   GET  /health   -> { "ok": true }
// Codes are random; the store is in-memory (a real deployment would persist it).

const http = require('http');

const PORT = Number(process.env.PORT || 3000);
const store = new Map();

const ALPHABET = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
function makeCode(length) {
  let code = '';
  for (let i = 0; i < length; i++) {
    code += ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
  }
  return code;
}

function uniqueCode() {
  for (let i = 0; i < 10; i++) {
    const code = makeCode(6);
    if (!store.has(code)) return code;
  }
  return makeCode(8); // collision storm fallback: longer code
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => {
      chunks.push(c);
      if (chunks.reduce((n, x) => n + x.length, 0) > 1024 * 1024) {
        reject(new Error('body too large'));
        req.destroy();
      }
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function send(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function isValidUrl(value) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const u = new URL(value);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    const path = url.pathname;

    if (req.method === 'GET' && path === '/health') {
      return send(res, 200, { ok: true });
    }

    if (req.method === 'POST' && path === '/shorten') {
      const raw = await readBody(req);
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return send(res, 400, { error: 'invalid JSON body' });
      }
      if (!isValidUrl(parsed && parsed.url)) {
        return send(res, 400, { error: 'body.url must be an http(s) URL' });
      }
      const code = uniqueCode();
      store.set(code, parsed.url);
      return send(res, 201, { code, url: parsed.url });
    }

    if (req.method === 'GET' && path.length > 1 && !path.includes('/', 1)) {
      const code = path.slice(1);
      if (store.has(code)) {
        res.writeHead(302, { Location: store.get(code) });
        return res.end();
      }
      return send(res, 404, { error: 'unknown code' });
    }

    return send(res, 404, { error: 'not found' });
  } catch (err) {
    return send(res, 500, { error: 'internal error' });
  }
});

server.listen(PORT, () => {
  console.log('url-shortener listening on port ' + PORT);
});
