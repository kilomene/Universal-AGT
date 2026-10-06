import type { Pool } from 'pg';
import { appendEvent } from './events';
import { logger } from './log';
import { reconcileTunnelRoutes } from './cloudflare-tunnel';
import {
  provisionDomain,
  verifyTunnelRoutes,
  type VerifyTunnelResult,
} from '../routes/domains';

// Domain reconciler (W13b §24/§51): periodic convergence of the domains
// table against the authoritative remote tunnel configuration.
//
// Every pass, in order:
//
//   1. reconcileTunnelRoutes — heal the REMOTE table from the DB: every
//      ACTIVE tunnel-mode domain on a routable deployment becomes an
//      ingress rule; managed-but-undesired hostnames (removed domains,
//      dead deployments) have their rules dropped; operator-managed
//      hostnames (present remotely but owned by nobody in the DB) are
//      preserved untouched.
//   2. verifyTunnelRoutes — re-read the remote table and compare it
//      against the DB: an 'active' row whose route is missing/wrong, or
//      whose route cannot be verified at all (Cloudflare API down), is
//      marked 'degraded' instead of permanently claiming 'active'.
//      'degraded' rows whose routes check out heal back to 'active'.
//   3. Retry: every 'degraded' row is reprovisioned (degraded ->
//      configuring -> active|failed) so a transient outage converges
//      without operator action.
//
// The pass never throws: a failure is logged and the next interval tries
// again. Each pass is idempotent.

export interface DomainReconcilerConfig {
  intervalS: number;
}

export function readDomainReconcilerConfig(
  env: NodeJS.ProcessEnv = process.env,
): DomainReconcilerConfig {
  const n = Number(env.DOMAIN_RECONCILE_INTERVAL_S);
  return { intervalS: Number.isFinite(n) && n > 0 ? n : 300 };
}

export interface ReconcileDomainsReport {
  tunnelReconciled: boolean;
  tunnelError?: string;
  verify: VerifyTunnelResult;
  reprovisioned: string[];
  reprovisionFailed: Array<{ hostname: string; error: string }>;
}

export async function reconcileDomainsOnce(pool: Pool): Promise<ReconcileDomainsReport> {
  const report: ReconcileDomainsReport = {
    tunnelReconciled: false,
    verify: { checked: 0, degraded: [], healed: [] },
    reprovisioned: [],
    reprovisionFailed: [],
  };

  // 1. Heal the remote table from the DB.
  const recon = await reconcileTunnelRoutes(pool);
  report.tunnelReconciled = recon.ok;
  if (!recon.ok) {
    report.tunnelError = recon.error;
    logger.warn('domain reconciler: tunnel reconcile failed', { err: recon.error });
  }

  // 2. Verify DB rows against the remote table (marks degraded/healed).
  report.verify = await verifyTunnelRoutes(pool);

  // 3. Retry degraded rows until convergence — but ONLY when this pass
  // could actually confirm remote state. If the tunnel reconcile failed
  // or the verification read could not reach Cloudflare, reprovisioning
  // now would just fail again (and flip degraded rows to failed on a
  // transient outage). The rows stay degraded and the next pass retries.
  const canRetry = report.tunnelReconciled && !report.verify.error;
  if (!canRetry) {
    logger.info('domain reconciler: skipping reprovision retry (remote state unconfirmed this pass)', {
      tunnel_reconciled: report.tunnelReconciled,
      verify_error: report.verify.error ?? null,
    });
  } else {
    const { rows } = await pool.query(
      `SELECT id, hostname FROM domains
       WHERE ingress = 'tunnel' AND status = 'degraded'`,
    );
    for (const row of rows as Array<{ id: string; hostname: string }>) {
      try {
        const after = await provisionDomain(pool, row.id);
        if (after.status === 'active') {
          report.reprovisioned.push(row.hostname);
        } else {
          report.reprovisionFailed.push({
            hostname: row.hostname,
            error: after.error ?? `still ${after.status}`,
          });
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        report.reprovisionFailed.push({ hostname: row.hostname, error: message });
        logger.warn('domain reconciler: reprovision threw', {
          hostname: row.hostname,
          err: message,
        });
      }
    }
  }

  if (
    report.verify.degraded.length > 0 ||
    report.verify.healed.length > 0 ||
    report.reprovisioned.length > 0
  ) {
    await appendEvent(pool, {
      type: 'domain.reconcile',
      actor_type: 'system',
      actor_id: 'domain-reconciler',
      payload: {
        tunnel_reconciled: report.tunnelReconciled,
        tunnel_error: report.tunnelError ?? null,
        checked: report.verify.checked,
        degraded: report.verify.degraded,
        healed: report.verify.healed,
        reprovisioned: report.reprovisioned,
        reprovision_failed: report.reprovisionFailed,
      },
    });
  }
  return report;
}

/** Start the periodic domain reconciliation pass. Returns a stop function. */
export function startDomainReconciler(
  pool: Pool,
  cfg: DomainReconcilerConfig = readDomainReconcilerConfig(),
): () => void {
  const timer = setInterval(() => {
    reconcileDomainsOnce(pool).catch((err) => {
      logger.warn('domain reconciler pass failed', { err: String(err) });
    });
  }, cfg.intervalS * 1000);
  timer.unref?.();
  logger.info('domain reconciler started', { ...cfg });
  return () => clearInterval(timer);
}
