// Regression tests for the 2026-10-08 fixes:
// 1. Dashboard static path must resolve to agent-host-platform/dashboard
// 2. GET /v1/events/stream must honor the ?since= query parameter
import { existsSync, readFileSync } from 'fs';
import { join, resolve } from 'path';
import { describe, expect, it } from 'vitest';

// --- 1. Dashboard path -------------------------------------------------------

// Mirrors the repoRoot() logic in src/index.ts:
// dist layout: <repo>/agent-host-platform/control-plane/api/dist
// Test dir:   <repo>/agent-host-platform/control-plane/api/test
// Both are 4 levels below <repo>.
function repoRootFromTest(): string {
  return resolve(__dirname, '..', '..', '..', '..');
}

describe('dashboard static path (regression 2026-10-08)', () => {
  it('resolves to agent-host-platform/dashboard which exists and has index.html', () => {
    const dashboardDir = join(repoRootFromTest(), 'agent-host-platform', 'dashboard');
    expect(existsSync(dashboardDir)).toBe(true);
    expect(existsSync(join(dashboardDir, 'index.html'))).toBe(true);
  });

  it('does NOT resolve to the old wrong path <repo>/dashboard', () => {
    const wrongDir = join(repoRootFromTest(), 'dashboard');
    const rightDir = join(repoRootFromTest(), 'agent-host-platform', 'dashboard');
    // The old code used join(repoRoot(), 'dashboard') — that folder must
    // not exist, proving the fix points at the real location.
    expect(existsSync(wrongDir)).toBe(false);
    expect(existsSync(rightDir)).toBe(true);
  });
});

// --- 2. SSE stream ?since= filter --------------------------------------------

describe('GET /v1/events/stream ?since= (regression 2026-10-08)', () => {
  const eventsSrc = readFileSync(
    join(__dirname, '..', 'src', 'routes', 'events.ts'),
    'utf8',
  );

  it('stream handler reads the since query param', () => {
    // The /stream route must reference req.query.since (it previously
    // ignored the query string entirely and replayed everything).
    const streamSection = eventsSrc.split("eventsRouter.get('/stream'")[1];
    expect(streamSection).toBeDefined();
    expect(streamSection).toMatch(/since/);
  });

  it('stream replay applies a created_at >= filter when since is given', () => {
    // Guards against a future rewrite dropping the filter again.
    // Mirrors the GET /v1/events contract: created_at >= $N.
    const streamSection = eventsSrc.split("eventsRouter.get('/stream'")[1];
    expect(streamSection).toMatch(/created_at >= /);
  });

  it('stream rejects a malformed since with 400 bad_request', () => {
    const streamSection = eventsSrc.split("eventsRouter.get('/stream'")[1];
    expect(streamSection).toMatch(/bad_request/);
    expect(streamSection).toMatch(/since must be an ISO-8601 timestamp/);
  });
});
