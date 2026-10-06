// Component version contract (§61).
//
// API_VERSION is the single source of truth for the control-plane release
// version. It MUST match the "version" field in package.json —
// scripts/check-build-consistency.sh (and CI) fail on drift, and
// GET /v1/health serves it.
//
// MIN_WORKER_VERSION is the oldest host-worker release this control plane
// will interoperate with. Enforcement:
//   * POST /v1/hosts/register rejects a *parseable* worker_version older
//     than this with 426 + code "worker_outdated".
//   * Heartbeats from such workers get worker_outdated: true in the
//     response (plus api_version / min_worker_version) and emit one
//     host.worker_outdated event per process.
// A missing or unparseable worker_version is accepted (legacy and custom
// builds stay online and stay visible in the hosts table) — it is never
// silently treated as a supported release, and a *known* older release is
// never silently interoperated with.

export const API_VERSION = '1.0.0';
export const MIN_WORKER_VERSION = '0.1.0';

// Manifest schema version for agent.deploy.json (PROTOCOL §4). The worker
// ignores unknown top-level keys, so a manifest may carry a manifestVersion
// newer than the control plane knows; the control plane only rejects a
// manifestVersion whose MAJOR version is newer than the one it supports.
export const DEPLOYMENT_MANIFEST_VERSION = '1.0.0';

const NUMERIC_PART = /^\d+$/;

function parseVersion(v: string): number[] | null {
  const s = v.trim().replace(/^v/i, '');
  if (!s) return null;
  const core = s.split('-')[0]!.split('+')[0]!; // strip prerelease/build metadata
  const parts = core.split('.');
  if (parts.length === 0 || parts.length > 4) return null;
  const nums: number[] = [];
  for (const p of parts) {
    if (!NUMERIC_PART.test(p)) return null;
    nums.push(parseInt(p, 10));
  }
  return nums;
}

/**
 * -1 | 0 | 1 — compares dot-separated numeric versions; missing parts count
 * as 0 ("1.0" equals "1.0.0"). Throws on an unparseable input.
 */
export function compareVersions(a: string, b: string): -1 | 0 | 1 {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) throw new Error(`unparseable version: ${!pa ? a : b}`);
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x < y) return -1;
    if (x > y) return 1;
  }
  return 0;
}

/**
 * True when the control plane may interoperate with a worker reporting
 * version `v`. Missing or unparseable versions are tolerated (legacy and
 * custom builds stay online and visible in the hosts table); only a *known*
 * older release is refused — never silently.
 */
export function isSupportedWorkerVersion(v: unknown): boolean {
  if (typeof v !== 'string') return true;
  if (parseVersion(v) === null) return true; // unparseable: visible, not judged
  return compareVersions(v, MIN_WORKER_VERSION) >= 0;
}

/**
 * True when `v` parses as a version strictly older than MIN_WORKER_VERSION
 * — i.e. a worker the control plane has positively identified as outdated.
 */
export function isOutdatedWorkerVersion(v: unknown): boolean {
  if (typeof v !== 'string') return false;
  if (parseVersion(v) === null) return false;
  return compareVersions(v, MIN_WORKER_VERSION) < 0;
}
