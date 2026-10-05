"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.domainsRouter = void 0;
const express_1 = require("express");
const pool_1 = require("../db/pool");
const errors_1 = require("../lib/errors");
const events_1 = require("../lib/events");
const log_1 = require("../lib/log");
const auth_1 = require("../middleware/auth");
const cloudflare_1 = require("../lib/cloudflare");
const _helpers_1 = require("./_helpers");
exports.domainsRouter = (0, express_1.Router)();
async function getDeployment(pool, id) {
    const { rows } = await pool.query('SELECT id, project_id, host_id, domains FROM deployments WHERE id = $1', [id]);
    return rows[0];
}
// POST /v1/domains {deployment_id, hostname, ingress?}
// Records the domain on the deployment; when Cloudflare is configured,
// creates a proxied CNAME hostname -> ingress target (tunnel hostname in
// tunnel mode, PUBLIC_INGRESS_HOSTNAME in direct mode, the default).
// `ingress` accepts 'tunnel' | 'direct'; tunnel mode without
// TUNNEL_INGRESS_HOSTNAME configured is a 422. Tunnel-mode changes enqueue
// an `ingress-sync` task for the host so the worker rebuilds its tunnel
// routes.
exports.domainsRouter.post('/', auth_1.requireAgent, (0, auth_1.requirePermission)('manage_domains'), async (req, res, next) => {
    try {
        const { deployment_id, hostname } = req.body ?? {};
        if (!(0, _helpers_1.isUuid)(deployment_id)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'deployment_id must be a UUID');
            return;
        }
        if (!(0, cloudflare_1.isValidHostname)(hostname)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'hostname is not a valid DNS name');
            return;
        }
        let mode;
        try {
            mode = (0, cloudflare_1.resolveIngressMode)(req.body?.ingress);
        }
        catch (err) {
            next(err);
            return;
        }
        const pool = (0, pool_1.getPool)();
        const dep = await getDeployment(pool, deployment_id);
        if (!dep) {
            next(new errors_1.HttpError(404, 'not_found', 'deployment not found'));
            return;
        }
        const domains = Array.isArray(dep.domains) ? dep.domains : [];
        if (domains.some((d) => d.hostname.toLowerCase() === hostname.toLowerCase())) {
            (0, errors_1.sendError)(res, 409, 'conflict', 'hostname already attached to this deployment');
            return;
        }
        const entry = {
            hostname,
            status: 'dns_pending',
            added_at: new Date().toISOString(),
            ingress: mode,
        };
        const cfg = (0, cloudflare_1.getCloudflareConfig)();
        if (mode === 'tunnel') {
            // 422 when the operator asked for tunnel mode without configuring it.
            // Reads the env directly: tunnel routing doesn't need the DNS token.
            entry.tunnel_host = (0, cloudflare_1.requireTunnelHostname)();
        }
        if (cfg) {
            try {
                entry.cf_record_id = await (0, cloudflare_1.ensureCnameRecord)(cfg, hostname, (0, cloudflare_1.cnameTargetFor)(cfg, mode));
                entry.status = 'active';
            }
            catch (err) {
                entry.status = 'error';
                entry.error = err instanceof Error ? err.message : String(err);
                log_1.logger.error('domain dns failed', { hostname, err: entry.error });
            }
        }
        domains.push(entry);
        await pool.query('UPDATE deployments SET domains = $1::jsonb WHERE id = $2', [
            JSON.stringify(domains),
            deployment_id,
        ]);
        let ingressTaskId = null;
        if (mode === 'tunnel') {
            ingressTaskId = await enqueueIngressSync(pool, dep.host_id, req.auth.id, { trigger: 'domain.added', hostname });
        }
        await (0, events_1.appendEvent)(pool, {
            type: cfg ? 'domain.added' : 'domain.requested',
            actor_type: 'agent',
            actor_id: req.auth.name,
            deployment_id,
            host_id: dep.host_id,
            payload: {
                hostname, status: entry.status, dns_managed: cfg !== null,
                ingress: mode, tunnel_host: entry.tunnel_host ?? null,
                ingress_task_id: ingressTaskId,
            },
        });
        res.status(201).json({ domain: entry });
    }
    catch (err) {
        next(err);
    }
});
/**
 * Queue an `ingress-sync` task for a host so its worker rebuilds the
 * tunnel route table (used when tunnel-mode domains change). Returns the
 * task id, or null when the insert fails (logged; the domain record is the
 * source of truth and a later manual ingress-sync heals it).
 */
async function enqueueIngressSync(pool, hostId, createdBy, trigger) {
    try {
        const { rows } = await pool.query(`INSERT INTO tasks (created_by, assigned_to, type, status, payload)
       VALUES ($1, $2, 'ingress-sync', 'queued', $3) RETURNING id`, [createdBy, hostId, JSON.stringify(trigger)]);
        log_1.logger.info('ingress-sync task queued', { task: rows[0].id, host: hostId, trigger });
        return rows[0].id;
    }
    catch (err) {
        log_1.logger.error('failed to queue ingress-sync task', { host: hostId, err });
        return null;
    }
}
// GET /v1/domains?deployment_id= — list domains for a deployment.
exports.domainsRouter.get('/', auth_1.requireAgent, (0, auth_1.requirePermission)('manage_domains'), async (req, res, next) => {
    try {
        const deploymentId = req.query.deployment_id;
        if (typeof deploymentId !== 'string' || !(0, _helpers_1.isUuid)(deploymentId)) {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'deployment_id query param (UUID) is required');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const dep = await getDeployment(pool, deploymentId);
        if (!dep) {
            next(new errors_1.HttpError(404, 'not_found', 'deployment not found'));
            return;
        }
        res.json({ domains: Array.isArray(dep.domains) ? dep.domains : [] });
    }
    catch (err) {
        next(err);
    }
});
// DELETE /v1/domains {deployment_id, hostname} — detach + remove DNS record.
exports.domainsRouter.delete('/', auth_1.requireAgent, (0, auth_1.requirePermission)('manage_domains'), async (req, res, next) => {
    try {
        const { deployment_id, hostname } = req.body ?? {};
        if (!(0, _helpers_1.isUuid)(deployment_id) || typeof hostname !== 'string') {
            (0, errors_1.sendError)(res, 400, 'bad_request', 'deployment_id (UUID) and hostname are required');
            return;
        }
        const pool = (0, pool_1.getPool)();
        const dep = await getDeployment(pool, deployment_id);
        if (!dep) {
            next(new errors_1.HttpError(404, 'not_found', 'deployment not found'));
            return;
        }
        const domains = Array.isArray(dep.domains) ? dep.domains : [];
        const idx = domains.findIndex((d) => d.hostname.toLowerCase() === hostname.toLowerCase());
        if (idx === -1) {
            next(new errors_1.HttpError(404, 'not_found', 'hostname not attached to this deployment'));
            return;
        }
        const [removed] = domains.splice(idx, 1);
        const cfg = (0, cloudflare_1.getCloudflareConfig)();
        if (cfg && removed.cf_record_id) {
            await (0, cloudflare_1.deleteDnsRecord)(cfg, removed.cf_record_id);
        }
        await pool.query('UPDATE deployments SET domains = $1::jsonb WHERE id = $2', [
            JSON.stringify(domains),
            deployment_id,
        ]);
        let ingressTaskId = null;
        if ((removed.ingress ?? 'direct') === 'tunnel') {
            ingressTaskId = await enqueueIngressSync(pool, dep.host_id, req.auth.id, { trigger: 'domain.removed', hostname: removed.hostname });
        }
        await (0, events_1.appendEvent)(pool, {
            type: 'domain.removed',
            actor_type: 'agent',
            actor_id: req.auth.name,
            deployment_id,
            host_id: dep.host_id,
            payload: { hostname: removed.hostname, ingress_task_id: ingressTaskId },
        });
        res.json({ removed: removed.hostname });
    }
    catch (err) {
        next(err);
    }
});
