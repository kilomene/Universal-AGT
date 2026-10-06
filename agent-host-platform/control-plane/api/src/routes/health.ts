import { Router } from 'express';
import { API_VERSION, MIN_WORKER_VERSION } from '../lib/versions';

export const healthRouter = Router();

// GET /v1/health — no auth required. The version is the single source of
// truth from lib/versions (== package.json "version", enforced by
// scripts/check-build-consistency.sh); min_worker_version lets operators
// and workers see the compatibility floor without authenticating.
healthRouter.get('/', (_req, res) => {
  res.json({
    ok: true,
    version: API_VERSION,
    min_worker_version: MIN_WORKER_VERSION,
    time: new Date().toISOString(),
  });
});
