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
 * Coerce an unknown value to an array of records, dropping non-object entries (including
 * nested arrays). Identical to the filter-variant local copies in projectTaskRuntime,
 * PeptideDesignResultsWorkspace, peptideTaskPreview and resultBundleParser.
 * (resultConfidenceStorage keeps its own plain-cast variant by design: it must preserve
 * array element identity without filtering.)
 */
export function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
}

/**
 * True when the value is a non-array object with at least one key — the shared
 * "has meaningful payload" check used by task-row merge logic. Identical to the local
 * copies in useProjectTasksDataLoader, useProjectTaskRowActions and
 * useProjectDetailRuntimeContext.
 */
export function hasObjectContent(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
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
 * Parse an array value into finite numbers, dropping non-finite entries.
 * Identical to the local copy in taskRowSync.
 */
export function readFiniteNumberArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => readFiniteNumber(item))
    .filter((item): item is number => item !== null);
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

/**
 * Coerce any non-null value to its string form WITHOUT trimming — the semantic
 * shared by the result-parsing copies in resultBundleParser,
 * PeptideDesignResultsWorkspace, leadOptPredictionHelpers,
 * useLeadOptReferenceFragment and projectLoadFlow. (recordReaders' `readText`
 * trims; the two must not be conflated.)
 */
export function asString(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

/**
 * Strict numeric coercion: numbers pass through only if finite; strings are NOT
 * parsed. Identical to the local copies in resultBundleParser, projectMetrics
 * and taskDataConfidence. (String inputs go through `readFiniteNumber` instead.)
 */
export function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}
