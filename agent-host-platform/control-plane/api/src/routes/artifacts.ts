import { createHash } from 'crypto';
import { randomUUID } from 'crypto';
import { createReadStream, createWriteStream } from 'fs';
import { mkdir, rm, stat } from 'fs/promises';
import { basename, join, resolve } from 'path';
import type { NextFunction, Request, Response } from 'express';
import { Router } from 'express';
import { getPool } from '../db/pool';
import { sendError, HttpError } from '../lib/errors';
import type { ErrorCode } from '../lib/errors';
import { appendEvent } from '../lib/events';
import { logger } from '../lib/log';
import { requireAgent, requirePermission } from '../middleware/auth';
import { authorizeArtifactAccess, authorizeProjectAccess, inList, listAccessibleProjectIds } from '../lib/authz';
import { manifestContractOf, manifestContractsEqual, validateManifest } from '../lib/manifest';
import { extractManifestFromArchive } from '../lib/artifactManifest';
import { isUuid } from './_helpers';

export const artifactsRouter = Router();

const DEFAULT_ARTIFACT_MAX_BYTES = 500 * 1024 * 1024; // 500MB

// Wire size cap for artifact uploads (2026-10-05). Env-configurable via
// ARTIFACT_MAX_BYTES; falls back to the 500MB default on missing/invalid.
export function artifactMaxBytes(): number {
  const raw = Number(process.env.ARTIFACT_MAX_BYTES);
  if (Number.isFinite(raw) && raw > 0) return Math.floor(raw);
  return DEFAULT_ARTIFACT_MAX_BYTES;
}

// Host download scoping (2026-10-05): a host may download only artifacts
// referenced by tasks it claimed (payload.artifact_id). Agents keep their
// read_status access; every other host gets 403.
export async function hostMayDownloadArtifact(
  pool: ReturnType<typeof getPool>,
  hostId: string,
  artifactId: string,
): Promise<boolean> {
  const { rows } = await pool.query(
    `SELECT 1 FROM tasks
      WHERE claimed_by = $1 AND payload->>'artifact_id' = $2
      LIMIT 1`,
    [hostId, artifactId],
  );
  return rows.length > 0;
}

function repoRoot(): string {
  return resolve(__dirname, '..', '..', '..', '..');
}

export function artifactDir(): string {
  const configured = process.env.ARTIFACT_DIR;
  return configured ? resolve(configured) : join(repoRoot(), 'data', 'artifacts');
}

function isChecksumFormat(s: unknown): s is string {
  return typeof s === 'string' && /^sha256:[0-9a-f]{64}$/i.test(s);
}

// POST /v1/artifacts/init — agent with deploy permission.
artifactsRouter.post('/init', requireAgent, requirePermission('deploy'), async (req, res, next) => {
  try {
    const { project_id, filename, size, checksum, version, manifest } = req.body ?? {};
    if (!isUuid(project_id)) {
      sendError(res, 400, 'bad_request', 'project_id (UUID) is required');
      return;
    }
    if (typeof filename !== 'string' || !basename(filename) || filename.includes('\0')) {
      sendError(res, 400, 'bad_request', 'filename is required');
      return;
    }
    if (!Number.isInteger(size) || size < 0) {
      sendError(res, 400, 'bad_request', 'size must be a non-negative integer');
      return;
    }
    if (!isChecksumFormat(checksum)) {
      sendError(res, 400, 'bad_request', 'checksum must look like sha256:<64 hex chars>');
      return;
    }
    if (typeof version !== 'string' || !version) {
      sendError(res, 400, 'bad_request', 'version is required');
      return;
    }
    // Wire size cap, enforced again on the actual byte stream (2026-10-05).
    const maxBytes = artifactMaxBytes();
    if (size > maxBytes) {
      sendError(res, 413, 'payload_too_large', `declared size ${size} exceeds the artifact cap of ${maxBytes} bytes`);
      return;
    }
    const pool = getPool();
    // §10: via project -> owner/ACL (also 404s on a missing project).
    await authorizeProjectAccess(pool, req.auth!.id, project_id);
    // Fix #1: the artifact may carry its agent.deploy.json manifest. When
    // present it is validated here with the same grammar the worker
    // enforces (lib/manifest.ts, twin of the worker's validate_manifest)
    // and stored on the row; the scheduler then reserves from THIS
    // manifest — the exact definition the worker will deploy — instead of
    // the possibly-stale projects.configuration. Omitted -> NULL ->
    // scheduler falls back to project configuration (legacy behavior).
    let storedManifest: string | null = null;
    if (manifest !== undefined && manifest !== null) {
      const projName = (
        await pool.query('SELECT name FROM projects WHERE id = $1', [project_id])
      ).rows[0]?.name;
      const manifestErrors = validateManifest(manifest, typeof projName === 'string' ? projName : undefined);
      if (manifestErrors.length > 0) {
        sendError(res, 422, 'unprocessable', `artifact manifest invalid: ${manifestErrors[0]}`);
        return;
      }
      storedManifest = JSON.stringify(manifest);
    }
    const id = randomUUID();
    const safeName = basename(filename);
    const storagePath = `${id}/${safeName}`;
    try {
      const { rows } = await pool.query(
        `INSERT INTO artifacts (id, project_id, filename, storage_path, checksum, size, version, status, manifest)
         VALUES ($1,$2,$3,$4,$5,$6,$7,'pending',$8) RETURNING *`,
        [id, project_id, safeName, storagePath, checksum.toLowerCase(), size, version, storedManifest],
      );
      res.status(201).json({ artifact: rows[0], upload_url: `/v1/artifacts/${id}/content` });
    } catch (err: unknown) {
      if ((err as { code?: string }).code === '23505') {
        sendError(res, 409, 'conflict', `artifact version '${version}' already exists for this project`);
        return;
      }
      throw err;
    }
  } catch (err) {
    next(err);
  }
});

// PUT /v1/artifacts/:id/content — Content-Type: application/octet-stream.
// Streams the body to ARTIFACT_DIR/<id>/<filename> while hashing; verifies
// byte size AND sha256 against the init record. Mismatch -> 422 + failed.
export async function uploadArtifactContent(req: Request, res: Response, next: NextFunction): Promise<void> {
  try {
    const id = req.params.id;
    if (!isUuid(id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }

    // Wire size cap (2026-10-05): reject upfront on a truthful
    // Content-Length BEFORE touching the DB or disk — the old code
    // streamed the whole body before verifying size.
    const maxBytes = artifactMaxBytes();
    const declaredLen = Number(req.headers['content-length']);
    if (Number.isFinite(declaredLen) && declaredLen > maxBytes) {
      sendError(res, 413, 'payload_too_large', `content-length ${declaredLen} exceeds the artifact cap of ${maxBytes} bytes`);
      return;
    }

    const pool = getPool();
    const { rows } = await pool.query('SELECT * FROM artifacts WHERE id = $1', [id]);
    const artifact = rows[0];
    if (!artifact) {
      next(new HttpError(404, 'not_found', 'artifact not found'));
      return;
    }
    // §10: via artifact -> project -> project ACL. Without this gate any
    // agent with the deploy permission could overwrite another project's
    // artifact bytes (W3 Part 4 audit, 2026-10-06).
    await authorizeArtifactAccess(pool, req.auth!.id, id);
    if (artifact.status === 'ready') {
      sendError(res, 409, 'conflict', 'artifact content already uploaded');
      return;
    }

    // Hard stream cap for liars (Content-Length understated or chunked):
    // enforced per-chunk below while writing.

    const dir = artifactDir();
    await mkdir(join(dir, id), { recursive: true });
    const dest = join(dir, artifact.storage_path);
    // storage_path is always "<uuid>/<basename>" — never trust it blindly.
    if (!dest.startsWith(join(dir, id) + '/') && dest !== join(dir, id)) {
      sendError(res, 500, 'internal', 'invalid storage path');
      return;
    }

    const expectedSize: number = Number(artifact.size);
    const expectedHex: string = String(artifact.checksum).replace(/^sha256:/i, '').toLowerCase();
    const hash = createHash('sha256');
    let received = 0;
    let streamError: unknown = null;
    let capExceeded = false;

    await new Promise<void>((resolvePromise, rejectPromise) => {
      const out = createWriteStream(dest);
      let settled = false;
      const settle = (err?: unknown) => {
        if (settled) return;
        settled = true;
        if (err) rejectPromise(err);
        else resolvePromise();
      };
      const onData = (chunk: Buffer) => {
        received += chunk.length;
        if (received > maxBytes) {
          // Cap hit: stop writing to disk, detach the pipe, and drain the
          // rest of the body quietly so the socket stays healthy enough
          // to carry the 413 response below. The partial file is removed
          // after the promise settles.
          capExceeded = true;
          req.unpipe(out);
          out.destroy();
          req.removeListener('data', onData);
          req.on('data', () => {});
          settle();
          return;
        }
        hash.update(chunk);
      };
      req.on('data', onData);
      req.on('error', (err) => {
        out.destroy();
        settle(err);
      });
      out.on('error', (err) => settle(err));
      out.on('finish', () => settle());
      req.pipe(out);
    }).catch((err) => {
      streamError = err;
    });

    if (capExceeded) {
      // Hard stream cap: the client exceeded ARTIFACT_MAX_BYTES mid-body
      // (or lied in Content-Length). Remove the partial file, leave the row
      // 'pending' so a legitimate retry can follow.
      await rm(dest, { force: true });
      logger.warn('artifact upload exceeded size cap', { artifact: id, received, maxBytes });
      sendError(res, 413, 'payload_too_large', `upload exceeded the artifact cap of ${maxBytes} bytes`);
      return;
    }

    if (streamError) {
      // Transport failure mid-upload: remove the partial file, leave the row
      // 'pending' so the client can retry the upload.
      await rm(dest, { force: true });
      next(streamError);
      return;
    }

    const actualHex = hash.digest('hex');
    if (received !== expectedSize || actualHex !== expectedHex) {
      await rm(dest, { force: true });
      await pool.query(`UPDATE artifacts SET status = 'failed' WHERE id = $1`, [id]);
      await appendEvent(pool, {
        type: 'artifact.upload_failed',
        actor_type: 'agent',
        actor_id: req.auth!.name,
        payload: {
          artifact_id: id,
          expected_size: expectedSize,
          received_size: received,
          checksum_match: actualHex === expectedHex,
        },
      });
      logger.warn('artifact upload verification failed', { artifact: id, received, expectedSize });
      sendError(
        res,
        422,
        'unprocessable',
        `upload verification failed: received ${received} bytes (expected ${expectedSize}), sha256 ${actualHex === expectedHex ? 'ok' : 'mismatch'}`,
      );
      return;
    }

    // Fix #1 follow-up: the scheduler reserves from the manifest registered
    // at init, so the uploaded tarball's embedded agent.deploy.json must be
    // proven to describe the same contract BEFORE the artifact is marked
    // ready. Either direction of disagreement corrupts capacity accounting
    // (under-reservation over-commits the host; over-reservation strands
    // phantom capacity). A tarball with no manifest at all cannot be
    // deployed (the worker requires one), so it is rejected here — fail
    // fast at upload, not mysteriously at deploy.
    //
    // Verification runs while the row is still 'pending': on failure the
    // row moves to 'failed' (terminal, like the checksum path) and the
    // bytes are removed, so a half-verified artifact can never be picked
    // up by a concurrent deployment.
    const failArtifact = async (code: ErrorCode, message: string, eventType: string, eventPayload: Record<string, unknown>) => {
      await rm(dest, { force: true });
      await pool.query(`UPDATE artifacts SET status = 'failed' WHERE id = $1`, [id]);
      await appendEvent(pool, {
        type: eventType,
        actor_type: 'agent',
        actor_id: req.auth!.name,
        payload: { artifact_id: id, ...eventPayload },
      });
      logger.warn('artifact manifest verification failed', { artifact: id, code });
      sendError(res, 422, code, message);
    };

    const projectName = (
      await pool.query('SELECT name FROM projects WHERE id = $1', [artifact.project_id])
    ).rows[0]?.name;
    const extracted = await extractManifestFromArchive(dest);
    const registeredManifest = artifact.manifest ?? null;

    let manifestToStore: string | null = null;
    if (extracted.error) {
      await failArtifact(
        'unprocessable',
        `artifact verification failed: ${extracted.error}`,
        'artifact.manifest_invalid',
        { reason: extracted.error },
      );
      return;
    }

    if (!extracted.found) {
      // No agent.deploy.json in the archive. When a manifest was
      // registered at init, the upload disagrees with it (the registered
      // contract would describe bytes that carry no manifest) — reject.
      // When nothing was registered either, preserve the legacy path
      // (ready, manifest NULL): the worker still fails such a deploy
      // loudly at deploy time, exactly as before.
      if (registeredManifest) {
        await failArtifact(
          'unprocessable',
          'artifact verification failed: a manifest was registered at init but the uploaded archive contains no agent.deploy.json at its root',
          'artifact.manifest_missing',
          { reason: 'no agent.deploy.json in archive despite registered manifest' },
        );
        return;
      }
    } else {
      const tarballErrors = validateManifest(
        extracted.manifest,
        typeof projectName === 'string' ? projectName : undefined,
      );
      if (tarballErrors.length > 0) {
        await failArtifact(
          'unprocessable',
          `artifact verification failed: the archive's agent.deploy.json is invalid: ${tarballErrors[0]}`,
          'artifact.manifest_invalid',
          { reason: tarballErrors[0] },
        );
        return;
      }

      if (registeredManifest) {
        // Both sides validated — now prove they describe the same contract.
        if (!manifestContractsEqual(registeredManifest, extracted.manifest)) {
          const want = manifestContractOf(registeredManifest);
          const got = manifestContractOf(extracted.manifest);
          await failArtifact(
            'unprocessable',
            'artifact verification failed: the uploaded archive\u2019s agent.deploy.json disagrees with the manifest registered at init ' +
              `(registered: cpu=${String(want.cpu)} ram_mb=${String(want.ram_mb)} runtime=${want.runtime}; ` +
              `archive: cpu=${String(got.cpu)} ram_mb=${String(got.ram_mb)} runtime=${got.runtime}); ` +
              're-upload with a matching manifest',
            'artifact.manifest_mismatch',
            {
              reason: 'init manifest disagrees with archive manifest',
              registered: { cpu: want.cpu, ram_mb: want.ram_mb, runtime: want.runtime },
              archive: { cpu: got.cpu, ram_mb: got.ram_mb, runtime: got.runtime },
            },
          );
          return;
        }
      } else {
        // No manifest was registered at init: adopt the tarball's validated
        // manifest so the scheduler still reserves from the exact
        // definition the worker will deploy.
        manifestToStore = JSON.stringify(extracted.manifest);
      }
    }

    const updated = (
      await pool.query(
        `UPDATE artifacts SET status = 'ready', manifest = COALESCE($2, manifest) WHERE id = $1 RETURNING *`,
        [id, manifestToStore],
      )
    ).rows[0];
    await appendEvent(pool, {
      type: 'artifact.created',
      actor_type: 'agent',
      actor_id: req.auth!.name,
      payload: { artifact_id: id, version: artifact.version, size: received },
    });
    logger.info('artifact uploaded', { artifact: id, size: received });
    res.json({ artifact: updated });
  } catch (err) {
    next(err);
  }
}

// GET /v1/artifacts/:id
artifactsRouter.get('/:id', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    // §10: via artifact -> project -> project ACL.
    await authorizeArtifactAccess(pool, req.auth!.id, req.params.id);
    const { rows } = await pool.query('SELECT * FROM artifacts WHERE id = $1', [req.params.id]);
    res.json({ artifact: rows[0] });
  } catch (err) {
    next(err);
  }
});

// GET /v1/artifacts?project_id=
artifactsRouter.get('/', requireAgent, requirePermission('read_status'), async (req, res, next) => {
  try {
    const { project_id } = req.query;
    const pool = getPool();
    // §10: only artifacts of projects the agent may access.
    const accessibleIds = await listAccessibleProjectIds(pool, req.auth!.id);
    let rows;
    if (project_id !== undefined) {
      if (!isUuid(project_id)) {
        sendError(res, 400, 'bad_request', 'project_id must be a UUID');
        return;
      }
      await authorizeProjectAccess(pool, req.auth!.id, project_id);
      rows = (await pool.query('SELECT * FROM artifacts WHERE project_id = $1 ORDER BY created_at DESC', [project_id])).rows;
    } else {
      if (accessibleIds.length === 0) {
        res.json({ artifacts: [] });
        return;
      }
      const params: unknown[] = [];
      const cond = inList('project_id', accessibleIds, params);
      rows = (await pool.query(`SELECT * FROM artifacts WHERE ${cond} ORDER BY created_at DESC LIMIT 500`, params)).rows;
    }
    res.json({ artifacts: rows });
  } catch (err) {
    next(err);
  }
});

// GET /v1/artifacts/:id/download — agent (read_status) OR host token with
// scoping (2026-10-05): a host may only download artifacts referenced by
// tasks it claimed. Streams bytes.
artifactsRouter.get('/:id/download', async (req, res, next) => {
  try {
    if (!req.auth) {
      sendError(res, 401, 'unauthorized', 'missing or invalid bearer token');
      return;
    }
    if (req.auth.kind === 'agent' && req.auth.permissions?.read_status !== true) {
      sendError(res, 403, 'forbidden', 'missing permission: read_status');
      return;
    }
    if (!isUuid(req.params.id)) {
      sendError(res, 400, 'bad_request', 'id must be a UUID');
      return;
    }
    const pool = getPool();
    if (req.auth.kind === 'agent') {
      // §10: via artifact -> project -> project ACL.
      await authorizeArtifactAccess(pool, req.auth.id, req.params.id);
    }
    const { rows } = await pool.query('SELECT * FROM artifacts WHERE id = $1', [req.params.id]);
    const artifact = rows[0];
    if (!artifact) {
      next(new HttpError(404, 'not_found', 'artifact not found'));
      return;
    }
    if (req.auth.kind === 'host') {
      const allowed = await hostMayDownloadArtifact(pool, req.auth.id, req.params.id);
      if (!allowed) {
        sendError(res, 403, 'forbidden', 'artifact is not referenced by any task claimed by this host');
        return;
      }
    }
    if (artifact.status !== 'ready') {
      sendError(res, 409, 'conflict', `artifact is ${artifact.status}; content not available`);
      return;
    }
    const filePath = join(artifactDir(), artifact.storage_path);
    let fileStat;
    try {
      fileStat = await stat(filePath);
    } catch {
      next(new HttpError(404, 'not_found', 'artifact file missing from store'));
      return;
    }
    res.setHeader('Content-Type', 'application/octet-stream');
    res.setHeader('Content-Length', String(fileStat.size));
    // W5: filename is basename'd at init, but strip any residual quoting
    // or control characters before reflecting it into the header.
    const safeFilename = String(artifact.filename).replace(/["\\\r\n]/g, '_');
    res.setHeader('Content-Disposition', `attachment; filename="${safeFilename}"`);
    createReadStream(filePath).pipe(res);
  } catch (err) {
    next(err);
  }
});
