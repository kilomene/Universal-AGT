// Shared route helpers.

export function parseLimit(raw: unknown, def = 50, max = 500): number {
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return def;
  return Math.min(Math.floor(n), max);
}

export function isUuid(s: unknown): s is string {
  return (
    typeof s === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s)
  );
}

// Opaque keyset cursor: base64url(JSON {created_at, id}).
export function encodeCursor(createdAt: string, id: string): string {
  return Buffer.from(JSON.stringify({ created_at: createdAt, id })).toString('base64url');
}

export function decodeCursor(cursor: unknown): { created_at: string; id: string } | null {
  if (typeof cursor !== 'string' || !cursor) return null;
  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')) as {
      created_at?: string;
      id?: string;
    };
    if (typeof parsed.created_at === 'string' && isUuid(parsed.id)) {
      return { created_at: parsed.created_at, id: parsed.id as string };
    }
    return null;
  } catch {
    return null;
  }
}

// Strip secret hash columns before serializing rows.
export function publicAgent(row: Record<string, unknown>): Record<string, unknown> {
  const { api_key_hash: _drop1, ...rest } = row;
  return rest;
}

export function publicHost(row: Record<string, unknown>): Record<string, unknown> {
  const { token_hash: _drop2, ...rest } = row;
  return rest;
}
