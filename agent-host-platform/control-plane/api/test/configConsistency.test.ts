import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';
import { describe, expect, it } from 'vitest';

// Repo-wide configuration-name consistency (W3). The canonical
// provisioning-token name is UAHT_PROVISIONING_TOKEN — one name, one
// concept, used for both agent registration (X-Provisioning-Token header)
// and host bootstrap registration (Authorization: Bearer). A bare
// PROVISIONING_TOKEN anywhere in tracked source is a bug: before W3 the
// auth middleware read the bare name while everything else read the
// UAHT_-prefixed name, so setting one name silently left the other gate
// open/closed.

const REPO_ROOT = resolve(__dirname, '..', '..', '..', '..');
const SKIP_DIRS = new Set(['node_modules', 'dist', '__pycache__', '.pytest_cache', '.git']);
const SCAN_EXTENSIONS = new Set([
  '.ts',
  '.py',
  '.sh',
  '.md',
  '.example',
  '.yml',
  '.yaml',
  '.service',
  '.toml',
]);

// Docs history may mention the old name when describing the rename itself,
// as may this test file (it defines the detection pattern) and the
// canonical CONFIG.md reference.
const EXCLUDED_FILES = new Set([
  'agent-host-platform/docs/implementation-status.md',
  'agent-host-platform/docs/CONFIG.md',
  'agent-host-platform/control-plane/api/test/configConsistency.test.ts',
]);

function walk(dir: string, out: string[]): void {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (SKIP_DIRS.has(entry)) continue;
    const st = statSync(full);
    if (st.isDirectory()) {
      walk(full, out);
    } else if ([...SCAN_EXTENSIONS].some((ext) => entry.endsWith(ext))) {
      out.push(full);
    }
  }
}

describe('config naming consistency (W3)', () => {
  it('no bare PROVISIONING_TOKEN (without the UAHT_ prefix) in tracked source', () => {
    const files: string[] = [];
    walk(REPO_ROOT, files);
    expect(files.length).toBeGreaterThan(50); // sanity: the scan actually ran

    const offenders: string[] = [];
    const bareRe = /(?<!UAHT_)PROVISIONING_TOKEN/g;
    for (const file of files) {
      const rel = relative(REPO_ROOT, file);
      if (EXCLUDED_FILES.has(rel)) continue;
      const text = readFileSync(file, 'utf8');
      const hits = text.match(bareRe);
      if (hits) offenders.push(`${rel} (${hits.length}x)`);
    }
    expect(offenders).toEqual([]);
  });

  it('the canonical UAHT_PROVISIONING_TOKEN is actually used (scan is not vacuous)', () => {
    const files: string[] = [];
    walk(REPO_ROOT, files);
    const users = files.filter((f) =>
      readFileSync(f, 'utf8').includes('UAHT_PROVISIONING_TOKEN'),
    );
    expect(users.length).toBeGreaterThan(5);
  });

  it('the worker runtime never reads the installer input name UAHT_TUNNEL_TOKEN as config', () => {
    // UAHT_TUNNEL_TOKEN is the operator-facing installer input; the worker
    // runtime name is WORKER_TUNNEL_TOKEN (mapped by install-host.sh).
    // Comments, docstrings, and error messages may *mention* the installer
    // name — what must never happen is the worker *reading* it as config.
    const sourceRoots = [
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'agent'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'ingress'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'executor'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'deployments'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'docker'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'health'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'logs'),
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', 'updater'),
    ];
    const usageRes = [
      /os\.environ(?:\.get)?\(\s*['"]UAHT_TUNNEL_TOKEN['"]/,
      /['"]UAHT_TUNNEL_TOKEN['"]\s+in\s+os\.environ/,
      /['"]uaht_tunnel_token['"]/, // legacy worker.env file key
    ];
    const offenders: string[] = [];
    for (const root of sourceRoots) {
      const found: string[] = [];
      try {
        walk(root, found);
      } catch {
        continue; // a package dir may not exist in slim checkouts
      }
      for (const f of found.filter((x) => x.endsWith('.py'))) {
        const text = readFileSync(f, 'utf8');
        for (const re of usageRes) {
          if (re.test(text)) offenders.push(`${relative(REPO_ROOT, f)} matches ${re}`);
        }
      }
    }
    // worker.env example: the runtime file must not carry the UAHT_ name.
    const example = readFileSync(
      join(REPO_ROOT, 'agent-host-platform', 'host-worker', '.env.example'),
      'utf8',
    );
    expect(/^UAHT_TUNNEL_TOKEN=/m.test(example)).toBe(false);
    expect(offenders).toEqual([]);
  });

  it('install-host.sh never writes UAHT_TUNNEL_TOKEN as a worker.env key', () => {
    const script = readFileSync(
      join(REPO_ROOT, 'agent-host-platform', 'scripts', 'install-host.sh'),
      'utf8',
    );
    // The installer may read $UAHT_TUNNEL_TOKEN (input) — including a
    // shell-local default assignment under `set -u` — and mention it in
    // comments. What must never happen is the *worker.env heredoc* carrying
    // a UAHT_-prefixed key: the runtime key there must be WORKER_TUNNEL_TOKEN.
    const heredoc = script.match(/cat > "\$ENV_FILE" <<EOF\n([\s\S]*?)\nEOF/);
    expect(heredoc, 'worker.env heredoc found in install-host.sh').not.toBeNull();
    const keyWrites = heredoc![1].split('\n').filter((line) => /^UAHT_TUNNEL_TOKEN=/.test(line));
    expect(keyWrites).toEqual([]);
    expect(heredoc![1]).toContain('WORKER_TUNNEL_TOKEN=$UAHT_TUNNEL_TOKEN');
  });
});

describe('config documentation completeness (W-C)', () => {
  it('every env var the API reads via process.env is documented in .env.example and CONFIG.md', () => {
    const example = readFileSync(
      join(REPO_ROOT, 'agent-host-platform', 'control-plane', 'api', '.env.example'),
      'utf8',
    );
    const configMd = readFileSync(
      join(REPO_ROOT, 'agent-host-platform', 'docs', 'CONFIG.md'),
      'utf8',
    );
    // Canonical names with exactly one meaning (see docs/CONFIG.md); the
    // ones below were previously missing from one or both documents.
    for (const name of [
      'CLOUDFLARE_TUNNEL_API_TOKEN',
      'CLOUDFLARE_ACCOUNT_ID',
      'CLOUDFLARE_API_BASE',
      'RATE_LIMIT_UNAUTH_PER_MIN',
      'UAHT_ROTATE_RATE_PER_MIN',
    ]) {
      expect(example, `${name} documented in .env.example`).toContain(name);
      expect(configMd, `${name} documented in CONFIG.md`).toContain(name);
    }
  });
});
