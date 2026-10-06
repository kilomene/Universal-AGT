/**
 * agent.deploy.json manifest validation — TypeScript twin of
 * host-worker/deployments/manifest.py `validate_manifest` (PROTOCOL §4).
 *
 * There is exactly ONE manifest grammar. This module is the control-plane
 * side of it; the worker is the other side. Both must accept/reject the
 * same shapes, especially `resources` (Fix #1: the scheduler reserves from
 * the artifact's validated manifest, so the grammar used at artifact-init
 * time must be the grammar the worker enforces at deploy time).
 *
 * Memory grammar (single, everywhere): /^\d{1,6}[mMgG][iI]?$/
 *   "256m" | "512Mi" | "1g" | "2Gi"  ->  megabytes via normalizeResources
 *   (lib/scheduler.ts) / normalize_resources (worker).
 */

export const MANIFEST_RUNTIMES = ['docker', 'docker-compose', 'static'] as const;
export const MANIFEST_RESTART_POLICIES = ['no', 'always', 'unless-stopped', 'on-failure'] as const;
/** Single memory grammar shared by validation and normalization. */
export const MANIFEST_MEMORY_RE = /^\d{1,6}[mMgG][iI]?$/;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isScalar(v: unknown): boolean {
  return (
    typeof v === 'string' ||
    typeof v === 'number' ||
    typeof v === 'boolean'
  );
}

/**
 * Strict validation — mirrors the worker's validate_manifest exactly.
 * Used for artifact manifests (POST /v1/artifacts/init `manifest`): the
 * stored manifest is the exact agent.deploy.json the worker will deploy,
 * so it must already satisfy everything the worker enforces.
 *
 * Returns the list of errors (empty = valid).
 */
export function validateManifest(manifest: unknown, projectName?: string): string[] {
  const errors: string[] = [];
  if (!isRecord(manifest)) {
    return ['manifest must be a JSON object'];
  }

  // -- name ------------------------------------------------------------
  const name = manifest.name;
  if (typeof name !== 'string' || !name.trim()) {
    errors.push('name: required non-empty string');
  } else if (projectName !== undefined && name !== projectName) {
    errors.push(`name: ${JSON.stringify(name)} does not match project name ${JSON.stringify(projectName)}`);
  }

  // -- runtime ---------------------------------------------------------
  if (!(manifest.runtime !== undefined && (MANIFEST_RUNTIMES as readonly string[]).includes(manifest.runtime as string))) {
    errors.push(`runtime: must be one of ${JSON.stringify(MANIFEST_RUNTIMES)}, got ${JSON.stringify(manifest.runtime)}`);
  }

  // -- build -----------------------------------------------------------
  const build = manifest.build;
  if (build !== undefined) {
    if (!isRecord(build)) {
      errors.push('build: must be an object');
    } else {
      const dockerfile = build.dockerfile;
      if (dockerfile !== undefined && (typeof dockerfile !== 'string' || !dockerfile.trim())) {
        errors.push('build.dockerfile: must be a non-empty string');
      }
      const context = build.context;
      if (context !== undefined && (typeof context !== 'string' || !context.trim())) {
        errors.push('build.context: must be a non-empty string');
      }
    }
  }

  // -- service ---------------------------------------------------------
  const service = manifest.service;
  if (service !== undefined) {
    if (!isRecord(service)) {
      errors.push('service: must be an object');
    } else {
      const port = service.port;
      if (port !== undefined) {
        if (!Number.isInteger(port) || (port as number) < 1 || (port as number) > 65535) {
          errors.push(`service.port: must be an integer 1..65535, got ${JSON.stringify(port)}`);
        }
      }
      const healthcheck = service.healthcheck;
      if (healthcheck !== undefined && (typeof healthcheck !== 'string' || !healthcheck.startsWith('/'))) {
        errors.push(`service.healthcheck: must be a string starting with '/', got ${JSON.stringify(healthcheck)}`);
      }
    }
  }

  // -- resources -------------------------------------------------------
  const resources = manifest.resources;
  if (resources !== undefined) {
    if (!isRecord(resources)) {
      errors.push('resources: must be an object');
    } else {
      const memory = resources.memory;
      if (memory !== undefined && (typeof memory !== 'string' || !MANIFEST_MEMORY_RE.test(memory))) {
        errors.push(
          `resources.memory: must look like '256m', '512Mi' or '1g', got ${JSON.stringify(memory)}`,
        );
      }
      const cpu = resources.cpu;
      if (cpu !== undefined) {
        if (typeof cpu !== 'boolean' && typeof cpu === 'number') {
          if (!(cpu > 0) || !Number.isFinite(cpu)) {
            errors.push(`resources.cpu: must be a positive finite number, got ${JSON.stringify(cpu)}`);
          }
        } else {
          errors.push(`resources.cpu: must be a positive number, got ${JSON.stringify(cpu)}`);
        }
      }
    }
  }

  // -- restart ---------------------------------------------------------
  const restart = manifest.restart;
  if (
    restart !== undefined &&
    !(typeof restart === 'string' && (MANIFEST_RESTART_POLICIES as readonly string[]).includes(restart))
  ) {
    errors.push(`restart: must be one of ${JSON.stringify(MANIFEST_RESTART_POLICIES)}, got ${JSON.stringify(restart)}`);
  }

  // -- env -------------------------------------------------------------
  const env = manifest.env;
  if (env !== undefined) {
    if (!isRecord(env)) {
      errors.push('env: must be an object of string keys to scalar values');
    } else {
      for (const [key, value] of Object.entries(env)) {
        if (!key) {
          errors.push(`env: invalid key ${JSON.stringify(key)}`);
        } else if (value === null || value === undefined || !isScalar(value)) {
          errors.push(`env.${key}: value must be a string/number/boolean, got ${JSON.stringify(value)}`);
        }
      }
    }
  }

  // -- volumes ---------------------------------------------------------
  // No partial volume support (spec §6): manifest-level `volumes` would be
  // persisted but never applied by the docker runtime. Reject outright.
  if (manifest.volumes !== undefined && manifest.volumes !== null) {
    errors.push(
      'volumes: not supported in agent.deploy.json; the docker runtime never mounts host paths. ' +
        'Use runtime=docker-compose with named volumes or workspace-relative bind mounts instead',
    );
  }

  return errors;
}
