import { createCipheriv, createDecipheriv, randomBytes } from 'crypto';

// AES-256-GCM secret encryption for the secrets table.
// Wire format: nonce(12) || ciphertext || authTag(16).
// The key comes from DATA_ENCRYPTION_KEY (64 hex chars = 32 bytes).

const NONCE_LEN = 12;
const TAG_LEN = 16;

export function getEncryptionKey(): Buffer {
  const hex = process.env.DATA_ENCRYPTION_KEY;
  if (!hex) {
    throw new Error('DATA_ENCRYPTION_KEY is not set');
  }
  if (!/^[0-9a-fA-F]{64}$/.test(hex)) {
    throw new Error('DATA_ENCRYPTION_KEY must be 64 hex chars (32 bytes)');
  }
  return Buffer.from(hex, 'hex');
}

export function encryptSecret(plaintext: string, key?: Buffer): Buffer {
  const k = key ?? getEncryptionKey();
  const nonce = randomBytes(NONCE_LEN);
  const cipher = createCipheriv('aes-256-gcm', k, nonce);
  const ciphertext = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([nonce, ciphertext, tag]);
}

export function decryptSecret(blob: Buffer, key?: Buffer): string {
  const k = key ?? getEncryptionKey();
  if (blob.length < NONCE_LEN + TAG_LEN) {
    throw new Error('encrypted blob too short');
  }
  const nonce = blob.subarray(0, NONCE_LEN);
  const tag = blob.subarray(blob.length - TAG_LEN);
  const ciphertext = blob.subarray(NONCE_LEN, blob.length - TAG_LEN);
  const decipher = createDecipheriv('aes-256-gcm', k, nonce);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
}
