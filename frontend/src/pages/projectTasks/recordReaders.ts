/**
 * Shared small "record reader" helpers extracted from multiple result/snapshot modules.
 *
 * Each function here is byte-for-byte identical in logic to the local copies it replaces.
 * If a consuming file previously used a different variant (e.g. a `readText` without `.trim()`
 * or a `readObjectPath` without the `Array.isArray` guard), that file keeps its own local copy.
 */

/**
 * Coerce an unknown value to a record, returning `{}` for non-objects (including arrays).
 * Identical to the local copies in useResultSnapshot, peptideTaskPreview, taskDataCore, taskRowSync.
 */
export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/**
 * Read a value as a trimmed string, returning `''` for null/undefined.
 * Identical to the local copies in useResultSnapshot and peptideTaskPreview.
 */
export function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

/**
 * Traverse a dotted path on a record, returning `undefined` when any segment is not a
 * plain object (arrays are treated as non-traversable). Identical to the local copies in
 * useResultSnapshot and peptideTaskPreview.
 */
export function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

/**
 * Parse a value to a finite number, returning `null` for non-finite values.
 * String inputs are trimmed before parsing. Identical to the local copies in
 * useResultSnapshot and peptideTaskPreview.
 */
export function readFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/**
 * Scan multiple payloads for the first finite number found at any of the given dotted paths.
 * Identical in logic to `readFirstFinite` (useResultSnapshot) and `firstFiniteMetric` (peptideTaskPreview).
 */
export function readFirstFinite(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

/**
 * Scan multiple payloads for the first non-empty text found at any of the given dotted paths.
 * Identical in logic to `readFirstText` (useResultSnapshot) and `firstTextMetric` (peptideTaskPreview).
 */
export function readFirstText(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readText(readObjectPath(payload, path));
      if (value) return value;
    }
  }
  return '';
}
