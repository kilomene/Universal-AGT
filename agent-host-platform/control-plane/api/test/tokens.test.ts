import { createHash } from 'crypto';
import { describe, expect, it } from 'vitest';
import { generateAgentKey, generateHostToken, sha256Hex, verifyToken } from '../src/lib/tokens';

describe('token issuance', () => {
  it('generateAgentKey returns a token and its sha256 hex hash', () => {
    const { token, hash } = generateAgentKey();
    expect(typeof token).toBe('string');
    expect(token.length).toBeGreaterThan(32);
    expect(hash).toBe(createHash('sha256').update(token, 'utf8').digest('hex'));
    expect(hash).toMatch(/^[0-9a-f]{64}$/);
  });
  it('generateHostToken returns a token and its sha256 hex hash', () => {
    const { token, hash } = generateHostToken();
    expect(hash).toBe(sha256Hex(token));
  });
  it('every issuance is unique', () => {
    const seen = new Set<string>();
    for (let i = 0; i < 100; i++) {
      const { token } = generateAgentKey();
      expect(seen.has(token)).toBe(false);
      seen.add(token);
    }
  });
  it('agent and host tokens are distinguishable by prefix', () => {
    expect(generateAgentKey().token.startsWith('uag_')).toBe(true);
    expect(generateHostToken().token.startsWith('uagh_')).toBe(true);
  });
});

describe('verifyToken (constant-time compare against stored hash)', () => {
  it('accepts the exact issued token', () => {
    const { token, hash } = generateAgentKey();
    expect(verifyToken(token, hash)).toBe(true);
  });
  it('rejects a wrong token', () => {
    const { hash } = generateAgentKey();
    expect(verifyToken('uag_wrongtoken', hash)).toBe(false);
  });
  it('rejects a token with one character changed', () => {
    const { token, hash } = generateAgentKey();
    const tampered = token.slice(0, -1) + (token.endsWith('a') ? 'b' : 'a');
    expect(verifyToken(tampered, hash)).toBe(false);
  });
  it('rejects an empty token', () => {
    const { hash } = generateHostToken();
    expect(verifyToken('', hash)).toBe(false);
  });
  it('rejects a malformed stored hash without throwing', () => {
    const { token } = generateAgentKey();
    expect(verifyToken(token, 'not-a-hash')).toBe(false);
  });
});
