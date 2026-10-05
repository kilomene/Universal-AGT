import { randomBytes } from 'crypto';
import { beforeAll, describe, expect, it } from 'vitest';
import { decryptSecret, encryptSecret, getEncryptionKey } from '../src/lib/secrets';

// Use a throwaway key for the test run; never touches a real .env value.
beforeAll(() => {
  process.env.DATA_ENCRYPTION_KEY = randomBytes(32).toString('hex');
});

describe('AES-256-GCM secret crypto', () => {
  it('encrypt/decrypt round-trips plaintext', () => {
    const blob = encryptSecret('super-secret-value');
    expect(decryptSecret(blob)).toBe('super-secret-value');
  });
  it('wire format is nonce(12) || ciphertext || tag(16)', () => {
    const plaintext = 'hello';
    const blob = encryptSecret(plaintext);
    expect(blob.length).toBe(12 + Buffer.byteLength(plaintext) + 16);
  });
  it('encryption is randomized (same plaintext -> different bytes)', () => {
    const a = encryptSecret('same');
    const b = encryptSecret('same');
    expect(a.equals(b)).toBe(false);
    expect(decryptSecret(a)).toBe('same');
    expect(decryptSecret(b)).toBe('same');
  });
  it('handles empty and unicode plaintext', () => {
    expect(decryptSecret(encryptSecret(''))).toBe('');
    expect(decryptSecret(encryptSecret('pässwörd-🔑-日本語'))).toBe('pässwörd-🔑-日本語');
  });
  it('decryption with the wrong key fails', () => {
    const blob = encryptSecret('do-not-read');
    const wrongKey = randomBytes(32);
    expect(() => decryptSecret(blob, wrongKey)).toThrow();
  });
  it('tampered ciphertext fails authentication', () => {
    const blob = encryptSecret('do-not-read');
    const tampered = Buffer.from(blob);
    tampered[20] ^= 0xff;
    expect(() => decryptSecret(tampered)).toThrow();
  });
  it('truncated blob is rejected', () => {
    expect(() => decryptSecret(Buffer.alloc(10))).toThrow();
  });
  it('explicit key parameter works without env', () => {
    const key = randomBytes(32);
    const blob = encryptSecret('explicit', key);
    expect(decryptSecret(blob, key)).toBe('explicit');
  });
  it('getEncryptionKey rejects a missing or malformed env value', () => {
    const saved = process.env.DATA_ENCRYPTION_KEY;
    delete process.env.DATA_ENCRYPTION_KEY;
    expect(() => getEncryptionKey()).toThrow(/not set/);
    process.env.DATA_ENCRYPTION_KEY = 'too-short';
    expect(() => getEncryptionKey()).toThrow(/64 hex/);
    process.env.DATA_ENCRYPTION_KEY = saved;
  });
});
