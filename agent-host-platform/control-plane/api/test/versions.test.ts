// Unit tests for the §61 component version contract (src/lib/versions).
// Pure functions — no database, no app boot.
import { describe, expect, it } from 'vitest';
import {
  API_VERSION,
  DEPLOYMENT_MANIFEST_VERSION,
  MIN_WORKER_VERSION,
  compareVersions,
  isOutdatedWorkerVersion,
  isSupportedWorkerVersion,
} from '../src/lib/versions';

describe('compareVersions', () => {
  it('orders dotted versions numerically, not lexicographically', () => {
    expect(compareVersions('0.9.0', '0.10.0')).toBe(-1);
    expect(compareVersions('0.10.0', '0.9.0')).toBe(1);
    expect(compareVersions('1.0.0', '1.0.0')).toBe(0);
  });
  it('treats missing parts as zero', () => {
    expect(compareVersions('1.0', '1.0.0')).toBe(0);
    expect(compareVersions('1', '1.0.0')).toBe(0);
    expect(compareVersions('1.0.1', '1.0')).toBe(1);
  });
  it('accepts a leading v and ignores prerelease/build metadata', () => {
    expect(compareVersions('v1.2.3', '1.2.3')).toBe(0);
    expect(compareVersions('9.9.9-e2e', '9.9.9')).toBe(0);
    expect(compareVersions('1.0.0+build.7', '1.0.0')).toBe(0);
  });
  it('throws on unparseable input', () => {
    expect(() => compareVersions('banana', '1.0.0')).toThrow();
    expect(() => compareVersions('1.0.0', '')).toThrow();
    expect(() => compareVersions('1.0.x', '1.0.0')).toThrow();
  });
});

describe('version floor constants', () => {
  it('the minimum supported worker is not newer than the API itself', () => {
    expect(compareVersions(MIN_WORKER_VERSION, API_VERSION)).toBeLessThanOrEqual(0);
  });
  it('the manifest schema version is a dotted version', () => {
    expect(DEPLOYMENT_MANIFEST_VERSION).toMatch(/^\d+\.\d+\.\d+$/);
  });
});

describe('isSupportedWorkerVersion', () => {
  it('accepts the current worker release and anything newer', () => {
    expect(isSupportedWorkerVersion(MIN_WORKER_VERSION)).toBe(true);
    expect(isSupportedWorkerVersion('0.2.0')).toBe(true);
    expect(isSupportedWorkerVersion('1.0.0')).toBe(true);
    expect(isSupportedWorkerVersion('9.9.9-e2e')).toBe(true);
  });
  it('rejects a positively-identified older release', () => {
    expect(isSupportedWorkerVersion('0.0.9')).toBe(false);
    expect(isSupportedWorkerVersion('0.0.1')).toBe(false);
  });
  it('tolerates missing and unparseable versions (legacy/custom builds)', () => {
    expect(isSupportedWorkerVersion(null)).toBe(true);
    expect(isSupportedWorkerVersion(undefined)).toBe(true);
    expect(isSupportedWorkerVersion('nightly')).toBe(true);
    expect(isSupportedWorkerVersion(42)).toBe(true);
  });
});

describe('isOutdatedWorkerVersion', () => {
  it('is true only for parseable versions below the floor', () => {
    expect(isOutdatedWorkerVersion('0.0.9')).toBe(true);
    expect(isOutdatedWorkerVersion(MIN_WORKER_VERSION)).toBe(false);
    expect(isOutdatedWorkerVersion('2.0.0')).toBe(false);
    expect(isOutdatedWorkerVersion(null)).toBe(false);
    expect(isOutdatedWorkerVersion('nightly')).toBe(false);
  });
});
