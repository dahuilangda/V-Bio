// Persistence-side helpers for peptide candidate rows: identity, richness
// scoring, non-regressive merging and confidence compaction. Imported by the
// result-bundle parser AND directly by storage code (taskRowSync /
// resultConfidenceStorage) — deliberately free of jszip so eager modules do
// not pull the parser bundle.
import { asRecordArray, asString } from '../../pages/projectTasks/recordReaders';
import { hasStorageValue, isPlainRecord } from './resultBundleHelpers';
import {
  firstNonEmptyTextByKeys,
  readFiniteNumberLoose,
  readObjectPathLoose
} from './parserPrimitives';

export const PEPTIDE_CANDIDATE_MODIFICATION_KEYS = [
  'modifications',
  'protein_modifications',
  'residue_modifications',
  'residueMods',
  'residue_mods',
  'mods'
];

export const PEPTIDE_CANDIDATE_ARRAY_PATHS = [
  'peptide_design.best_sequences',
  'peptide_design.current_best_sequences',
  'peptide_design.candidates',
  'peptide_design.progress.best_sequences',
  'peptide_design.progress.current_best_sequences',
  'peptide_design.progress.candidates',
  'best_sequences',
  'current_best_sequences',
  'progress.best_sequences',
  'progress.current_best_sequences',
  'progress.candidates',
  'candidates'
] as const;

const PEPTIDE_CANDIDATE_ARRAY_KEYS = ['best_sequences', 'current_best_sequences', 'candidates'] as const;
const PEPTIDE_CANDIDATE_STRUCTURE_TEXT_KEYS = [
  'structure_text',
  'structureText',
  'cif_text',
  'pdb_text',
  'content'
] as const;

export function stripPeptideCandidateArrayKeys(payload: Record<string, unknown>): void {
  for (const key of PEPTIDE_CANDIDATE_ARRAY_KEYS) {
    delete payload[key];
  }
}

export function stripCandidateStructureTextFields(row: Record<string, unknown>): Record<string, unknown> {
  const next = { ...row };
  for (const key of PEPTIDE_CANDIDATE_STRUCTURE_TEXT_KEYS) {
    delete next[key];
  }
  for (const nestedKey of ['result', 'prediction', 'structure_payload', 'structure'] as const) {
    const nested = next[nestedKey];
    if (!isPlainRecord(nested)) continue;
    const compactNested = { ...nested };
    for (const key of PEPTIDE_CANDIDATE_STRUCTURE_TEXT_KEYS) {
      delete compactNested[key];
    }
    next[nestedKey] = compactNested;
  }
  return next;
}

export function peptideCandidateStorageIdentity(row: Record<string, unknown>, index: number): string {
  const sequence = firstNonEmptyTextByKeys(row, [
    'peptide_sequence',
    'binder_sequence',
    'candidate_sequence',
    'designed_sequence',
    'sequence'
  ]).replace(/\s+/g, '').trim().toUpperCase();
  const generation = readFiniteNumberLoose(row.generation) ?? readFiniteNumberLoose(row.iteration) ?? readFiniteNumberLoose(row.iter);
  const rank = readFiniteNumberLoose(row.rank) ?? readFiniteNumberLoose(row.ranking) ?? readFiniteNumberLoose(row.order);
  if (sequence) {
    return `seq:${sequence}|gen:${generation ?? ''}|rank:${rank ?? ''}`;
  }
  const structureName = firstNonEmptyTextByKeys(row, ['structure_name', 'structureName', 'structure_file', 'structure_path', 'name']);
  if (structureName) return `structure:${structureName}|rank:${rank ?? ''}`;
  const rowId = asString(row.id).trim();
  return rowId ? `id:${rowId}` : `row:${index}`;
}

export function countCandidateStorageNumbers(value: unknown): number {
  if (Array.isArray(value)) {
    return value.reduce<number>((sum, item) => sum + countCandidateStorageNumbers(item), 0);
  }
  if (value && typeof value === 'object') {
    return Object.values(value as Record<string, unknown>).reduce<number>(
      (sum, item) => sum + countCandidateStorageNumbers(item),
      0
    );
  }
  return typeof value === 'number' && Number.isFinite(value) ? 1 : 0;
}

export function peptideCandidateHasModifications(row: Record<string, unknown>): boolean {
  if (PEPTIDE_CANDIDATE_MODIFICATION_KEYS.some((key) => Array.isArray(row[key]) && (row[key] as unknown[]).length > 0)) return true;
  return ['result.modifications', 'prediction.modifications', 'metadata.modifications', 'structure_payload.modifications'].some((path) => {
    const value = readObjectPathLoose(row, path);
    return Array.isArray(value) && value.length > 0;
  });
}

export function peptideCandidateStorageRichness(row: Record<string, unknown>): number {
  let score = 0;
  if (firstNonEmptyTextByKeys(row, ['structure_name', 'structureName', 'structure_file', 'structure_path', 'name'])) score += 8;
  if (peptideCandidateHasModifications(row)) score += 10;
  if (readFiniteNumberLoose(row.pair_iptm_target_binder) !== null || readFiniteNumberLoose(row.pair_iptm) !== null) score += 8;
  if (readFiniteNumberLoose(row.binder_avg_plddt) !== null || readFiniteNumberLoose(row.plddt) !== null) score += 6;
  if (readFiniteNumberLoose(row.score) !== null || readFiniteNumberLoose(row.composite_score) !== null) score += 4;
  score += countCandidateStorageNumbers(row.residue_plddt_by_chain) > 0 ? 4 : 0;
  score += countCandidateStorageNumbers(row.residue_plddt) > 0 ? 3 : 0;
  score += Object.keys(row).length / 100;
  return score;
}

export function hasMeaningfulCandidateValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === 'string') return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value as Record<string, unknown>).length > 0;
  return true;
}

/**
 * Merge persisted candidate rows into freshly parsed rows, field by field.
 *
 * Preferred-structure archives carry only the requested structure file, so a
 * fresh parse legitimately knows FEWER structure names than the rows already
 * persisted from a full parse. Replacing the persisted collection wholesale
 * (the old copyMissingFields behavior — a non-empty parsed list always wins)
 * erased those names and broke every subsequent candidate switch. The merged
 * row is the incoming row with empty slots backfilled from its persisted twin;
 * persisted rows with no incoming counterpart are kept.
 */
export function mergePeptideCandidateRowsPreferRicher(
  incomingRows: Array<Record<string, unknown>>,
  existingRows: Array<Record<string, unknown>>
): Array<Record<string, unknown>> {
  if (existingRows.length === 0) return incomingRows;
  if (incomingRows.length === 0) return existingRows;
  const existingByIdentity = new Map<string, Record<string, unknown>>();
  existingRows.forEach((row, index) => {
    existingByIdentity.set(peptideCandidateStorageIdentity(row, index), row);
  });
  const merged: Array<Record<string, unknown>> = [];
  const consumed = new Set<string>();
  incomingRows.forEach((row, index) => {
    const identity = peptideCandidateStorageIdentity(row, index);
    const existing = existingByIdentity.get(identity);
    consumed.add(identity);
    if (!existing) {
      merged.push(row);
      return;
    }
    const next = { ...existing };
    for (const [key, value] of Object.entries(row)) {
      if (hasMeaningfulCandidateValue(value) || !hasMeaningfulCandidateValue(next[key])) {
        next[key] = value;
      }
    }
    merged.push(next);
  });
  for (const [identity, row] of existingByIdentity) {
    if (!consumed.has(identity)) merged.push(row);
  }
  return merged;
}

export function collectPeptideCandidateRows(input: Record<string, unknown>): Array<Record<string, unknown>> {
  const byIdentity = new Map<string, { row: Record<string, unknown>; score: number; index: number }>();
  let index = 0;
  for (const path of PEPTIDE_CANDIDATE_ARRAY_PATHS) {
    for (const row of asRecordArray(readObjectPathLoose(input, path))) {
      const key = peptideCandidateStorageIdentity(row, index);
      const score = peptideCandidateStorageRichness(row);
      const existing = byIdentity.get(key);
      if (!existing || score > existing.score) {
        byIdentity.set(key, { row, score, index: existing?.index ?? index });
      }
      index += 1;
    }
  }
  return Array.from(byIdentity.values())
    .sort((a, b) => a.index - b.index)
    .map((entry) => entry.row);
}

export function compactPeptideCandidateRows(
  rows: Array<Record<string, unknown>>,
  preservePeptideCandidateStructureText: boolean
): Array<Record<string, unknown>> {
  return rows.map((row) => {
    const compact = preservePeptideCandidateStructureText ? { ...row } : stripCandidateStructureTextFields(row);
    return compact;
  });
}

export function compactConfidenceForStorage(
  input: Record<string, unknown>,
  options?: { preservePeptideCandidateStructureText?: boolean }
): Record<string, unknown> {
  const next = { ...input };
  const preservePeptideCandidateStructureText = Boolean(options?.preservePeptideCandidateStructureText);
  const pae = next.pae;
  // Full PAE matrices dominate payload size; scalar summaries remain in confidence.
  if (Array.isArray(pae) || (pae && typeof pae === 'object')) {
    delete next.pae;
  }

  const compactCandidateRows = compactPeptideCandidateRows(
    collectPeptideCandidateRows(input),
    preservePeptideCandidateStructureText
  );

  stripPeptideCandidateArrayKeys(next);

  const topProgressRaw = next.progress;
  if (isPlainRecord(topProgressRaw)) {
    const topProgress = { ...topProgressRaw };
    stripPeptideCandidateArrayKeys(topProgress);
    if (Object.keys(topProgress).length > 0) {
      next.progress = topProgress;
    } else {
      delete next.progress;
    }
  }

  const peptideDesignRaw = next.peptide_design;
  const peptideDesign = isPlainRecord(peptideDesignRaw) ? { ...peptideDesignRaw } : {};
  stripPeptideCandidateArrayKeys(peptideDesign);
  const peptideProgressRaw = peptideDesign.progress;
  if (isPlainRecord(peptideProgressRaw)) {
    const peptideProgress = { ...peptideProgressRaw };
    stripPeptideCandidateArrayKeys(peptideProgress);
    if (Object.keys(peptideProgress).length > 0) {
      peptideDesign.progress = peptideProgress;
    } else {
      delete peptideDesign.progress;
    }
  }

  if (compactCandidateRows.length > 0) {
    peptideDesign.best_sequences = compactCandidateRows;
    if (!Number.isFinite(Number(peptideDesign.candidate_count))) {
      peptideDesign.candidate_count = compactCandidateRows.length;
    }
  }

  if (Object.keys(peptideDesign).some((key) => hasStorageValue(peptideDesign[key]))) {
    next.peptide_design = peptideDesign;
  } else {
    delete next.peptide_design;
  }

  return next;
}

export function compactResultConfidenceForStorage(
  input: Record<string, unknown>,
  options?: { preservePeptideCandidateStructureText?: boolean }
): Record<string, unknown> {
  return compactConfidenceForStorage(input, options);
}

export const PEPTIDE_CANDIDATE_RESIDUE_PATH_ALIASES: Array<{ path: string; asKey: string }> = [
  { path: 'confidence.residue_plddt', asKey: 'residue_plddt' },
  { path: 'confidence.residue_plddts', asKey: 'residue_plddts' },
  { path: 'confidence.plddts', asKey: 'plddts' },
  { path: 'confidence.per_residue_plddt', asKey: 'per_residue_plddt' },
  { path: 'confidence.binder_residue_plddt', asKey: 'binder_residue_plddt' },
  { path: 'confidence.token_plddt', asKey: 'token_plddt' },
  { path: 'confidence.token_plddts', asKey: 'token_plddts' },
  { path: 'metrics.residue_plddt', asKey: 'residue_plddt' },
  { path: 'metrics.per_residue_plddt', asKey: 'per_residue_plddt' },
  { path: 'scores.plddts', asKey: 'plddts' },
  { path: 'scores.residue_plddt', asKey: 'residue_plddt' },
  { path: 'scores.per_residue_plddt', asKey: 'per_residue_plddt' },
  { path: 'confidence.residue_plddt_by_chain', asKey: 'residue_plddt_by_chain' },
  { path: 'confidence.residuePlddtByChain', asKey: 'residuePlddtByChain' },
  { path: 'confidence.residue_plddts_by_chain', asKey: 'residue_plddts_by_chain' },
  { path: 'confidence.chain_residue_plddt', asKey: 'chain_residue_plddt' },
  { path: 'confidence.chain_plddt', asKey: 'chain_plddt' },
  { path: 'confidence.chain_plddts', asKey: 'chain_plddts' }
];
