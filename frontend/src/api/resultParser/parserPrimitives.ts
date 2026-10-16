import { asRecordArray, asString } from '../../pages/projectTasks/recordReaders';

// Tiny structural primitives shared by the result-bundle parser and the
// peptide-candidate storage helpers. Kept dependency-free so the heavy
// parser (and jszip) can stay a lazily-loaded module.

export function getBaseName(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

export function readFirstObjectArrayPath(payload: Record<string, unknown>, paths: string[]): Array<Record<string, unknown>> {
  for (const path of paths) {
    let current: unknown = payload;
    let matched = true;
    for (const token of path.split('.')) {
      if (!current || typeof current !== 'object' || Array.isArray(current)) {
        matched = false;
        break;
      }
      current = (current as Record<string, unknown>)[token];
    }
    if (!matched) continue;
    const rows = asRecordArray(current);
    if (rows.length > 0) return rows;
  }
  return [];
}

export function readFiniteNumberLoose(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

export function firstNonEmptyTextByKeys(row: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const text = asString(row[key]).trim();
    if (text) return text;
  }
  return '';
}

export function readObjectPathLoose(payload: Record<string, unknown>, path: string): unknown {
  let current: unknown = payload;
  for (const token of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[token];
  }
  return current;
}
