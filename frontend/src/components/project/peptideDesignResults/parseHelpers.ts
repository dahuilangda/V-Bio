/** Pure parsing/metrics helpers; no JSX, no hooks. */
import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { normalizePlddt } from '../../../pages/projectTasks/taskDataConfidence';
import { asRecord, asRecordArray, asString } from '../../../pages/projectTasks/recordReaders';


export type ResultsGridStyle = CSSProperties & { '--results-main-width'?: string };
export type RuntimeState = 'SUCCESS' | 'RUNNING' | 'QUEUED' | 'FAILURE' | 'UNSCORED';
export type PeptideSortKey = 'rank' | 'generation' | 'score' | 'plddt' | 'interface';
export type ConfidenceTone = 'vhigh' | 'high' | 'low' | 'vlow' | 'na';
export const PEPTIDE_RESULTS_PAGE_SIZE_OPTIONS = [8, 20, 50, 100] as const;
export const EMPTY_RECORD_ROWS: Array<Record<string, unknown>> = [];

export interface PeptideDesignResultsWorkspaceProps {
  projectTaskId: string;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: ResultsGridStyle;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  snapshotConfidence: Record<string, unknown>;
  statusInfo: Record<string, unknown>;
  projectTaskState: string;
  progressPercent: number;
  displayStructureText: string;
  displayStructureFormat: 'cif' | 'pdb';
  displayStructureName: string;
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  selectedResultLigandSequence: string;
  confidenceBackend: string;
  projectBackend: string;
  fallbackPlddt: number | null;
  fallbackIptm: number | null;
  onRequestStructure?: (options?: { preferredStructureName?: string }) => Promise<void> | void;
}

export interface PeptideDesignCandidateModification {
  position: number;
  ccd: string;
  baseResidue: string;
}

export interface PeptideDesignCandidate {
  id: string;
  rank: number;
  sequence: string;
  modifications: PeptideDesignCandidateModification[];
  score: number | null;
  plddt: number | null;
  residuePlddts: number[];
  interfaceMetric: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  interfaceMetricSource: 'ipsae' | 'iptm' | 'none';
  iptm: number | null;
  ipsae: number | null;
  generation: number | null;
  modelLabel: string;
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  structureName: string;
  runtimeState: RuntimeState;
  source: 'result' | 'live';
}

export interface PeptideRuntimeContext {
  state: RuntimeState;
  currentStatus: string;
  statusMessage: string;
  currentGeneration: number | null;
  totalGenerations: number | null;
  bestScore: number | null;
  progressPercent: number | null;
  completedTasks: number | null;
  pendingTasks: number | null;
  totalTasks: number | null;
  generationCompletedTasks: number | null;
  generationRunningTasks: number | null;
  generationQueuedTasks: number | null;
  generationTotalTasks: number | null;
  elapsedSeconds: number | null;
  estimatedRemainingSeconds: number | null;
  estimatedCompletionTime: string;
  candidatesEvaluated: number | null;
  adaptiveMutationRate: number | null;
  stagnantGenerations: number | null;
  liveCandidateRows: Array<Record<string, unknown>>;
}



export function getBaseName(path: string): string {
  const parts = path.split(/[\/\\]/);
  return parts[parts.length - 1] || path;
}

export function normalizeStructureToken(value: string): string {
  return value.trim().replace(/^[\/\\]+/, '').toLowerCase();
}

export function structureNameMatches(loadedName: string, candidateName: string): boolean {
  const candidateToken = normalizeStructureToken(candidateName);
  if (!candidateToken) return false;
  const loadedToken = normalizeStructureToken(loadedName);
  if (!loadedToken || loadedToken === '-') return false;
  if (loadedToken === candidateToken) return true;
  return normalizeStructureToken(getBaseName(loadedToken)) === normalizeStructureToken(getBaseName(candidateToken));
}

export function readFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

export function readObjectPath(payload: Record<string, unknown>, path: string): unknown {
  let current: unknown = payload;
  for (const token of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[token];
  }
  return current;
}

export function chainTokenEquals(a: string, b: string): boolean {
  const left = normalizeChainToken(a);
  const right = normalizeChainToken(b);
  if (!left || !right) return false;
  if (left === right) return true;
  const compactLeft = left.replace(/[^A-Z0-9]/g, '');
  const compactRight = right.replace(/[^A-Z0-9]/g, '');
  if (compactLeft && compactRight && compactLeft === compactRight) return true;
  const leftTokens = left.split(/[^A-Z0-9]+/).filter(Boolean);
  if (leftTokens.includes(right) || (compactRight && leftTokens.includes(compactRight))) {
    return true;
  }
  if (compactLeft && compactRight) {
    if (compactLeft.startsWith(compactRight) || compactLeft.endsWith(compactRight)) {
      return true;
    }
    if (compactRight.startsWith(compactLeft) || compactRight.endsWith(compactLeft)) {
      return true;
    }
  }
  return false;
}

export function chainVariants(chainId: string): string[] {
  const token = asString(chainId).trim();
  if (!token) return [];
  const variants: string[] = [];
  const push = (value: string) => {
    const normalized = value.trim();
    if (!normalized) return;
    if (!variants.some((item) => chainTokenEquals(item, normalized))) variants.push(normalized);
  };
  push(token);
  push(token.toUpperCase());
  push(token.toLowerCase());
  return variants;
}

export function toChainList(value: unknown): string[] {
  if (Array.isArray(value)) {
    const rows: string[] = [];
    for (const item of value) {
      const text = asString(item).trim();
      if (!text) continue;
      rows.push(text);
    }
    return rows;
  }
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return [];
    return text
      .split(/[\s,;|]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  const token = asString(value).trim();
  return token ? [token] : [];
}

export function addChainHints(bucket: string[], value: unknown) {
  for (const token of toChainList(value)) {
    if (!bucket.some((entry) => chainTokenEquals(entry, token))) {
      bucket.push(token);
    }
  }
}

export function isNumericToken(value: string): boolean {
  return /^\d+$/.test(value.trim());
}

export function readMapValueByChainToken(record: Record<string, unknown>, token: string): unknown {
  if (Object.prototype.hasOwnProperty.call(record, token)) return record[token];
  for (const [key, value] of Object.entries(record)) {
    if (chainTokenEquals(key, token) || chainTokenEquals(token, key)) return value;
  }
  return undefined;
}

export function readPairValueFromNestedMap(mapValue: unknown, chainA: string, chainB: string): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;

  const rowA = readMapValueByChainToken(byChain, chainA);
  if (!rowA || typeof rowA !== 'object' || Array.isArray(rowA)) return null;
  const direct = normalizeIptm(readFiniteNumber(readMapValueByChainToken(rowA as Record<string, unknown>, chainB)));
  if (direct !== null) return direct;
  return null;
}

export function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[],
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex((item) => chainTokenEquals(item, chainA));
  const idxB = chainOrderHints.findIndex((item) => chainTokenEquals(item, chainB));
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const ligandToTarget = readPairValueFromNestedMap(byChain, String(idxB), String(idxA));
    const targetToLigand = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
      const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
      const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
      return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
    }
    if (ligandToTarget !== null) return ligandToTarget;
    if (targetToLigand !== null) return targetToLigand;
  }

  if (keys.length === 2 && preferredDirectionalIptm !== null) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const forward = readPairValueFromNestedMap(byChain, first, second);
    const backward = readPairValueFromNestedMap(byChain, second, first);
    if (forward !== null && backward !== null) {
      const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
      const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
      return forwardDelta <= backwardDelta ? forward : backward;
    }
    if (forward !== null) return forward;
    if (backward !== null) return backward;
  }
  return null;
}

export function readPairValueFromAnyTwoKeyMap(
  mapValue: unknown,
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  if (preferredDirectionalIptm === null) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length !== 2) return null;
  const [first, second] = keys;
  const forward = readPairValueFromNestedMap(byChain, first, second);
  const backward = readPairValueFromNestedMap(byChain, second, first);
  if (forward === null && backward === null) return null;
  if (forward !== null && backward !== null) {
    const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
    const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
    return forwardDelta <= backwardDelta ? forward : backward;
  }
  return forward ?? backward;
}

export function readPairIptmForChains(
  payload: Record<string, unknown>,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  if (!chainA || !chainB || chainTokenEquals(chainA, chainB)) return null;
  const preferredDirectionalIptm = normalizeIptm(firstFiniteMetric(payload, ['ligand_iptm', 'iptm']));
  const pairMap = payload.pair_chains_iptm;
  const ligandToTarget = readPairValueFromNestedMap(pairMap, chainB, chainA);
  if (ligandToTarget !== null) return ligandToTarget;
  const targetToLigand = readPairValueFromNestedMap(pairMap, chainA, chainB);
  if (targetToLigand !== null) return targetToLigand;

  const chainIdsRaw = toChainList(payload.chain_ids);
  const chainIds = chainIdsRaw.length > 0 ? chainIdsRaw : chainOrderHints;
  const numericMapped = readPairValueFromNumericMap(pairMap, chainA, chainB, chainIds, preferredDirectionalIptm);
  if (numericMapped !== null) return numericMapped;
  const twoKeyMapped = readPairValueFromAnyTwoKeyMap(pairMap, preferredDirectionalIptm);
  if (twoKeyMapped !== null) return twoKeyMapped;

  const matrixRaw = payload.chain_pair_iptm ?? payload.chain_pair_iptm_global;
  if (Array.isArray(matrixRaw)) {
    const i = chainIds.findIndex((item) => chainTokenEquals(item, chainA));
    const j = chainIds.findIndex((item) => chainTokenEquals(item, chainB));
    if (i >= 0 && j >= 0 && i !== j) {
      const rowI = matrixRaw[i];
      const rowJ = matrixRaw[j];
      const matrixLigandToTarget = Array.isArray(rowJ) ? normalizeIptm(readFiniteNumber(rowJ[i])) : null;
      const matrixTargetToLigand = Array.isArray(rowI) ? normalizeIptm(readFiniteNumber(rowI[j])) : null;
      if (matrixLigandToTarget !== null && matrixTargetToLigand !== null && preferredDirectionalIptm !== null) {
        const ligandDelta = Math.abs(matrixLigandToTarget - preferredDirectionalIptm);
        const targetDelta = Math.abs(matrixTargetToLigand - preferredDirectionalIptm);
        return ligandDelta <= targetDelta ? matrixLigandToTarget : matrixTargetToLigand;
      }
      if (matrixLigandToTarget !== null) return matrixLigandToTarget;
      if (matrixTargetToLigand !== null) return matrixTargetToLigand;
    }
  }
  return null;
}

export function resolvePairIptmForCandidate(
  row: Record<string, unknown>,
  preferredTargetChainId: string | undefined,
  preferredLigandChainId: string | undefined
): number | null {
  const nested = [
    asRecord(row.result),
    asRecord(row.prediction),
    asRecord(row.metadata),
    asRecord(row.structure_payload),
    asRecord(row.confidence),
    asRecord(row.metrics),
    asRecord(row.affinity)
  ];
  const payloads = [row, ...nested];
  const targetHints: string[] = [];
  const ligandHints: string[] = [];
  const chainOrderHints: string[] = [];

  // Primary source for peptide design rows: candidate-level ipTM in results_summary/design_results.
  const directRowValue = normalizeIptm(
    firstFiniteMetric(row, [
      'pair_iptm_target_binder',
      'pairIptmTargetBinder',
      'pair_iptm',
      'pairIptm'
    ])
  );
  if (directRowValue !== null) return directRowValue;

  addChainHints(targetHints, preferredTargetChainId);
  addChainHints(ligandHints, preferredLigandChainId);

  for (const payload of payloads) {
    addChainHints(targetHints, payload.target_chain_id);
    addChainHints(targetHints, payload.requested_target_chain_id);
    addChainHints(targetHints, payload.protein_chain_id);
    addChainHints(targetHints, payload.peptide_design_target_chain);
    addChainHints(targetHints, payload.target_chain_ids);
    addChainHints(targetHints, payload.protein_chain_ids);

    addChainHints(ligandHints, payload.ligand_chain_id);
    addChainHints(ligandHints, payload.requested_ligand_chain_id);
    addChainHints(ligandHints, payload.model_ligand_chain_id);
    addChainHints(ligandHints, payload.binder_chain_id);
    addChainHints(ligandHints, payload.peptide_chain_id);
    addChainHints(ligandHints, payload.ligand_chain_ids);
    addChainHints(ligandHints, payload.binder_chain_ids);

    addChainHints(chainOrderHints, payload.chain_ids);
    addChainHints(chainOrderHints, payload.chain_order);
  }

  if (targetHints.length === 0 && ligandHints.length > 0 && chainOrderHints.length > 0) {
    for (const chainId of chainOrderHints) {
      if (!ligandHints.some((ligand) => chainTokenEquals(ligand, chainId))) addChainHints(targetHints, chainId);
    }
  }
  if (ligandHints.length === 0 && targetHints.length > 0 && chainOrderHints.length > 0) {
    for (const chainId of chainOrderHints) {
      if (!targetHints.some((target) => chainTokenEquals(target, chainId))) addChainHints(ligandHints, chainId);
    }
  }

  addChainHints(chainOrderHints, targetHints);
  addChainHints(chainOrderHints, ligandHints);

  if (targetHints.length > 0 && ligandHints.length > 0) {
    for (const targetHint of targetHints) {
      for (const ligandHint of ligandHints) {
        if (chainTokenEquals(targetHint, ligandHint)) continue;
        for (const targetCandidate of chainVariants(targetHint)) {
          for (const ligandCandidate of chainVariants(ligandHint)) {
            for (const payload of payloads) {
              const pairValue = readPairIptmForChains(payload, targetCandidate, ligandCandidate, chainOrderHints);
              if (pairValue !== null) return pairValue;
            }
          }
        }
      }
    }
  }

  for (const payload of payloads) {
    const pairScalar = normalizeIptm(
      firstFiniteMetric(payload, [
        'pair_iptm_target_binder',
        'pairIptmTargetBinder',
        'pair_iptm',
        'pairIptm'
      ])
    );
    if (pairScalar !== null) return pairScalar;
    const globalIptm = normalizeIptm(firstFiniteMetric(payload, ['ligand_iptm', 'iptm', 'protein_iptm']));
    if (globalIptm !== null) return globalIptm;
  }
  return null;
}

export function readPreferredInterfaceMetricForCandidate(
  row: Record<string, unknown>,
  preferredTargetChainId: string | undefined,
  preferredLigandChainId: string | undefined
): { value: number | null; label: 'IPSAE' | 'ipTM'; source: 'ipsae' | 'iptm' | 'none' } {
  const nested = [
    asRecord(row.result),
    asRecord(row.prediction),
    asRecord(row.metadata),
    asRecord(row.structure_payload),
    asRecord(row.confidence),
    asRecord(row.metrics),
    asRecord(row.affinity)
  ];
  const payloads = [row, ...nested];

  for (const payload of payloads) {
    const ligandIpsaeMax = normalizeIptm(firstFiniteMetric(payload, ['ligand_ipsae_max', 'ligandIpsaeMax']));
    if (ligandIpsaeMax !== null) {
      return { value: ligandIpsaeMax, label: 'IPSAE', source: 'ipsae' };
    }
    const ipsaeDom = normalizeIptm(firstFiniteMetric(payload, ['ipsae_dom', 'ipsaeDom']));
    if (ipsaeDom !== null) {
      return { value: ipsaeDom, label: 'IPSAE', source: 'ipsae' };
    }
    // interface_metric carries its own label (IPSAE or IPTM).
    const interfaceMetric = normalizeIptm(firstFiniteMetric(payload, ['interface_metric', 'interfaceMetric']));
    if (interfaceMetric !== null) {
      const label = firstNonEmptyText(payload, ['interface_metric_label', 'interfaceMetricLabel']).toUpperCase();
      if (label === 'IPSAE') {
        return { value: interfaceMetric, label: 'IPSAE', source: 'ipsae' };
      }
      if (label === 'IPTM') {
        return { value: interfaceMetric, label: 'ipTM', source: 'iptm' };
      }
    }
  }

  const iptm = resolvePairIptmForCandidate(row, preferredTargetChainId, preferredLigandChainId);
  if (iptm !== null) {
    return { value: iptm, label: 'ipTM', source: 'iptm' };
  }
  return { value: null, label: 'IPSAE', source: 'none' };
}

export function normalizeWeight(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  if (value > 1 && value <= 100) return value / 100;
  if (value < 0) return null;
  return value;
}

export function computePeptideCompositeScore(
  row: Record<string, unknown>,
  plddt: number | null,
  interfaceMetricValue: number | null
): number | null {
  if (plddt === null || interfaceMetricValue === null) return null;
  const nested = [row, asRecord(row.result), asRecord(row.prediction), asRecord(row.metadata), asRecord(row.scoring)];
  let wPlddt = normalizeWeight(
    readFirstFiniteFromPaths(nested, ['w1', 'weight_plddt', 'plddt_weight', 'score_weight_plddt', 'weights.plddt'])
  );
  let wIptm = normalizeWeight(
    readFirstFiniteFromPaths(nested, ['w2', 'weight_iptm', 'iptm_weight', 'score_weight_iptm', 'weights.iptm'])
  );
  if (wPlddt === null && wIptm === null) {
    wPlddt = 0.3;
    wIptm = 0.7;
  } else if (wPlddt === null) {
    wIptm = wIptm ?? 0.7;
    wPlddt = Math.max(0, 1 - wIptm);
  } else if (wIptm === null) {
    wPlddt = wPlddt ?? 0.3;
    wIptm = Math.max(0, 1 - wPlddt);
  }
  const sum = (wPlddt ?? 0) + (wIptm ?? 0);
  if (!Number.isFinite(sum) || sum <= 0) return null;
  const wp = (wPlddt ?? 0) / sum;
  const wi = (wIptm ?? 0) / sum;
  return wp * (plddt / 100) + wi * interfaceMetricValue;
}

export function normalizeIptm(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

export function detectStructureFormat(text: string, hinted: unknown): 'cif' | 'pdb' {
  const hint = asString(hinted).trim().toLowerCase();
  if (hint === 'pdb' || hint === 'cif') return hint;
  const head = text.trim().slice(0, 20).toUpperCase();
  if (head.startsWith('ATOM') || head.startsWith('HETATM') || head.startsWith('HEADER')) return 'pdb';
  return 'cif';
}

export function firstNonEmptyText(source: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = asString(source[key]).trim();
    if (value) return value;
  }
  return '';
}

export function firstFiniteMetric(source: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = readFiniteNumber(source[key]);
    if (value !== null) return value;
  }
  return null;
}

export function parseNumberList(value: unknown): number[] {
  if (Array.isArray(value)) {
    return value
      .map((item) => readFiniteNumber(item))
      .filter((item): item is number => item !== null);
  }
  if (typeof value === 'string') {
    const token = value.trim();
    if (!token) return [];
    if (token.startsWith('[') && token.endsWith(']')) {
      try {
        const parsed = JSON.parse(token) as unknown;
        return parseNumberList(parsed);
      } catch {
        // Fall through to split parsing.
      }
    }
    return token
      .split(/[\s,;]+/)
      .map((item) => readFiniteNumber(item))
      .filter((item): item is number => item !== null);
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const scalarEntries = Object.entries(record)
      .map(([key, item]) => ({
        key,
        keyNumber: Number(key),
        value: readFiniteNumber(item)
      }))
      .filter((entry) => entry.value !== null);
    const numericKeyEntries = scalarEntries.filter((entry) => Number.isFinite(entry.keyNumber));
    if (numericKeyEntries.length >= 3 && numericKeyEntries.length >= Math.floor(scalarEntries.length * 0.6)) {
      numericKeyEntries.sort((a, b) => a.keyNumber - b.keyNumber);
      return numericKeyEntries.map((entry) => entry.value as number);
    }
    if (Array.isArray(record.values)) return parseNumberList(record.values);
    if (Array.isArray(record.scores)) return parseNumberList(record.scores);
    if (Array.isArray(record.plddt)) return parseNumberList(record.plddt);
    if (Array.isArray(record.plddts)) return parseNumberList(record.plddts);
    if (Array.isArray(record.residue_plddt)) return parseNumberList(record.residue_plddt);
    if (Array.isArray(record.residue_plddts)) return parseNumberList(record.residue_plddts);
    if (Array.isArray(record.per_residue_plddt)) return parseNumberList(record.per_residue_plddt);
    if (Array.isArray(record.token_plddt)) return parseNumberList(record.token_plddt);
    if (Array.isArray(record.token_plddts)) return parseNumberList(record.token_plddts);
    for (const entry of Object.values(record)) {
      const nested = parseNumberList(entry);
      if (nested.length > 0) return nested;
    }
  }
  return [];
}

export function normalizePlddtList(values: number[]): number[] {
  return values
    .map((item) => normalizePlddt(item))
    .filter((item): item is number => item !== null && Number.isFinite(item));
}

export function alignResidueSeriesToSequence(values: number[], sequenceLength: number): number[] {
  const normalized = normalizePlddtList(values);
  if (normalized.length === 0) return [];
  if (sequenceLength <= 0) return normalized;
  if (normalized.length === sequenceLength) return normalized;
  if (normalized.length < Math.min(sequenceLength, 4)) return [];
  if (normalized.length > sequenceLength) {
    const reduced: number[] = [];
    for (let i = 0; i < sequenceLength; i += 1) {
      const start = Math.floor((i * normalized.length) / sequenceLength);
      const end = Math.max(start + 1, Math.floor(((i + 1) * normalized.length) / sequenceLength));
      const chunk = normalized.slice(start, end);
      const avg = chunk.reduce((sum, value) => sum + value, 0) / chunk.length;
      reduced.push(avg);
    }
    return reduced;
  }
  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * normalized.length) / sequenceLength);
    expanded.push(normalized[Math.min(normalized.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

export function readResiduePlddtByChainSeries(
  payloads: Array<Record<string, unknown>>,
  sequenceLength: number,
  preferredChainId?: string
): number[] {
  if (sequenceLength <= 0) return [];
  const byChain = new Map<string, { chainId: string; values: number[] }>();
  const mapPaths = [
    'residue_plddt_by_chain',
    'residuePlddtByChain',
    'residue_plddts_by_chain',
    'confidence.residue_plddt_by_chain',
    'confidence.residuePlddtByChain'
  ];

  for (const payload of payloads) {
    for (const path of mapPaths) {
      const raw = readObjectPath(payload, path);
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
      for (const [chainIdRaw, chainValuesRaw] of Object.entries(raw as Record<string, unknown>)) {
        const chainId = asString(chainIdRaw).trim();
        if (!chainId) continue;
        const values = normalizePlddtList(parseNumberList(chainValuesRaw));
        if (values.length === 0) continue;
        const key = normalizeChainToken(chainId);
        const existing = byChain.get(key);
        if (!existing || values.length > existing.values.length) {
          byChain.set(key, { chainId, values });
        }
      }
    }
  }

  const entries = [...byChain.values()];
  if (entries.length === 0) return [];

  const preferredToken = normalizeChainToken(asString(preferredChainId));
  let best: { chainId: string; values: number[]; score: number } | null = null;
  for (const entry of entries) {
    const chainToken = normalizeChainToken(entry.chainId);
    const length = entry.values.length;
    let score = 0;
    score -= Math.abs(length - sequenceLength) * 4;
    if (length === sequenceLength) score += 30;
    if (length >= Math.max(1, sequenceLength - 2) && length <= sequenceLength + 2) score += 16;
    if (sequenceLength >= 8 && length <= 4) score -= 40;
    if (preferredToken && chainToken === preferredToken) score += 12;
    if (!best || score > best.score) {
      best = { chainId: entry.chainId, values: entry.values, score };
    }
  }
  if (!best) return [];
  return alignResidueSeriesToSequence(best.values, sequenceLength);
}

export function normalizeChainToken(chainId: string): string {
  return chainId.trim().toUpperCase();
}

export function residueToOneLetter(name: string): string {
  const token = name.trim().toUpperCase();
  if (token === 'ALA') return 'A';
  if (token === 'ARG') return 'R';
  if (token === 'ASN') return 'N';
  if (token === 'ASP') return 'D';
  if (token === 'CYS') return 'C';
  if (token === 'GLN') return 'Q';
  if (token === 'GLU') return 'E';
  if (token === 'GLY') return 'G';
  if (token === 'HIS') return 'H';
  if (token === 'ILE') return 'I';
  if (token === 'LEU') return 'L';
  if (token === 'LYS') return 'K';
  if (token === 'MET') return 'M';
  if (token === 'PHE') return 'F';
  if (token === 'PRO') return 'P';
  if (token === 'SER') return 'S';
  if (token === 'THR') return 'T';
  if (token === 'TRP') return 'W';
  if (token === 'TYR') return 'Y';
  if (token === 'VAL') return 'V';
  if (token === 'SEC') return 'U';
  if (token === 'PYL') return 'O';
  return 'X';
}

export function sequenceMatchScore(chainSequence: string, peptideSequence: string): number {
  const chain = chainSequence.trim().toUpperCase();
  const peptide = peptideSequence.trim().toUpperCase();
  if (!chain || !peptide) return Number.NEGATIVE_INFINITY;
  if (chain === peptide) return 10_000;
  if (chain.includes(peptide)) return 9_000 - Math.abs(chain.length - peptide.length);

  let bestMatches = 0;
  if (chain.length >= peptide.length) {
    for (let start = 0; start <= chain.length - peptide.length; start += 1) {
      let matches = 0;
      for (let idx = 0; idx < peptide.length; idx += 1) {
        if (chain[start + idx] === peptide[idx]) matches += 1;
      }
      if (matches > bestMatches) bestMatches = matches;
    }
  } else {
    for (let idx = 0; idx < chain.length; idx += 1) {
      if (chain[idx] === peptide[idx]) bestMatches += 1;
    }
  }
  return (bestMatches / peptide.length) * 1000 - Math.abs(chain.length - peptide.length) * 2;
}

export function tokenizeCifRow(line: string): string[] {
  const tokens: string[] = [];
  const re = /'([^']*)'|"([^"]*)"|(\S+)/g;
  let match: RegExpExecArray | null = null;
  while ((match = re.exec(line)) !== null) {
    tokens.push(match[1] ?? match[2] ?? match[3]);
  }
  return tokens;
}

export interface PolymerResidueEntry {
  seq: number;
  ins: string;
  residueName: string;
}

export function pushPolymerResidue(
  chains: Map<string, Map<string, PolymerResidueEntry>>,
  chainId: string,
  residueKey: string,
  seq: number,
  ins: string,
  residueName: string
) {
  let chain = chains.get(chainId);
  if (!chain) {
    chain = new Map<string, PolymerResidueEntry>();
    chains.set(chainId, chain);
  }
  if (chain.has(residueKey)) return;
  chain.set(residueKey, { seq, ins, residueName });
}

export function extractPolymerChainsFromPdb(structureText: string): Map<string, Map<string, PolymerResidueEntry>> {
  const chains = new Map<string, Map<string, PolymerResidueEntry>>();
  for (const line of structureText.split(/\r?\n/)) {
    if (!line.startsWith('ATOM')) continue;
    const chainId = line.slice(21, 22).trim() || '_';
    const residueName = line.slice(17, 20).trim().toUpperCase();
    const seqRaw = line.slice(22, 26).trim();
    const ins = line.slice(26, 27).trim();
    const seq = Number(seqRaw);
    if (!Number.isFinite(seq)) continue;
    const residueKey = `${seqRaw}:${ins || '_'}`;
    pushPolymerResidue(chains, chainId, residueKey, Math.floor(seq), ins, residueName);
  }
  return chains;
}

export function extractPolymerChainsFromCif(structureText: string): Map<string, Map<string, PolymerResidueEntry>> {
  const chains = new Map<string, Map<string, PolymerResidueEntry>>();
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;
    const headers: string[] = [];
    let j = i + 1;
    while (j < lines.length) {
      const headerLine = lines[j].trim();
      if (!headerLine.startsWith('_')) break;
      headers.push(headerLine);
      j += 1;
    }
    if (!headers.some((header) => header.startsWith('_atom_site.'))) {
      i = j;
      continue;
    }
    const groupIdx = headers.findIndex((header) => header === '_atom_site.group_PDB');
    const chainIdx = headers.findIndex((header) => header === '_atom_site.label_asym_id' || header === '_atom_site.auth_asym_id');
    const seqIdx = headers.findIndex((header) => header === '_atom_site.label_seq_id' || header === '_atom_site.auth_seq_id');
    const compIdx = headers.findIndex((header) => header === '_atom_site.label_comp_id' || header === '_atom_site.auth_comp_id');
    const insIdx = headers.findIndex((header) => header === '_atom_site.pdbx_PDB_ins_code');
    if (chainIdx < 0 || seqIdx < 0 || compIdx < 0) {
      i = j;
      continue;
    }

    while (j < lines.length) {
      const rowLine = lines[j].trim();
      if (!rowLine || rowLine === '#') {
        j += 1;
        continue;
      }
      if (rowLine === 'loop_' || rowLine.startsWith('_')) {
        j -= 1;
        break;
      }
      const tokens = tokenizeCifRow(rowLine);
      if (tokens.length <= Math.max(chainIdx, seqIdx, compIdx)) {
        j += 1;
        continue;
      }
      const group = groupIdx >= 0 ? asString(tokens[groupIdx]).trim().toUpperCase() : 'ATOM';
      if (group && group !== 'ATOM') {
        j += 1;
        continue;
      }
      const chainId = asString(tokens[chainIdx]).trim() || '_';
      const seqToken = asString(tokens[seqIdx]).trim();
      const residueName = asString(tokens[compIdx]).trim().toUpperCase();
      const seq = Number(seqToken);
      const ins = insIdx >= 0 ? asString(tokens[insIdx]).trim() : '';
      if (!Number.isFinite(seq)) {
        j += 1;
        continue;
      }
      const residueKey = `${seqToken}:${ins || '_'}`;
      pushPolymerResidue(chains, chainId, residueKey, Math.floor(seq), ins, residueName);
      j += 1;
    }
    i = j;
  }
  return chains;
}

export function extractPolymerChainsFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb'
): Map<string, Map<string, PolymerResidueEntry>> {
  const text = structureText.trim();
  if (!text) return new Map();
  if (structureFormat === 'pdb') {
    return extractPolymerChainsFromPdb(text);
  }
  const cifChains = extractPolymerChainsFromCif(text);
  return cifChains.size > 0 ? cifChains : extractPolymerChainsFromPdb(text);
}

export function resolvePeptideFocusChainId(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  candidateSequence: string,
  preferredChainId?: string
): string | null {
  const chains = extractPolymerChainsFromStructure(structureText, structureFormat);
  if (chains.size === 0) return preferredChainId || null;

  const chainEntries = [...chains.entries()].map(([chainId, residueMap]) => {
    const residues = [...residueMap.values()].sort((a, b) => {
      if (a.seq !== b.seq) return a.seq - b.seq;
      return a.ins.localeCompare(b.ins);
    });
    const chainSequence = residues.map((item) => residueToOneLetter(item.residueName)).join('');
    return { chainId, residues, chainSequence };
  });

  const peptide = candidateSequence.trim().toUpperCase();
  const preferredToken = normalizeChainToken(asString(preferredChainId));

  if (peptide) {
    let best: { chainId: string; score: number } | null = null;
    for (const entry of chainEntries) {
      const score = sequenceMatchScore(entry.chainSequence, peptide);
      if (!Number.isFinite(score)) continue;
      if (!best || score > best.score) best = { chainId: entry.chainId, score };
    }
    if (best) {
      if (!preferredToken) return best.chainId;
      const preferredEntry = chainEntries.find((entry) => normalizeChainToken(entry.chainId) === preferredToken);
      if (!preferredEntry) return best.chainId;
      const preferredScore = sequenceMatchScore(preferredEntry.chainSequence, peptide);
      if (!Number.isFinite(preferredScore) || best.score > preferredScore + 20) return best.chainId;
      return preferredEntry.chainId;
    }
  }

  if (preferredToken) {
    const preferredEntry = chainEntries.find((entry) => normalizeChainToken(entry.chainId) === preferredToken);
    if (preferredEntry) return preferredEntry.chainId;
  }

  let longest = chainEntries[0];
  for (const entry of chainEntries) {
    if (entry.residues.length > longest.residues.length) longest = entry;
  }
  return longest.chainId;
}

export function parseCandidateResiduePlddts(
  row: Record<string, unknown>,
  sequenceLength: number,
  preferredChainId?: string
): number[] {
  const nested = [
    row,
    asRecord(row.result),
    asRecord(row.prediction),
    asRecord(row.metadata),
    asRecord(row.structure_payload)
  ];
  const directKeys = [
    'residue_plddt',
    'residue_plddts',
    'plddts',
    'residue_confidence',
    'residue_confidences',
    'residue_scores',
    'per_residue_plddt',
    'per_residue_confidence',
    'binder_residue_plddt',
    'binder_residue_plddts',
    'binder_plddt_per_residue',
    'plddt_per_residue',
    'plddt_by_residue',
    'aa_plddt',
    'aa_plddts',
    'ligand_residue_plddt',
    'ligand_residue_plddts',
    'token_plddt',
    'token_plddts'
  ];
  const pathKeys = [
    'confidence.residue_plddt',
    'confidence.residue_plddts',
    'confidence.per_residue_plddt',
    'confidence.binder_residue_plddt',
    'metrics.residue_plddt',
    'metrics.per_residue_plddt',
    'scores.residue_plddt',
    'scores.per_residue_plddt'
  ];

  for (const source of nested) {
    for (const key of directKeys) {
      const parsed = alignResidueSeriesToSequence(parseNumberList(source[key]), sequenceLength);
      if (parsed.length > 0) {
        return parsed;
      }
    }
    for (const path of pathKeys) {
      const parsed = alignResidueSeriesToSequence(parseNumberList(readObjectPath(source, path)), sequenceLength);
      if (parsed.length > 0) {
        return parsed;
      }
    }
  }

  const byChainSeries = readResiduePlddtByChainSeries(nested, sequenceLength, preferredChainId);
  if (byChainSeries.length > 0) return byChainSeries;


  return [];
}

export function readFirstFiniteFromPaths(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

export function readFirstTextFromPaths(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const text = asString(readObjectPath(payload, path)).trim();
      if (text) return text;
    }
  }
  return '';
}

export function readFirstRecordArrayFromPaths(payloads: Record<string, unknown>[], paths: string[]): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = asRecordArray(readObjectPath(payload, path));
      if (rows.length > 0) return rows;
    }
  }
  return [];
}

export function normalizeRuntimeState(raw: unknown): RuntimeState {
  const token = asString(raw).trim().toUpperCase();
  if (token === 'SUCCESS' || token === 'COMPLETED' || token === 'DONE') return 'SUCCESS';
  if (
    token === 'RUNNING' ||
    token === 'STARTED' ||
    token === 'STARTING' ||
    token === 'PROGRESS' ||
    token === 'PREPARING' ||
    token === 'ACQUIRING_GPU' ||
    token === 'GPU_ACQUIRED' ||
    token === 'TERMINATING'
  ) {
    return 'RUNNING';
  }
  if (token === 'QUEUED' || token === 'PENDING' || token === 'WAITING' || token === 'RECEIVED' || token === 'RETRY') {
    return 'QUEUED';
  }
  if (token === 'FAILURE' || token === 'FAILED' || token === 'ERROR') return 'FAILURE';
  return 'UNSCORED';
}

export function parseCandidateStructure(row: Record<string, unknown>): { structureText: string; structureFormat: 'cif' | 'pdb'; structureName: string } {
  const nested = [row, asRecord(row.result), asRecord(row.prediction), asRecord(row.structure_payload), asRecord(row.structure)];
  for (const source of nested) {
    const structureName = firstNonEmptyText(source, [
      'structureName',
      'structure_name',
      'structure_file',
      'structure_path',
      'name'
    ]);
    if (!structureName) continue;
    const formatHint = source.structureFormat ?? source.structure_format ?? source.format ?? structureName;
    const structureFormat = detectStructureFormat('', formatHint);
    return { structureText: '', structureFormat, structureName };
  }
  return { structureText: '', structureFormat: 'cif', structureName: '' };
}

export function readCandidateStructureName(row: Record<string, unknown>): string {
  const nested = [row, asRecord(row.result), asRecord(row.prediction), asRecord(row.structure_payload), asRecord(row.structure)];
  for (const source of nested) {
    const structureName = firstNonEmptyText(source, [
      'structureName',
      'structure_name',
      'structure_file',
      'structure_path',
      'name'
    ]);
    if (structureName) return structureName;
  }
  return '';
}

export function normalizeModelLabel(raw: string): string {
  const token = raw.trim();
  if (!token) return '';
  const lower = token.toLowerCase();
  if (lower === 'alphafold3' || lower === 'af3') return 'AF3';
  if (lower === 'protenix') return 'Protenix';
  if (lower === 'boltz') return 'Boltz';
  if (lower === 'live' || lower === 'final' || lower === 'result') return '';
  return token;
}

export function parseCandidateModelLabel(row: Record<string, unknown>, fallback: string): string {
  const nested = [asRecord(row.result), asRecord(row.prediction), asRecord(row.metadata), asRecord(row.structure_payload)];
  const candidates = [row, ...nested];
  for (const source of candidates) {
    const normalized = normalizeModelLabel(
      firstNonEmptyText(source, [
        'model',
        'backend',
        'engine',
        'model_backend',
        'prediction_backend',
        'backend_name',
        'backendLabel',
        'backend_label'
      ])
    );
    if (normalized) return normalized;
  }
  return normalizeModelLabel(fallback) || '-';
}

export function extractRawCandidates(snapshotConfidence: Record<string, unknown>): Array<Record<string, unknown>> {
  const candidatePaths = [
    'progress.current_best_sequences',
    'progress.best_sequences',
    'peptide_design.progress.current_best_sequences',
    'peptide_design.progress.best_sequences',
    'peptide_design.current_best_sequences',
    'current_best_sequences',
    'peptide_design.best_sequences',
    'best_sequences',
    'peptide_design.candidates',
    'designer.current_best_sequences',
    'designer.best_sequences',
    'designer.candidates',
    'results.current_best_sequences',
    'results.best_sequences',
    'results.candidates',
    'peptide_candidates',
    'designed_peptides',
    'design_candidates',
    'candidates'
  ];
  const rows: Array<Record<string, unknown>> = [];
  for (const path of candidatePaths) {
    rows.push(...asRecordArray(readObjectPath(snapshotConfidence, path)));
  }
  return dedupeRawCandidateRows(rows);
}

export function rawCandidateIdentity(row: Record<string, unknown>, index: number): string {
  const sequence = firstNonEmptyText(row, [
    'peptide_sequence',
    'binder_sequence',
    'candidate_sequence',
    'designed_sequence',
    'sequence'
  ])
    .replace(/\s+/g, '')
    .trim()
    .toUpperCase();
  const generation = firstFiniteMetric(row, ['generation', 'iteration', 'iter']);
  if (sequence) {
    return generation === null ? `seq:${sequence}` : `seq:${sequence}|gen:${Math.floor(generation)}`;
  }
  const structureName = readCandidateStructureName(row);
  if (structureName) return `structure:${structureName}`;
  const rowId = asString(row.id).trim();
  return rowId ? `id:${rowId}` : `row:${index}`;
}

export function rawCandidateRichnessScore(row: Record<string, unknown>): number {
  let score = 0;
  if (readCandidateStructureName(row)) score += 100;
  if (Array.isArray(row.modifications) && row.modifications.length > 0) score += 20;
  if (firstFiniteMetric(row, ['plddt', 'binder_avg_plddt', 'ligand_mean_plddt', 'mean_plddt']) !== null) score += 8;
  if (firstFiniteMetric(row, ['pair_iptm_target_binder', 'pairIptmTargetBinder', 'pair_iptm', 'pairIptm', 'iptm']) !== null) score += 8;
  if (firstFiniteMetric(row, ['composite_score', 'score', 'fitness', 'objective']) !== null) score += 4;
  const residueSeries = parseNumberList(row.residue_plddts ?? row.residue_plddt ?? row.per_residue_plddt);
  if (residueSeries.length > 0) score += Math.min(50, residueSeries.length);
  return score;
}

export function dedupeRawCandidateRows(rows: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const byIdentity = new Map<string, { row: Record<string, unknown>; score: number; index: number }>();
  rows.forEach((row, index) => {
    const key = rawCandidateIdentity(row, index);
    const score = rawCandidateRichnessScore(row);
    const existing = byIdentity.get(key);
    if (!existing || score > existing.score) {
      byIdentity.set(key, { row, score, index: existing?.index ?? index });
    }
  });
  return Array.from(byIdentity.values())
    .sort((a, b) => a.index - b.index)
    .map((entry) => entry.row);
}

export function candidateIdentity(row: PeptideDesignCandidate): string {
  if (row.sequence) {
    if (row.generation !== null) return `seq:${row.sequence}|gen:${row.generation}`;
    return `seq:${row.sequence}`;
  }
  if (row.structureName) return `structure:${row.structureName}`;
  return row.id;
}

export function statePriority(state: RuntimeState): number {
  if (state === 'SUCCESS') return 5;
  if (state === 'RUNNING') return 4;
  if (state === 'QUEUED') return 3;
  if (state === 'UNSCORED') return 2;
  return 1;
}

export function mergeCandidateRows(rows: PeptideDesignCandidate[]): PeptideDesignCandidate[] {
  const merged = new Map<string, PeptideDesignCandidate>();
  for (const row of rows) {
    const key = candidateIdentity(row);
    const prev = merged.get(key);
    if (!prev) {
      merged.set(key, row);
      continue;
    }
    const prevHasStructure = Boolean(prev.structureText.trim());
    const rowHasStructure = Boolean(row.structureText.trim());
    const preferRowInterfaceMetric =
      (prev.interfaceMetricSource !== 'ipsae' && row.interfaceMetricSource === 'ipsae') ||
      prev.interfaceMetricSource === 'none';
    const next: PeptideDesignCandidate = {
      ...prev,
      id: prev.id,
      rank: Math.min(prev.rank, row.rank),
      sequence: prev.sequence || row.sequence,
      modifications: row.modifications.length > 0 ? row.modifications : prev.modifications,
      score:
        prev.score === null
          ? row.score
          : row.score === null
            ? prev.score
            : Math.max(prev.score, row.score),
      plddt:
        prev.plddt === null
          ? row.plddt
          : row.plddt === null
            ? prev.plddt
            : Math.max(prev.plddt, row.plddt),
      residuePlddts:
        row.residuePlddts.length > prev.residuePlddts.length ? row.residuePlddts : prev.residuePlddts,
      interfaceMetric:
        preferRowInterfaceMetric
          ? row.interfaceMetric
          : prev.interfaceMetricSource === 'ipsae' && row.interfaceMetricSource !== 'ipsae'
            ? prev.interfaceMetric
            : prev.interfaceMetric === null
              ? row.interfaceMetric
              : row.interfaceMetric === null
                ? prev.interfaceMetric
                : Math.max(prev.interfaceMetric, row.interfaceMetric),
      interfaceMetricLabel:
        preferRowInterfaceMetric
          ? row.interfaceMetricLabel
          : prev.interfaceMetricLabel,
      interfaceMetricSource:
        preferRowInterfaceMetric
          ? row.interfaceMetricSource
          : prev.interfaceMetricSource !== 'none'
            ? prev.interfaceMetricSource
            : row.interfaceMetricSource,
      iptm: prev.iptm ?? row.iptm,
      ipsae: prev.ipsae ?? row.ipsae,
      generation: prev.generation ?? row.generation,
      modelLabel: prev.modelLabel || row.modelLabel,
      structureText: rowHasStructure && !prevHasStructure ? row.structureText : prev.structureText,
      structureFormat: rowHasStructure && !prevHasStructure ? row.structureFormat : prev.structureFormat,
      structureName: rowHasStructure && !prevHasStructure ? row.structureName : prev.structureName,
      runtimeState: statePriority(row.runtimeState) > statePriority(prev.runtimeState) ? row.runtimeState : prev.runtimeState,
      source: prev.source === 'result' || row.source === 'result' ? 'result' : 'live'
    };
    merged.set(key, next);
  }
  return [...merged.values()];
}

export function parsePeptideCandidateModifications(row: Record<string, unknown>, sequenceLength: number): PeptideDesignCandidateModification[] {
  const raw =
    readObjectPath(row, 'modifications') ??
    readObjectPath(row, 'protein_modifications') ??
    readObjectPath(row, 'residue_modifications') ??
    readObjectPath(row, 'residueMods') ??
    readObjectPath(row, 'residue_mods') ??
    readObjectPath(row, 'mods') ??
    readObjectPath(row, 'result.modifications') ??
    readObjectPath(row, 'prediction.modifications') ??
    readObjectPath(row, 'metadata.modifications') ??
    readObjectPath(row, 'structure_payload.modifications');
  if (!Array.isArray(raw)) return [];
  const seen = new Set<number>();
  const rows: PeptideDesignCandidateModification[] = [];
  raw.forEach((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return;
    const record = item as Record<string, unknown>;
    const position = Math.floor(Number(record.position ?? record.residue_index ?? record.residue ?? record.pos));
    const ccd = asString(record.ccd ?? record.code ?? record.residue_name).trim().toUpperCase();
    if (!Number.isFinite(position) || position < 1 || position > sequenceLength || !ccd || seen.has(position)) return;
    seen.add(position);
    rows.push({
      position,
      ccd,
      baseResidue: asString(record.baseResidue ?? record.base_residue).trim().toUpperCase().slice(0, 1)
    });
  });
  return rows.sort((a, b) => a.position - b.position);
}

export function parseCandidateRows(
  rows: Array<Record<string, unknown>>,
  source: 'result' | 'live',
  defaultState: RuntimeState,
  defaultModelLabel: string,
  preferredLigandChainId?: string,
  preferredTargetChainId?: string
): PeptideDesignCandidate[] {
  return rows
    .map((row, index) => {
      const sequence = firstNonEmptyText(row, [
        'peptide_sequence',
        'binder_sequence',
        'candidate_sequence',
        'designed_sequence',
        'sequence'
      ])
        .replace(/\s+/g, '')
        .trim()
        .toUpperCase();
      const modifications = parsePeptideCandidateModifications(row, sequence.length);
      // pLDDT of exactly 0 is a design-backend placeholder (never a real measurement) — treat as missing.
      const plddtRaw = firstFiniteMetric(row, ['plddt', 'binder_avg_plddt', 'ligand_mean_plddt', 'mean_plddt']);
      const plddt = normalizePlddt(plddtRaw === 0 ? null : plddtRaw);
      const interfaceMetric = readPreferredInterfaceMetricForCandidate(
        row,
        preferredTargetChainId,
        preferredLigandChainId
      );
      // ipSAE is read independently so the card shows pLDDT / ipTM / ipSAE side by side.
      const ipsaeRaw = firstFiniteMetric(row, ['ligand_ipsae_max', 'ligandIpsaeMax', 'ipsae_dom', 'ipsaeDom']);
      const ipsae = normalizeIptm(
        ipsaeRaw ?? (interfaceMetric.source === 'ipsae' ? interfaceMetric.value : null)
      );
      const iptm = normalizeIptm(firstFiniteMetric(row, ['pair_iptm', 'iptm', 'ligand_iptm', 'protein_iptm']));
      const score = computePeptideCompositeScore(row, plddt, interfaceMetric.value);
      const generation = firstFiniteMetric(row, ['generation', 'iteration', 'iter']);
      const rankRaw = firstFiniteMetric(row, ['rank', 'ranking', 'order']);
      const structure = parseCandidateStructure(row);
      const residuePlddts = parseCandidateResiduePlddts(
        row,
        sequence.length,
        preferredLigandChainId
      );
      const modelLabel = parseCandidateModelLabel(row, defaultModelLabel);
      const hasStructure = Boolean(structure.structureText.trim());
      const rowState = normalizeRuntimeState(
        row.runtime_state ?? row.state ?? row.status ?? row.prediction_state ?? row.task_state
      );
      const runtimeState = source === 'result'
        ? 'SUCCESS'
        : hasStructure
          ? 'SUCCESS'
          : rowState !== 'UNSCORED'
            ? rowState
            : defaultState;
      const idBase = asString(row.id).trim() || sequence || asString(rankRaw).trim() || `${index + 1}`;
      return {
        id: `peptide-design-${source}-${idBase}-${index + 1}`,
        rank: rankRaw === null ? index + 1 : Math.max(1, Math.floor(rankRaw)),
        sequence,
        modifications,
        score,
        plddt,
        residuePlddts,
        interfaceMetric: interfaceMetric.value,
        interfaceMetricLabel: interfaceMetric.label,
        interfaceMetricSource: interfaceMetric.source,
        iptm,
        ipsae,
        generation: generation === null ? null : Math.max(0, Math.floor(generation)),
        modelLabel,
        structureText: structure.structureText,
        structureFormat: structure.structureFormat,
        structureName: structure.structureName,
        runtimeState,
        source
      } as PeptideDesignCandidate;
    })
    .filter((row) => Boolean(row.sequence || row.structureName));
}

function parseProgressPercent(value: number | null): number | null {
  if (value === null) return null;
  const normalized = value <= 1 ? value * 100 : value;
  if (!Number.isFinite(normalized)) return null;
  return Math.max(0, Math.min(100, normalized));
}

export function extractRuntimeContext(params: {
  statusInfo: Record<string, unknown>;
  snapshotConfidence: Record<string, unknown>;
  projectTaskState: string;
  fallbackProgressPercent: number;
}): PeptideRuntimeContext {
  const { statusInfo, snapshotConfidence, projectTaskState, fallbackProgressPercent } = params;
  const statusPayload = asRecord(statusInfo);
  const statusProgress = asRecord(statusPayload.progress);
  const statusPeptide = asRecord(statusPayload.peptide_design);
  const statusPeptideProgress = asRecord(statusPeptide.progress);
  const confidencePeptide = asRecord(snapshotConfidence.peptide_design);
  const confidencePeptideProgress = asRecord(confidencePeptide.progress);

  const payloads = [
    statusPayload,
    statusProgress,
    statusPeptide,
    statusPeptideProgress,
    confidencePeptide,
    confidencePeptideProgress
  ];

  const currentStatus = readFirstTextFromPaths(payloads, [
    'current_status',
    'status_stage',
    'stage',
    'progress.current_status'
  ]);

  const statusMessage = readFirstTextFromPaths(payloads, [
    'status_message',
    'message',
    'status',
    'progress.status_message'
  ]);

  const currentGeneration = readFirstFiniteFromPaths(payloads, [
    'current_generation',
    'generation',
    'iter',
    'progress.current_generation'
  ]);

  const totalGenerations = readFirstFiniteFromPaths(payloads, [
    'total_generations',
    'generations',
    'max_generation',
    'progress.total_generations'
  ]);

  const bestScore = readFirstFiniteFromPaths(payloads, ['best_score', 'current_best_score', 'score']);

  const completedTasks = readFirstFiniteFromPaths(payloads, ['completed_tasks', 'done_tasks', 'finished_tasks']);
  const pendingTasks = readFirstFiniteFromPaths(payloads, ['pending_tasks']);
  const totalTasks =
    readFirstFiniteFromPaths(payloads, ['total_tasks', 'task_total']) ??
    (completedTasks !== null && pendingTasks !== null ? completedTasks + pendingTasks : null);
  const generationCompletedTasks = readFirstFiniteFromPaths(payloads, ['generation_completed_tasks']);
  const generationRunningTasks = readFirstFiniteFromPaths(payloads, ['generation_running_tasks']);
  const generationQueuedTasks = readFirstFiniteFromPaths(payloads, ['generation_queued_tasks']);
  const generationTotalTasks = readFirstFiniteFromPaths(payloads, ['generation_total_tasks']);
  const elapsedSeconds = readFirstFiniteFromPaths(payloads, ['elapsed_seconds']);
  const estimatedRemainingSeconds = readFirstFiniteFromPaths(payloads, ['estimated_remaining_seconds']);
  const estimatedCompletionTime = readFirstTextFromPaths(payloads, ['estimated_completion_time']);
  const candidatesEvaluated = readFirstFiniteFromPaths(payloads, ['candidates_evaluated']);
  const adaptiveMutationRate = readFirstFiniteFromPaths(payloads, ['adaptive_mutation_rate']);
  const stagnantGenerations = readFirstFiniteFromPaths(payloads, ['stagnant_generations']);

  let progress = parseProgressPercent(
    readFirstFiniteFromPaths(payloads, [
      'estimated_progress',
      'progress_percent',
      'overall_progress',
      'progress_info.overall_progress'
    ])
  );
  if (progress === null && currentGeneration !== null && totalGenerations !== null && totalGenerations > 0) {
    progress = parseProgressPercent(currentGeneration / totalGenerations);
  }
  if (progress === null && totalTasks !== null && totalTasks > 0 && completedTasks !== null) {
    progress = parseProgressPercent(completedTasks / totalTasks);
  }
  if (progress === null && Number.isFinite(fallbackProgressPercent) && fallbackProgressPercent > 0) {
    progress = parseProgressPercent(fallbackProgressPercent);
  }

  const taskState = normalizeRuntimeState(projectTaskState);
  const liveCandidateRows = shouldUseLivePeptideRows(taskState)
    ? readFirstRecordArrayFromPaths(
        [statusPayload, statusProgress, statusPeptide, statusPeptideProgress],
        [
          'progress.current_best_sequences',
          'progress.best_sequences',
          'current_best_sequences',
          'best_sequences',
          'current_candidates',
          'candidates'
        ]
      )
    : EMPTY_RECORD_ROWS;

  return {
    state: taskState,
    currentStatus,
    statusMessage,
    currentGeneration: currentGeneration === null ? null : Math.max(0, Math.floor(currentGeneration)),
    totalGenerations: totalGenerations === null ? null : Math.max(0, Math.floor(totalGenerations)),
    bestScore,
    progressPercent: progress,
    completedTasks: completedTasks === null ? null : Math.max(0, Math.floor(completedTasks)),
    pendingTasks: pendingTasks === null ? null : Math.max(0, Math.floor(pendingTasks)),
    totalTasks: totalTasks === null ? null : Math.max(0, Math.floor(totalTasks)),
    generationCompletedTasks: generationCompletedTasks === null ? null : Math.max(0, Math.floor(generationCompletedTasks)),
    generationRunningTasks: generationRunningTasks === null ? null : Math.max(0, Math.floor(generationRunningTasks)),
    generationQueuedTasks: generationQueuedTasks === null ? null : Math.max(0, Math.floor(generationQueuedTasks)),
    generationTotalTasks: generationTotalTasks === null ? null : Math.max(0, Math.floor(generationTotalTasks)),
    elapsedSeconds,
    estimatedRemainingSeconds,
    estimatedCompletionTime,
    candidatesEvaluated: candidatesEvaluated === null ? null : Math.max(0, Math.floor(candidatesEvaluated)),
    adaptiveMutationRate,
    stagnantGenerations: stagnantGenerations === null ? null : Math.max(0, Math.floor(stagnantGenerations)),
    liveCandidateRows
  };
}

export function shouldUseLivePeptideRows(state: RuntimeState): boolean {
  return state === 'RUNNING' || state === 'QUEUED';
}

/**
 * Header-facing staged progress line, e.g. "Generation 3/6 · 45/128 candidates · ~35m left".
 * Returns null when the payload carries no stage information (non-peptide workflows).
 */
export function buildStagedProgressText(statusInfo: Record<string, unknown>): string | null {
  const runtime = extractRuntimeContext({
    statusInfo,
    snapshotConfidence: {},
    projectTaskState: 'RUNNING',
    fallbackProgressPercent: Number.NaN
  });
  const parts: string[] = [];
  if (runtime.currentGeneration !== null && runtime.totalGenerations !== null && runtime.totalGenerations > 0) {
    parts.push(`Generation ${runtime.currentGeneration}/${runtime.totalGenerations}`);
  }
  if (runtime.completedTasks !== null && runtime.totalTasks !== null && runtime.totalTasks > 0) {
    parts.push(`${runtime.completedTasks}/${runtime.totalTasks} candidates`);
  }
  if (
    runtime.estimatedRemainingSeconds !== null &&
    Number.isFinite(runtime.estimatedRemainingSeconds) &&
    runtime.estimatedRemainingSeconds > 0
  ) {
    const minutes = Math.round(runtime.estimatedRemainingSeconds / 60);
    parts.push(minutes >= 1 ? `~${minutes}m left` : '<1m left');
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}

export function buildRawCandidateRowsSignature(rows: Array<Record<string, unknown>>): string {
  if (rows.length === 0) return '0';
  return rows
    .map((row, index) => {
      const identity = rawCandidateIdentity(row, index);
      const rank = firstFiniteMetric(row, ['rank', 'ranking', 'order']);
      const score = firstFiniteMetric(row, ['composite_score', 'score', 'fitness', 'objective']);
      const plddt = firstFiniteMetric(row, ['plddt', 'binder_avg_plddt', 'ligand_mean_plddt', 'mean_plddt']);
      const structureName = readCandidateStructureName(row);
      return [identity, rank ?? '', score ?? '', plddt ?? '', structureName].join(':');
    })
    .join('|');
}

export function buildCandidateRows(
  finalizedRows: Array<Record<string, unknown>>,
  liveRows: Array<Record<string, unknown>>,
  liveDefaultState: RuntimeState,
  runtimeModelLabel: string,
  selectedLigandChainId?: string,
  selectedTargetChainId?: string
): PeptideDesignCandidate[] {
  const parsed: PeptideDesignCandidate[] = [
    ...parseCandidateRows(
      finalizedRows,
      'result',
      'SUCCESS',
      runtimeModelLabel,
      selectedLigandChainId,
      selectedTargetChainId
    )
  ];
  if (liveRows.length > 0) {
    parsed.push(
      ...parseCandidateRows(
        liveRows,
        'live',
        liveDefaultState,
        runtimeModelLabel,
        selectedLigandChainId,
        selectedTargetChainId
      )
    );
  }
  return mergeCandidateRows(parsed)
    .sort((a, b) => {
      if (a.score !== null && b.score !== null && a.score !== b.score) return b.score - a.score;
      if (a.plddt !== null && b.plddt !== null && a.plddt !== b.plddt) return b.plddt - a.plddt;
      if (a.generation !== null && b.generation !== null && a.generation !== b.generation) return b.generation - a.generation;
      return a.rank - b.rank;
    })
    .map((item, index) => ({ ...item, rank: index + 1 }));
}

export function formatScore(value: number | null): string {
  if (value === null) return '-';
  return value.toFixed(3);
}

export function formatPlddt(value: number | null): string {
  if (value === null) return '-';
  return `${value.toFixed(1)}`;
}

export function formatInterfaceMetric(value: number | null): string {
  if (value === null) return '-';
  return value.toFixed(3);
}

export function toneForPlddtValue(value: number | null): 'excellent' | 'good' | 'medium' | 'low' | 'neutral' {
  if (value === null) return 'neutral';
  if (value >= 90) return 'excellent';
  if (value >= 70) return 'good';
  if (value >= 50) return 'medium';
  return 'low';
}

export function confidenceTone(value: number | null): ConfidenceTone {
  if (value === null || !Number.isFinite(value)) return 'na';
  if (value >= 90) return 'vhigh';
  if (value >= 70) return 'high';
  if (value >= 50) return 'low';
  return 'vlow';
}

export function scoreConfidencePercent(value: number | null, minScore: number | null, maxScore: number | null): number | null {
  if (value === null || minScore === null || maxScore === null) return null;
  if (!Number.isFinite(value) || !Number.isFinite(minScore) || !Number.isFinite(maxScore)) return null;
  const span = maxScore - minScore;
  if (span <= 1e-9) return 75;
  const normalized = ((value - minScore) / span) * 100;
  return Math.max(0, Math.min(100, normalized));
}

export function buildPeptideLigandViewTokens(
  sequence: string,
  modifications: PeptideDesignCandidateModification[] = []
): Array<{ residue: string; displayResidue: string; modifiedLabel: string }> {
  const normalized = sequence.trim().toUpperCase().replace(/[^A-Z]/g, '');
  if (!normalized) return [];
  const modificationByPosition = new Map<number, string>();
  for (const mod of modifications) {
    const position = Math.floor(Number(mod.position));
    const ccd = asString(mod.ccd).trim().toUpperCase();
    if (!Number.isFinite(position) || position < 1 || !ccd) continue;
    modificationByPosition.set(position, ccd);
  }
  return normalized.split('').map((residue, index) => {
    const modifiedLabel = modificationByPosition.get(index + 1) || '';
    return {
      residue,
      displayResidue: modifiedLabel || residue,
      modifiedLabel
    };
  });
}


