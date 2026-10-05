import type { NextFunction, Request, Response } from 'express';
import { sendError } from '../lib/errors';

// In-memory sliding-window rate limiter.
// 120 req/min per agent key, 600 req/min per host token (PROTOCOL §6).
// Limits are configurable via RATE_LIMIT_AGENT_PER_MIN / RATE_LIMIT_HOST_PER_MIN.
// Unauthenticated requests (health, agent registration) get their own
// bucket: RATE_LIMIT_UNAUTH_PER_MIN per IP (default 10/min, tightened
// 2026-10-05 — registration used to be unthrottled).

const WINDOW_MS = 60_000;

interface Bucket {
  timestamps: number[];
}

const buckets = new Map<string, Bucket>();

function limitFor(kind: 'agent' | 'host'): number {
  if (kind === 'host') {
    return Number(process.env.RATE_LIMIT_HOST_PER_MIN ?? 600);
  }
  return Number(process.env.RATE_LIMIT_AGENT_PER_MIN ?? 120);
}

function unauthLimit(): number {
  const n = Number(process.env.RATE_LIMIT_UNAUTH_PER_MIN ?? 10);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 10;
}

function checkBucket(key: string, limit: number): boolean {
  const now = Date.now();
  let bucket = buckets.get(key);
  if (!bucket) {
    bucket = { timestamps: [] };
    buckets.set(key, bucket);
  }
  bucket.timestamps = bucket.timestamps.filter((t) => now - t < WINDOW_MS);
  if (bucket.timestamps.length >= limit) {
    return false;
  }
  bucket.timestamps.push(now);
  return true;
}

export function rateLimit(req: Request, res: Response, next: NextFunction): void {
  const auth = req.auth;
  if (!auth) {
    // Unauthenticated bucket, keyed by client IP. Health stays cheap
    // (its route is mounted before this middleware), but open endpoints
    // like agent registration are throttled against enumeration floods.
    const key = `ip:${req.ip}`;
    const limit = unauthLimit();
    if (!checkBucket(key, limit)) {
      sendError(res, 429, 'rate_limited', `rate limit exceeded: ${limit} requests per minute`);
      return;
    }
    return next();
  }

  const key = `${auth.kind}:${auth.id}`;
  const limit = limitFor(auth.kind);
  if (!checkBucket(key, limit)) {
    sendError(res, 429, 'rate_limited', `rate limit exceeded: ${limit} requests per minute`);
    return;
  }
  next();
}

// Test hook: drop all buckets (isolates rate-limit assertions).
export function resetRateLimitBucketsForTests(): void {
  buckets.clear();
}

// Strict bucket for credential rotation endpoints (POST /v1/agents/me/rotate
// and POST /v1/hosts/:id/rotate-token). Rotation is the most sensitive
// authenticated operation: a tightened per-credential limit on top of the
// general API limit (120/min agent, 600/min host), configurable via
// UAHT_ROTATE_RATE_PER_MIN (default 10/min per credential).
function rotateLimit(): number {
  const n = Number(process.env.UAHT_ROTATE_RATE_PER_MIN ?? 10);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 10;
}

export function rotationRateLimit(req: Request, res: Response, next: NextFunction): void {
  const auth = req.auth;
  if (!auth) {
    sendError(res, 401, 'unauthorized', 'missing or invalid bearer token');
    return;
  }
  const key = `rotate:${auth.kind}:${auth.id}`;
  const limit = rotateLimit();
  if (!checkBucket(key, limit)) {
    sendError(res, 429, 'rate_limited', `rate limit exceeded: ${limit} rotation requests per minute`);
    return;
  }
  next();
}

// Keep the map from growing unboundedly when many distinct keys appear.
setInterval(() => {
  const now = Date.now();
  for (const [key, bucket] of buckets) {
    bucket.timestamps = bucket.timestamps.filter((t) => now - t < WINDOW_MS);
    if (bucket.timestamps.length === 0) buckets.delete(key);
  }
}, WINDOW_MS).unref?.();
