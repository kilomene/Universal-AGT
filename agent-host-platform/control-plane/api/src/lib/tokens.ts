import { createHash, randomBytes, timingSafeEqual } from 'crypto';

// Token issuance: the plaintext token is shown once at registration and
// never stored; only its SHA-256 hex digest is persisted (agents.api_key_hash,
// hosts.token_hash). Comparison is constant-time.

export interface IssuedToken {
  token: string;
  hash: string; // sha256 hex of token
}

export function sha256Hex(token: string): string {
  return createHash('sha256').update(token, 'utf8').digest('hex');
}

export function generateAgentKey(): IssuedToken {
  const token = 'uag_' + randomBytes(32).toString('hex');
  return { token, hash: sha256Hex(token) };
}

export function generateHostToken(): IssuedToken {
  const token = 'uagh_' + randomBytes(32).toString('hex');
  return { token, hash: sha256Hex(token) };
}

// Constant-time comparison of a presented token against a stored hash.
export function verifyToken(presented: string, storedHash: string): boolean {
  const presentedHash = sha256Hex(presented);
  const a = Buffer.from(presentedHash, 'hex');
  const b = Buffer.from(storedHash, 'hex');
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}
