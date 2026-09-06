/**
 * Pure helper functions extracted from resultBundleParser.ts.
 *
 * These functions have no closure over module-private mutable state; they depend
 * only on their arguments and the module constants declared in this file.
 *
 * Groups:
 * 1. JSON normalization: parseJsonObject, normalizeNonFiniteJsonLiterals, isJsonLiteralBoundary
 * 2. Small pure predicates: isLikelyLigandCompId, isLikelyLigandAtomRow, isHydrogenLikeElement,
 *    isPlainRecord, hasStorageValue, hasNonEmptyResiduePlddtByChain
 * 3. Chain-hint selection: selectByChainHints<T> (unified from three near-identical functions),
 *    plus chainIdMatches, normalizeChainToken, collectLigandCoverageChainIds
 */

// ---------------------------------------------------------------------------
// Shared constants
// ---------------------------------------------------------------------------

export const WATER_COMP_IDS = new Set(['HOH', 'WAT', 'DOD', 'SOL']);

export const POLYMER_COMP_IDS = new Set([
  'ACE',
  'NME',
  'NMA',
  'NH2',
  'ALA',
  'ARG',
  'ASN',
  'ASP',
  'CYS',
  'GLN',
  'GLU',
  'GLY',
  'HIS',
  'ILE',
  'LEU',
  'LYS',
  'MET',
  'PHE',
  'PRO',
  'SER',
  'THR',
  'TRP',
  'TYR',
  'VAL',
  'SEC',
  'PYL',
  'ASX',
  'GLX',
  'UNK',
  'A',
  'C',
  'G',
  'U',
  'I',
  'DA',
  'DC',
  'DG',
  'DT',
  'DI',
  'DU'
]);

// ---------------------------------------------------------------------------
// JSON normalization helpers
// ---------------------------------------------------------------------------

export function parseJsonObject(text: string | null | undefined): Record<string, unknown> | null {
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    const normalized = normalizeNonFiniteJsonLiterals(text);
    if (normalized === text) return null;
    try {
      const parsed = JSON.parse(normalized) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
      return parsed as Record<string, unknown>;
    } catch (exc) {
      // Surface corrupt backend JSON instead of silently treating it as "no data".
      console.warn('[resultBundleParser] JSON parse failed after non-finite normalization; treating as absent.', exc);
      return null;
    }
  }
}

function isJsonLiteralBoundary(value: string | undefined): boolean {
  return !value || /\s|[,:{}\[\]]/.test(value);
}

export function normalizeNonFiniteJsonLiterals(text: string): string {
  let out = '';
  let inString = false;
  let escaping = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      out += char;
      if (escaping) {
        escaping = false;
      } else if (char === '\\') {
        escaping = true;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }
    if (char === '"') {
      inString = true;
      out += char;
      continue;
    }
    const previous = index > 0 ? text[index - 1] : undefined;
    if (
      text.startsWith('-Infinity', index) &&
      isJsonLiteralBoundary(previous) &&
      isJsonLiteralBoundary(text[index + '-Infinity'.length])
    ) {
      out += 'null';
      index += '-Infinity'.length - 1;
      continue;
    }
    if (
      text.startsWith('Infinity', index) &&
      isJsonLiteralBoundary(previous) &&
      isJsonLiteralBoundary(text[index + 'Infinity'.length])
    ) {
      out += 'null';
      index += 'Infinity'.length - 1;
      continue;
    }
    if (
      text.startsWith('NaN', index) &&
      isJsonLiteralBoundary(previous) &&
      isJsonLiteralBoundary(text[index + 'NaN'.length])
    ) {
      out += 'null';
      index += 'NaN'.length - 1;
      continue;
    }
    out += char;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Small pure predicates
// ---------------------------------------------------------------------------

export function isLikelyLigandCompId(compId: string): boolean {
  const normalized = compId.trim().toUpperCase();
  if (!normalized) return false;
  if (WATER_COMP_IDS.has(normalized)) return false;
  return !POLYMER_COMP_IDS.has(normalized);
}

export function isLikelyLigandAtomRow(groupPdb: string, compId: string): boolean {
  const normalizedGroup = groupPdb.trim().toUpperCase();
  if (!isLikelyLigandCompId(compId)) return false;
  if (!normalizedGroup) return true;
  // Some runtimes may emit ligand atoms as ATOM instead of HETATM.
  if (normalizedGroup === 'HETATM') return true;
  if (normalizedGroup === 'ATOM') return true;
  return false;
}

export function isHydrogenLikeElement(raw: string): boolean {
  const value = raw.trim().toUpperCase();
  if (!value) return false;
  const head = value.replace(/[^A-Z]/g, '').slice(0, 1);
  return head === 'H' || head === 'D' || head === 'T';
}

export function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

export function hasStorageValue(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (typeof value === 'string') return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value as Record<string, unknown>).length > 0;
  return true;
}

export function hasNonEmptyResiduePlddtByChain(value: Record<string, number[]>): boolean {
  return Object.values(value).some((series) => Array.isArray(series) && series.length > 0);
}

// ---------------------------------------------------------------------------
// Chain-hint selection (unified generic)
// ---------------------------------------------------------------------------

export function normalizeChainToken(value: string): string {
  return value.trim().toUpperCase();
}

export function chainIdMatches(candidate: string, preferred: string): boolean {
  const normalizedCandidate = normalizeChainToken(candidate);
  const normalizedPreferred = normalizeChainToken(preferred);
  if (!normalizedCandidate || !normalizedPreferred) return false;
  if (normalizedCandidate === normalizedPreferred) return true;

  const compactCandidate = normalizedCandidate.replace(/[^A-Z0-9]/g, '');
  const compactPreferred = normalizedPreferred.replace(/[^A-Z0-9]/g, '');
  if (!compactCandidate || !compactPreferred) return false;
  if (compactCandidate === compactPreferred) return true;
  if (compactCandidate.startsWith(compactPreferred) || compactCandidate.endsWith(compactPreferred)) return true;
  if (compactPreferred.startsWith(compactCandidate) || compactPreferred.endsWith(compactCandidate)) return true;
  return false;
}

export function collectLigandCoverageChainIds(confidence: Record<string, unknown>): Set<string> {
  const ids = new Set<string>();
  const add = (value: unknown) => {
    if (typeof value !== 'string') return;
    const normalized = normalizeChainToken(value);
    if (normalized) ids.add(normalized);
  };

  const ligandCoverage = confidence.ligand_atom_coverage;
  if (Array.isArray(ligandCoverage)) {
    for (const row of ligandCoverage) {
      if (!row || typeof row !== 'object') continue;
      add((row as Record<string, unknown>).chain);
    }
  }

  const chainCoverage = confidence.chain_atom_coverage;
  if (Array.isArray(chainCoverage)) {
    for (const row of chainCoverage) {
      if (!row || typeof row !== 'object') continue;
      const entry = row as Record<string, unknown>;
      const molType = String(entry.mol_type || '').trim().toLowerCase();
      if (!molType) continue;
      if (molType.includes('nonpolymer') || molType.includes('ligand')) {
        add(entry.chain);
      }
    }
  }
  return ids;
}

/**
 * Generic chain-hint selection. Unified from three near-identical functions:
 *   selectLigandAtomPlddtsByChain        (value type: number[])
 *   selectLigandAtomPlddtsByChainAndName (value type: Record<string, number>)
 *   selectLigandAtomNameKeysByChain      (value type: string[])
 *
 * All three had byte-for-byte identical control flow: short-circuit on <=1 entry,
 * try coverage hints, then try preferred-ligand-chain hints, then fall back to
 * the input. The only difference was the value type, which is captured by T.
 */
export function selectByChainHints<T>(
  confidence: Record<string, unknown>,
  byChain: Record<string, T>
): Record<string, T> {
  const entries = Object.entries(byChain);
  if (entries.length <= 1) return byChain;

  const selectByHints = (hints: Set<string>): Record<string, T> | null => {
    if (hints.size === 0) return null;
    const filtered = Object.fromEntries(
      entries.filter(([chainId]) =>
        Array.from(hints).some((hint) => chainIdMatches(chainId, hint) || chainIdMatches(hint, chainId))
      )
    ) as Record<string, T>;
    return Object.keys(filtered).length > 0 ? filtered : null;
  };

  const coverageSelected = selectByHints(collectLigandCoverageChainIds(confidence));
  if (coverageSelected) return coverageSelected;

  const preferredHints = new Set<string>();
  for (const value of [
    confidence.requested_ligand_chain_id,
    confidence.ligand_chain_id,
    confidence.model_ligand_chain_id
  ]) {
    if (typeof value !== 'string') continue;
    const normalized = normalizeChainToken(value);
    if (normalized) preferredHints.add(normalized);
  }
  const preferredSelected = selectByHints(preferredHints);
  if (preferredSelected) return preferredSelected;

  return byChain;
}

/** Split one CIF loop row into tokens, honoring single/double-quoted fields.
 *  Identical to the local copies that used to live in cifConfidenceColoring and
 *  resultBundleParser. */
export function tokenizeCifRow(row: string): string[] {
  const matcher = /'(?:[^']*)'|"(?:[^"]*)"|[^\s]+/g;
  const tokens: string[] = [];
  let match: RegExpExecArray | null = matcher.exec(row);
  while (match) {
    tokens.push(match[0]);
    match = matcher.exec(row);
  }
  return tokens;
}
