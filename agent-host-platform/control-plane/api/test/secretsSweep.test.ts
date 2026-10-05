// W11: local secret-scan test.
//
// Runs the SAME scan logic as the CI `secrets-sweep` job
// (scripts/secrets-sweep.sh) against fixture trees and the repo tree, so
// the sweep is exercised even when GitHub Actions cannot acquire runners.
//
// The fixtures below are built dynamically (string concatenation): the
// test source itself must never contain a literal secret-shaped string,
// or the sweep would flag this file.
import { execFileSync } from 'child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir as osTmpdir } from 'os';
import { join, resolve } from 'path';
import { afterAll, describe, expect, it } from 'vitest';

const SCRIPT = resolve(__dirname, '../../../../scripts/secrets-sweep.sh');
const REPO_ROOT = resolve(__dirname, '../../../..');

function runSweep(root: string): { code: number; out: string } {
  try {
    const out = execFileSync('bash', [SCRIPT, root], {
      encoding: 'utf8',
      timeout: 120000,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return { code: 0, out: String(out) };
  } catch (err: any) {
    return {
      code: typeof err.status === 'number' ? err.status : 1,
      out: String(err.stdout ?? '') + String(err.stderr ?? ''),
    };
  }
}

// Secret-shaped fixtures, assembled at runtime so no literal appears here.
const shapes = {
  awsKey: 'AKIA' + 'A'.repeat(16),
  githubToken: 'ghp_' + 'a'.repeat(36),
  githubPat: 'github_pat_' + 'a'.repeat(22),
  slackToken: 'xoxb-' + '1'.repeat(12) + '-abcdef',
  stripeLive: 'sk-live-' + 'a'.repeat(24),
  openaiProj: 'sk-proj-' + 'a'.repeat(32),
  privateKey: '-----BEGIN ' + 'RSA PRIVATE KEY-----',
  assignApiKey: 'api_key = "' + 'r3al-looking-secret-value' + '"',
  assignPassword: 'password = "' + 's3cret-hunter2-value' + '"',
  awsSecretRef: 'aws_secret' + '_access_key',
};

const dirs: string[] = [];
function fixtureDir(files: Record<string, string>): string {
  const dir = mkdtempSync(join(osTmpdir(), 'uaht-sweep-'));
  dirs.push(dir);
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(dir, name), content);
  }
  return dir;
}

afterAll(() => {
  for (const d of dirs) rmSync(d, { recursive: true, force: true });
});

describe('secrets-sweep.sh', () => {
  it('flags committed-secret shapes (one per supported pattern)', () => {
    const dir = fixtureDir({
      'aws.txt': `key id ${shapes.awsKey}\n`,
      'gh.txt': `token=${shapes.githubToken}\n`,
      'pat.txt': `pat ${shapes.githubPat}\n`,
      'slack.txt': `hook ${shapes.slackToken}\n`,
      'stripe.txt': `stripe ${shapes.stripeLive}\n`,
      'openai.txt': `openai ${shapes.openaiProj}\n`,
      'key.pem': `${shapes.privateKey}\nAAAA\n`,
      'cfg.py': `${shapes.assignApiKey}\n`,
      'cfg2.py': `${shapes.assignPassword}\n`,
      'aws2.txt': `export ${shapes.awsSecretRef}=hunter2\n`,
    });
    const { code, out } = runSweep(dir);
    expect(code).toBe(1);
    for (const f of ['aws.txt', 'gh.txt', 'key.pem', 'cfg.py']) {
      expect(out).toContain(f);
    }
  });

  it('does not flag obvious placeholders or template markers', () => {
    const dir = fixtureDir({
      'good.py': [
        'api_key = "test-key-not-real"',
        'password = "changeme"',
        'token = "<your-token-here>"',
        'secret = "AKIAIOSFODNN7EXAMPLE"',
        'api_key = "${API_KEY}"',
        '# example config, nothing real',
      ].join('\n'),
      '.env.example': 'API_KEY=changeme\n',
    });
    const { code, out } = runSweep(dir);
    expect(out).toContain('secrets sweep clean');
    expect(code).toBe(0);
  });

  it('fails on a real .env file, passes on a template', () => {
    const bad = fixtureDir({ '.env': 'API_KEY=test-key-not-real\n' });
    expect(runSweep(bad).code).toBe(1);
    const good = fixtureDir({ '.env.example': 'API_KEY=\n' });
    expect(runSweep(good).code).toBe(0);
  });

  it('passes on the repository tree itself (guards this test file too)', () => {
    const { code, out } = runSweep(REPO_ROOT);
    expect(out).toContain('secrets sweep clean');
    expect(code).toBe(0);
  });
});
