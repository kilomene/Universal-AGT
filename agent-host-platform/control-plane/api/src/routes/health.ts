import { Router } from 'express';

export const healthRouter = Router();

// GET /v1/health — no auth required.
healthRouter.get('/', (_req, res) => {
  res.json({ ok: true, version: '1.0.0', time: new Date().toISOString() });
});
