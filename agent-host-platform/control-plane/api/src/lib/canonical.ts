// Canonical JSON: deterministic key-sorted serialization for
// idempotency payload comparison (order-insensitive deep equality).

export function canonicalStringify(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalStringify).join(',') + ']';
  }
  if (typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>).sort();
    return (
      '{' +
      keys
        .map((k) => JSON.stringify(k) + ':' + canonicalStringify((value as Record<string, unknown>)[k]))
        .join(',') +
      '}'
    );
  }
  return JSON.stringify(value) ?? 'null';
}

export function deepEqual(a: unknown, b: unknown): boolean {
  return canonicalStringify(a) === canonicalStringify(b);
}
