"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.createApp = createApp;
require("dotenv/config");
const fs_1 = require("fs");
const path_1 = require("path");
const express_1 = __importDefault(require("express"));
const pool_1 = require("./db/pool");
const migrate_1 = require("./db/migrate");
const errors_1 = require("./lib/errors");
const events_1 = require("./lib/events");
const log_1 = require("./lib/log");
const agents_1 = require("./routes/agents");
const artifacts_1 = require("./routes/artifacts");
const deployments_1 = require("./routes/deployments");
const events_2 = require("./routes/events");
const health_1 = require("./routes/health");
const hosts_1 = require("./routes/hosts");
const projects_1 = require("./routes/projects");
const services_1 = require("./routes/services");
const tasks_1 = require("./routes/tasks");
const worker_1 = require("./routes/worker");
const auth_1 = require("./middleware/auth");
const rateLimit_1 = require("./middleware/rateLimit");
function repoRoot() {
    // dist layout: <repo>/agent-host-platform/control-plane/api/dist
    return (0, path_1.resolve)(__dirname, '..', '..', '..', '..');
}
function createApp() {
    const app = (0, express_1.default)();
    app.disable('x-powered-by');
    // Structured request log: one JSON line per request.
    app.use((req, res, next) => {
        const start = Date.now();
        res.on('finish', () => {
            log_1.logger.info('request', {
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
    app.use('/v1/health', health_1.healthRouter);
    // Artifact content upload streams the raw body — registered before the
    // JSON parser so the request stream is never buffered by body parsing.
    app.put('/v1/artifacts/:id/content', auth_1.attachAuth, auth_1.requireAgent, (0, auth_1.requirePermission)('deploy'), rateLimit_1.rateLimit, artifacts_1.uploadArtifactContent);
    app.use(express_1.default.json({ limit: '256kb' }));
    // Everything else under /v1: optional auth attach -> rate limit -> routers.
    const v1 = express_1.default.Router();
    v1.use(auth_1.attachAuth, rateLimit_1.rateLimit);
    v1.use('/agents', agents_1.agentsRouter);
    v1.use('/tasks', tasks_1.tasksRouter);
    v1.use('/hosts', hosts_1.hostsRouter);
    v1.use('/hosts', worker_1.workerRouter); // heartbeat lives here: POST /hosts/:id/heartbeat
    v1.use('/worker', worker_1.workerRouter); // claim + progress: /worker/tasks/...
    v1.use('/projects', projects_1.projectsRouter);
    v1.use('/artifacts', artifacts_1.artifactsRouter);
    v1.use('/deployments', deployments_1.deploymentsRouter);
    v1.use('/services', services_1.servicesRouter);
    v1.use('/events', events_2.eventsRouter);
    app.use('/v1', v1);
    // Dashboard static files (dashboard team builds into DASHBOARD_DIR).
    const dashboardDir = process.env.DASHBOARD_DIR
        ? (0, path_1.resolve)(process.env.DASHBOARD_DIR)
        : (0, path_1.join)(repoRoot(), 'dashboard');
    if ((0, fs_1.existsSync)(dashboardDir)) {
        app.use('/', express_1.default.static(dashboardDir));
        log_1.logger.info('serving dashboard', { dir: dashboardDir });
    }
    else {
        log_1.logger.warn('dashboard directory not found; serving API only', { dir: dashboardDir });
    }
    // 404s in PROTOCOL error shape.
    app.use('/v1', (_req, res) => (0, errors_1.sendError)(res, 404, 'not_found', 'endpoint not found'));
    app.use((_req, res) => (0, errors_1.sendError)(res, 404, 'not_found', 'not found'));
    app.use(auth_1.apiErrorHandler);
    return app;
}
async function main() {
    const pool = (0, pool_1.getPool)();
    const applied = await (0, migrate_1.runMigrations)(pool);
    if (applied.length)
        log_1.logger.info('migrations applied at startup', { applied });
    await (0, events_1.startEventBus)(pool);
    const port = Number(process.env.PORT ?? 3000);
    const server = createApp().listen(port, () => {
        log_1.logger.info('control plane api listening', { port });
    });
    const shutdown = async (signal) => {
        log_1.logger.info('shutting down', { signal });
        server.close();
        await (0, events_1.stopEventBus)().catch(() => { });
        await (0, pool_1.closePool)().catch(() => { });
        process.exit(0);
    };
    process.on('SIGTERM', () => void shutdown('SIGTERM'));
    process.on('SIGINT', () => void shutdown('SIGINT'));
}
if (require.main === module) {
    main().catch((err) => {
        log_1.logger.error('startup failed', { err: String(err) });
        process.exit(1);
    });
}
