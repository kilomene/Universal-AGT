import 'dotenv/config';
import { randomUUID } from 'crypto';
import { existsSync } from 'fs';
import { join, resolve } from 'path';
import express from 'express';
import { closePool, getPool } from './db/pool';
import { runMigrations } from './db/migrate';
import { sendError } from './lib/errors';
import { startEventBus, stopEventBus } from './lib/events';
import { startHostSweeper } from './lib/hostSweeper';
import { startTaskSweeper } from './lib/taskSweeper';
import { startDomainReconciler } from './lib/domainReconciler';
import { startArtifactSweeper } from './lib/artifactSweeper';
import { logger } from './lib/log';
import { agentsRouter } from './routes/agents';
import { artifactsRouter, uploadArtifactContent } from './routes/artifacts';
import { deploymentsRouter } from './routes/deployments';
import { domainsRouter } from './routes/domains';
import { eventsRouter } from './routes/events';
import { healthRouter } from './routes/health';
import { hostsRouter } from './routes/hosts';
import { projectsRouter } from './routes/projects';
import { servicesRouter } from './routes/services';
import { tasksRouter } from './routes/tasks';
import { workerRouter } from './routes/worker';
import { apiErrorHandler, attachAuth, requireAgent, requirePermission } from './middleware/auth';
import { rateLimit } from './middleware/rateLimit';
import { validateStartupConfig } from './lib/config';

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      /** Per-request correlation id (inbound X-Request-Id, else generated). */
      req_id?: string;
    }
  }
}

function repoRoot(): string {
  // dist layout: <repo>/agent-host-platform/control-plane/api/dist
  return resolve(__dirname, '..', '..', '..', '..');
}

export function createApp(): express.Express {
  const app = express();
  app.disable('x-powered-by');

  // Request correlation id (§33 observability): honor an inbound
  // X-Request-Id when it is a safe token, otherwise generate one. The id
  // is logged with the request line and echoed back as a response header
  // so operators can trace a request end to end.
  const REQ_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  app.use((req, res, next) => {
    const inbound = req.header('x-request-id');
    const reqId = inbound && REQ_ID_RE.test(inbound) ? inbound : randomUUID();
    req.req_id = reqId;
    res.setHeader('x-request-id', reqId);
    next();
  });

  // Structured request log: one JSON line per request.
  app.use((req, res, next) => {
    const start = Date.now();
    res.on('finish', () => {
      logger.info('request', {
        req_id: req.req_id,
        method: req.method,
        path: req.path,
        status: res.statusCode,
        ms: Date.now() - start,
        agent: req.auth?.kind === 'agent' ? req.auth.name : undefined,
        host: req.auth?.kind === 'host' ? req.auth.name : undefined,
      });
    });
    next();
  });

  // Health: no auth.
  app.use('/v1/health', healthRouter);

  // Artifact content upload streams the raw body — registered before the
  // JSON parser so the request stream is never buffered by body parsing.
  app.put(
    '/v1/artifacts/:id/content',
    attachAuth,
    requireAgent,
    requirePermission('deploy'),
    rateLimit,
    uploadArtifactContent,
  );

  app.use(express.json({ limit: '256kb' }));

  // Everything else under /v1: optional auth attach -> rate limit -> routers.
  const v1 = express.Router();
  v1.use(attachAuth, rateLimit);
  v1.use('/agents', agentsRouter);
  v1.use('/tasks', tasksRouter);
  v1.use('/hosts', hostsRouter);
  v1.use('/hosts', workerRouter); // heartbeat lives here: POST /hosts/:id/heartbeat
  v1.use('/worker', workerRouter); // claim + progress: /worker/tasks/...
  v1.use('/projects', projectsRouter);
  v1.use('/artifacts', artifactsRouter);
  v1.use('/deployments', deploymentsRouter);
  v1.use('/domains', domainsRouter);
  v1.use('/services', servicesRouter);
  v1.use('/events', eventsRouter);
  app.use('/v1', v1);

  // Dashboard static files (dashboard team builds into DASHBOARD_DIR).
  const dashboardDir = process.env.DASHBOARD_DIR
    ? resolve(process.env.DASHBOARD_DIR)
    : join(repoRoot(), 'agent-host-platform', 'dashboard');
  if (existsSync(dashboardDir)) {
    app.use('/', express.static(dashboardDir));
    logger.info('serving dashboard', { dir: dashboardDir });
  } else {
    logger.warn('dashboard directory not found; serving API only', { dir: dashboardDir });
  }

  // 404s in PROTOCOL error shape.
  app.use('/v1', (_req, res) => sendError(res, 404, 'not_found', 'endpoint not found'));
  app.use((_req, res) => sendError(res, 404, 'not_found', 'not found'));

  app.use(apiErrorHandler);
  return app;
}

async function main(): Promise<void> {
  // Startup configuration (2026-10-05, W3): fail fast on missing production
  // secrets/config. Development (NODE_ENV=development) relaxes
  // UAHT_PROVISIONING_TOKEN only (bootstrap mode); production — the default
  // when NODE_ENV is unset — is strict. No insecure fallbacks in any mode.
  try {
    const { relaxed, warnings } = validateStartupConfig();
    for (const note of relaxed) {
      logger.warn(`startup check relaxed (development mode): ${note}`);
    }
    for (const note of warnings) {
      logger.warn(`startup configuration warning: ${note}`);
    }
    logger.info('startup configuration validated');
  } catch (err) {
    logger.error('startup configuration invalid', { err: String(err) });
    process.exit(1);
  }
  const pool = getPool();
  const applied = await runMigrations(pool);
  if (applied.length) logger.info('migrations applied at startup', { applied });
  await startEventBus(pool);
  const stopHostSweeper = startHostSweeper(pool);
  const stopTaskSweeper = startTaskSweeper(pool);
  const stopDomainReconciler = startDomainReconciler(pool);
  const stopArtifactSweeper = startArtifactSweeper(pool);

  const port = Number(process.env.PORT ?? 3000);
  const server = createApp().listen(port, () => {
    logger.info('control plane api listening', { port });
  });

  const shutdown = async (signal: string) => {
    logger.info('shutting down', { signal });
    server.close();
    stopHostSweeper();
    stopTaskSweeper();
    stopDomainReconciler();
    stopArtifactSweeper();
    await stopEventBus().catch(() => {});
    await closePool().catch(() => {});
    process.exit(0);
  };
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('SIGINT', () => void shutdown('SIGINT'));
}

if (require.main === module) {
  main().catch((err) => {
    logger.error('startup failed', { err: String(err) });
    process.exit(1);
  });
}
