import { describe, expect, it } from 'vitest';
import { readSweeperConfig, staleHostTransition } from '../src/lib/hostSweeper';

const NOW = new Date('2026-10-05T12:00:00Z');
const secsAgo = (s: number) => new Date(NOW.getTime() - s * 1000);

describe('staleHostTransition', () => {
  it('keeps a fresh online host as-is', () => {
    expect(staleHostTransition('online', secsAgo(10), NOW, 90, 300)).toBeNull();
  });
  it('marks online -> degraded past the degraded threshold', () => {
    expect(staleHostTransition('online', secsAgo(91), NOW, 90, 300)).toBe('degraded');
  });
  it('marks online -> offline past the offline threshold (not degraded)', () => {
    expect(staleHostTransition('online', secsAgo(301), NOW, 90, 300)).toBe('offline');
  });
  it('marks degraded -> offline past the offline threshold', () => {
    expect(staleHostTransition('degraded', secsAgo(400), NOW, 90, 300)).toBe('offline');
  });
  it('does not re-emit for an already-offline host', () => {
    expect(staleHostTransition('offline', secsAgo(9999), NOW, 90, 300)).toBeNull();
  });
  it('does not re-emit for an already-degraded host still within offline window', () => {
    expect(staleHostTransition('degraded', secsAgo(120), NOW, 90, 300)).toBeNull();
  });
  it('never touches a draining host, however stale', () => {
    expect(staleHostTransition('draining', secsAgo(9999), NOW, 90, 300)).toBeNull();
    expect(staleHostTransition('draining', secsAgo(10), NOW, 90, 300)).toBeNull();
  });
  it('uses custom thresholds', () => {
    expect(staleHostTransition('online', secsAgo(61), NOW, 60, 120)).toBe('degraded');
    expect(staleHostTransition('online', secsAgo(59), NOW, 60, 120)).toBeNull();
    expect(staleHostTransition('online', secsAgo(121), NOW, 60, 120)).toBe('offline');
  });
  it('boundary: exactly at the threshold counts as stale', () => {
    expect(staleHostTransition('online', secsAgo(90), NOW, 90, 300)).toBe('degraded');
    expect(staleHostTransition('online', secsAgo(300), NOW, 90, 300)).toBe('offline');
  });
});

describe('readSweeperConfig', () => {
  it('defaults to 30s interval, 90s degraded, 300s offline', () => {
    expect(readSweeperConfig({})).toEqual({ intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 });
  });
  it('reads env overrides', () => {
    expect(
      readSweeperConfig({
        HEARTBEAT_SWEEP_INTERVAL_S: '10',
        HEARTBEAT_DEGRADED_AFTER_S: '45',
        HEARTBEAT_OFFLINE_AFTER_S: '120',
      }),
    ).toEqual({ intervalS: 10, degradedAfterS: 45, offlineAfterS: 120 });
  });
  it('ignores invalid env values', () => {
    expect(
      readSweeperConfig({
        HEARTBEAT_DEGRADED_AFTER_S: 'not-a-number',
        HEARTBEAT_OFFLINE_AFTER_S: '-5',
      }),
    ).toEqual({ intervalS: 30, degradedAfterS: 90, offlineAfterS: 300 });
  });
});
