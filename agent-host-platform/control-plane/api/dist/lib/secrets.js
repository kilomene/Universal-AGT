"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.getEncryptionKey = getEncryptionKey;
exports.encryptSecret = encryptSecret;
exports.decryptSecret = decryptSecret;
const crypto_1 = require("crypto");
// AES-256-GCM secret encryption for the secrets table.
// Wire format: nonce(12) || ciphertext || authTag(16).
// The key comes from DATA_ENCRYPTION_KEY (64 hex chars = 32 bytes).
const NONCE_LEN = 12;
const TAG_LEN = 16;
function getEncryptionKey() {
    const hex = process.env.DATA_ENCRYPTION_KEY;
    if (!hex) {
        throw new Error('DATA_ENCRYPTION_KEY is not set');
    }
    if (!/^[0-9a-fA-F]{64}$/.test(hex)) {
        throw new Error('DATA_ENCRYPTION_KEY must be 64 hex chars (32 bytes)');
    }
    return Buffer.from(hex, 'hex');
}
function encryptSecret(plaintext, key) {
    const k = key ?? getEncryptionKey();
    const nonce = (0, crypto_1.randomBytes)(NONCE_LEN);
    const cipher = (0, crypto_1.createCipheriv)('aes-256-gcm', k, nonce);
    const ciphertext = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
    const tag = cipher.getAuthTag();
    return Buffer.concat([nonce, ciphertext, tag]);
}
function decryptSecret(blob, key) {
    const k = key ?? getEncryptionKey();
    if (blob.length < NONCE_LEN + TAG_LEN) {
        throw new Error('encrypted blob too short');
    }
    const nonce = blob.subarray(0, NONCE_LEN);
    const tag = blob.subarray(blob.length - TAG_LEN);
    const ciphertext = blob.subarray(NONCE_LEN, blob.length - TAG_LEN);
    const decipher = (0, crypto_1.createDecipheriv)('aes-256-gcm', k, nonce);
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
}
