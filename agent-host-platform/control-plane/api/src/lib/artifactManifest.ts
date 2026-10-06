/**
 * Extract `agent.deploy.json` from an uploaded artifact archive without
 * extracting the archive to disk (Fix #1 follow-up).
 *
 * The control plane must verify that the manifest inside the uploaded
 * tarball matches the manifest registered at `/v1/artifacts/init` — the
 * scheduler reserves from the registered manifest, so a disagreement
 * would corrupt the persistent capacity accounting in either direction
 * (under-reservation -> over-commit; over-reservation -> phantom capacity).
 *
 * Only the single manifest file's bytes are ever read into memory
 * (capped); nothing is written to disk and no archive member is executed.
 * Supported formats mirror the worker: .tar / .tar.gz / .tgz / .zip.
 */
import { stat } from 'fs/promises';
import * as tar from 'tar';
import * as yauzl from 'yauzl';

const MANIFEST_FILENAME = 'agent.deploy.json';
/** Manifests are tiny; anything larger is not a manifest. */
const MAX_MANIFEST_BYTES = 1_000_000;

function cleanRootName(name: string): string | null {
  let clean = name;
  while (clean.startsWith('./')) clean = clean.slice(2);
  // Archive-root only: subdirectories and traversal members never match,
  // mirroring the worker (which reads <extract_dir>/agent.deploy.json).
  if (clean !== MANIFEST_FILENAME) return null;
  return clean;
}

async function fromTar(archivePath: string): Promise<Buffer | null> {
  let found: Buffer | null = null;
  await tar.list({
    file: archivePath,
    onentry: (entry) => {
      if (found || entry.type !== 'File') {
        entry.resume();
        return;
      }
      if (cleanRootName(entry.path) === null) {
        entry.resume();
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      let tooBig = false;
      entry.on('data', (c: Buffer) => {
        size += c.length;
        if (size > MAX_MANIFEST_BYTES) {
          tooBig = true;
          entry.resume();
          return;
        }
        chunks.push(c);
      });
      entry.on('end', () => {
        if (!tooBig) found = Buffer.concat(chunks);
      });
      // Do not call entry.resume() here: we are consuming the stream.
    },
  });
  return found;
}

async function fromZip(archivePath: string): Promise<Buffer | null> {
  return new Promise((resolve, reject) => {
    yauzl.open(archivePath, { lazyEntries: true }, (err, zipfile) => {
      if (err || !zipfile) {
        reject(err ?? new Error('yauzl.open failed'));
        return;
      }
      let settled = false;
      const done = (v: Buffer | null) => {
        if (settled) return;
        settled = true;
        zipfile.close();
        resolve(v);
      };
      zipfile.on('error', (e) => done(null) ?? reject(e));
      zipfile.readEntry();
      zipfile.on('entry', (entry: yauzl.Entry) => {
        if (cleanRootName(entry.fileName) === null || entry.uncompressedSize > MAX_MANIFEST_BYTES) {
          zipfile.readEntry();
          return;
        }
        zipfile.openReadStream(entry, (e, stream) => {
          if (e || !stream) {
            zipfile.readEntry();
            return;
          }
          const chunks: Buffer[] = [];
          let size = 0;
          stream.on('data', (c: Buffer) => {
            size += c.length;
            if (size <= MAX_MANIFEST_BYTES) chunks.push(c);
          });
          stream.on('end', () => {
            done(size <= MAX_MANIFEST_BYTES ? Buffer.concat(chunks) : null);
          });
          stream.on('error', () => zipfile.readEntry());
        });
      });
      zipfile.on('end', () => done(null));
    });
  });
}

function isZipPath(p: string): boolean {
  return p.toLowerCase().endsWith('.zip');
}

export interface ExtractedManifest {
  /** Whether the archive contained a readable agent.deploy.json. */
  found: boolean;
  /** The parsed manifest object (when found and valid JSON). */
  manifest?: unknown;
  /** Human-readable reason when found but unusable. */
  error?: string;
}

/**
 * Read and parse the archive's agent.deploy.json. Never throws for
 * malformed archives — returns `{ found: false }` / `{ error }` so the
 * caller can decide (finalization treats an unreadable archive as a
 * verification failure, never as a silent pass).
 */
export async function extractManifestFromArchive(archivePath: string): Promise<ExtractedManifest> {
  try {
    const st = await stat(archivePath);
    if (!st.isFile() || st.size === 0) return { found: false };
  } catch {
    return { found: false };
  }
  let raw: Buffer | null = null;
  try {
    raw = isZipPath(archivePath) ? await fromZip(archivePath) : await fromTar(archivePath);
  } catch {
    // Not a readable tar/zip (or a hostile archive): no manifest found.
    // tar.list throws on non-tar input — that is a "no manifest" signal,
    // not a crash.
    return { found: false };
  }
  if (!raw) return { found: false };
  try {
    return { found: true, manifest: JSON.parse(raw.toString('utf-8')) };
  } catch {
    return { found: true, error: 'agent.deploy.json is not valid JSON' };
  }
}
