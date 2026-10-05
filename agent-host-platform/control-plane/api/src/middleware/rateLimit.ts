import type { NextFunction, Request, Response } from 'express';
import { sendError } from '../lib/errors';

// In-memory sliding-window rate limiter.
// 120 req/min per agent key, 600 req/min per host token (PROTOCOL §6).
// Limits are configurable via RATE_LIMIT_AGENT_PER_MIN / RATE_LIMIT_HOST_PER_MIN.
// Unauthenticated requests (health, agent registration) are not limited here.

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

export function rateLimit(req: Request, res: Response, next: NextFunction): void {
  const auth = req.auth;
  if (!auth) return next();

  const key = `${auth.kind}:${auth.id}`;
  const limit = limitFor(auth.kind);
  const now = Date.now();

  let bucket = buckets.get(key);
  if (!bucket) {
    bucket = { timestamps: [] };
    buckets.set(key, bucket);
  }
  bucket.timestamps = bucket.timestamps.filter((t) => now - t < WINDOW_MS);
  if (bucket.timestamps.length >= limit) {
    sendError(res, 429, 'rate_limited', `rate limit exceeded: ${limit} requests per minute`);
    return;
  }
  bucket.timestamps.push(now);
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
