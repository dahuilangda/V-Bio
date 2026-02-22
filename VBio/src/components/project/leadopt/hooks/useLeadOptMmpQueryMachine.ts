import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  clusterLeadOptimizationMmp,
  downloadResultBlob,
  enumerateLeadOptimizationMmp,
  fetchLeadOptimizationMmpEvidence,
  fetchLeadOptimizationMmpQueryResult,
  getTaskStatus,
  type LeadOptMmpEvidenceResponse,
  parseResultBundle,
  predictLeadOptimizationCandidate,
  queryLeadOptimizationMmp
} from '../../../../api/backendApi';
import { readFirstFiniteMetric, readObjectPath, readPairIptmForChains } from '../../../../pages/projectDetail/projectMetrics';
import type { ParsedResultBundle } from '../../../../types/models';
import type {
  LeadOptDirection as Direction,
  LeadOptGroupedByEnvironment as GroupedByEnvironmentMode,
  LeadOptQueryMode as QueryMode,
  LeadOptQueryProperty as QueryProperty,
  LeadOptVariableMode as VariableMode
} from './useLeadOptMmpQueryForm';

interface VariableItemInput {
  query: string;
  mode: VariableMode;
  fragment_id?: string;
  atom_indices?: number[];
}

interface RunMmpQueryInput {
  canQuery: boolean;
  effectiveLigandSmiles: string;
  variableItems: VariableItemInput[];
  constantQuery: string;
  direction: Direction;
  queryProperty: QueryProperty;
  mmpDatabaseId: string;
  queryMode: QueryMode;
  groupedByEnvironment: GroupedByEnvironmentMode;
  minPairs: number;
  envRadius: number;
  onTaskQueued?: (payload: { taskId: string; requestPayload: Record<string, unknown> }) => void | Promise<void>;
  onTaskCompleted?: (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => void | Promise<void>;
  onTaskFailed?: (payload: { taskId: string; error: string }) => void | Promise<void>;
}

interface RunMmpQueryResult {
  queryId: string;
  transformCount: number;
  candidateCount: number;
}

export interface LeadOptMmpPersistedSnapshot {
  query_result?: Record<string, unknown>;
  enumerated_candidates?: Array<Record<string, unknown>>;
  prediction_by_smiles?: Record<string, LeadOptPredictionRecord>;
  reference_prediction_by_backend?: Record<string, LeadOptPredictionRecord>;
  ui_state?: Record<string, unknown>;
  selection?: Record<string, unknown>;
  target_chain?: string;
  ligand_chain?: string;
}

interface UseLeadOptMmpQueryMachineParams {
  proteinSequence: string;
  targetChain: string;
  ligandChain: string;
  backend: string;
  onError: (message: string | null) => void;
  onPredictionQueued?: (payload: { taskId: string; backend: string; candidateSmiles: string }) => void | Promise<void>;
  onPredictionStateChange?: (payload: {
    records: Record<string, LeadOptPredictionRecord>;
    referenceRecords: Record<string, LeadOptPredictionRecord>;
    summary: {
      total: number;
      queued: number;
      running: number;
      success: number;
      failure: number;
      latestTaskId: string;
    };
  }) => void | Promise<void>;
}

type PredictionState = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE';
type ClusterGroupBy = 'to' | 'from' | 'rule_env_radius';
const RESULT_HYDRATION_RETRY_BASE_MS = 1200;
const RESULT_HYDRATION_RETRY_MAX_MS = 10000;

export interface LeadOptPredictionRecord {
  taskId: string;
  state: PredictionState;
  backend: string;
  pairIptm: number | null;
  pairPae: number | null;
  pairIptmResolved?: boolean;
  ligandPlddt: number | null;
  ligandAtomPlddts: number[];
  structureText?: string;
  structureFormat?: 'cif' | 'pdb';
  structureName?: string;
  error: string;
  updatedAt: number;
}

function normalizePredictionRecord(value: unknown): LeadOptPredictionRecord | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const taskId = readText(raw.taskId || raw.task_id).trim();
  if (!taskId) return null;
  const state = readText(raw.state).toUpperCase();
  const normalizedState: PredictionState =
    state === 'QUEUED' || state === 'RUNNING' || state === 'SUCCESS' || state === 'FAILURE' ? state : 'QUEUED';
  const structureText = readText(raw.structureText ?? raw.structure_text);
  const structureFormat = readText(raw.structureFormat ?? raw.structure_format).toLowerCase() === 'pdb' ? 'pdb' : 'cif';
  const structureName = readText(raw.structureName ?? raw.structure_name);
  const pairIptm = normalizeIptm(raw.pairIptm ?? raw.pair_iptm);
  const pairPae = normalizePae(raw.pairPae ?? raw.pair_pae);
  const ligandPlddtRaw = Number(raw.ligandPlddt ?? raw.ligand_plddt);
  const ligandPlddt = Number.isFinite(ligandPlddtRaw) ? normalizePlddtValue(ligandPlddtRaw) : null;
  const ligandAtomPlddts = normalizePlddtArray(raw.ligandAtomPlddts ?? raw.ligand_atom_plddts);
  const hasResolvedMetrics = pairIptm !== null || pairPae !== null || ligandPlddt !== null || ligandAtomPlddts.length > 0;
  const pairIptmResolvedRaw = raw.pairIptmResolved ?? raw.pair_iptm_resolved;
  return {
    taskId,
    state: normalizedState,
    backend: readText(raw.backend).trim().toLowerCase() || 'boltz',
    pairIptm,
    pairPae,
    pairIptmResolved: pairIptmResolvedRaw === true && hasResolvedMetrics ? true : hasResolvedMetrics,
    ligandPlddt,
    ligandAtomPlddts,
    ...(structureText.trim()
      ? {
          structureText,
          structureFormat,
          structureName
        }
      : {}),
    error: readText(raw.error),
    updatedAt: Number.isFinite(Number(raw.updatedAt ?? raw.updated_at))
      ? Number(raw.updatedAt ?? raw.updated_at)
      : Date.now()
  };
}

function normalizePredictionMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [smiles, rawRecord] of Object.entries(value as Record<string, unknown>)) {
    const key = String(smiles || '').trim();
    if (!key) continue;
    const record = normalizePredictionRecord(rawRecord);
    if (!record) continue;
    out[key] = record;
  }
  return out;
}

function mapPredictionRuntimeState(raw: unknown): PredictionState | null {
  const token = String(raw || '').trim().toUpperCase();
  if (!token) return null;
  if (token === 'SUCCESS') return 'SUCCESS';
  if (token === 'FAILURE' || token === 'REVOKED') return 'FAILURE';
  if (token === 'PENDING' || token === 'RECEIVED' || token === 'RETRY') return 'QUEUED';
  if (token === 'STARTED' || token === 'RUNNING' || token === 'PROGRESS') return 'RUNNING';
  return null;
}

function normalizeBackendKey(value: unknown): string {
  const token = String(value || '').trim().toLowerCase();
  return token === 'alphafold3' || token === 'protenix' ? token : 'boltz';
}

function summarizePredictionRecords(records: Record<string, LeadOptPredictionRecord>) {
  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  let latestTaskId = '';
  let latestTs = -1;
  for (const record of Object.values(records)) {
    const state = String(record.state || '').toUpperCase();
    if (state === 'QUEUED') queued += 1;
    else if (state === 'RUNNING') running += 1;
    else if (state === 'SUCCESS') success += 1;
    else if (state === 'FAILURE') failure += 1;
    const ts = Number(record.updatedAt || 0);
    if (Number.isFinite(ts) && ts > latestTs) {
      latestTs = ts;
      latestTaskId = String(record.taskId || '').trim();
    }
  }
  return {
    total: Object.keys(records).length,
    queued,
    running,
    success,
    failure,
    latestTaskId
  };
}

function buildQueuedPredictionRecord(taskId: string, backend: string): LeadOptPredictionRecord {
  return {
    taskId,
    state: 'QUEUED',
    backend: String(backend || '').trim().toLowerCase() || 'boltz',
    pairIptm: null,
    pairPae: null,
    pairIptmResolved: false,
    ligandPlddt: null,
    ligandAtomPlddts: [],
    error: '',
    updatedAt: Date.now()
  };
}

function hasResolvedPredictionMetrics(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  const pairIptm = typeof record.pairIptm === 'number' && Number.isFinite(record.pairIptm);
  const pairPae = typeof record.pairPae === 'number' && Number.isFinite(record.pairPae);
  const ligandPlddt = typeof record.ligandPlddt === 'number' && Number.isFinite(record.ligandPlddt);
  const ligandAtomPlddts = Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
  return record.pairIptmResolved === true && (pairIptm || pairPae || ligandPlddt || ligandAtomPlddts);
}

function shouldHydratePredictionRecord(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (String(record.state || '').toUpperCase() !== 'SUCCESS') return false;
  const taskId = String(record.taskId || '').trim();
  if (!taskId || taskId.startsWith('local:')) return false;
  return !hasResolvedPredictionMetrics(record);
}

function isResultArchivePendingError(error: unknown): boolean {
  const message = String(error instanceof Error ? error.message : error || '').toLowerCase();
  if (!message) return false;
  return (
    (message.includes('failed to download result (404)') && message.includes('/results/')) ||
    message.includes('result file information not found in task metadata or on disk') ||
    message.includes('result file not found on disk') ||
    message.includes('task has not completed yet')
  );
}

function computeHydrationRetryDelayMs(attempt: number): number {
  const safeAttempt = Math.max(0, Math.min(6, attempt));
  const delay = RESULT_HYDRATION_RETRY_BASE_MS * (2 ** safeAttempt);
  return Math.min(RESULT_HYDRATION_RETRY_MAX_MS, delay);
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function readNumber(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function readBoolean(value: unknown, fallback = false): boolean {
  if (value === true) return true;
  if (value === false) return false;
  const token = String(value || '').trim().toLowerCase();
  if (!token) return fallback;
  if (token === '1' || token === 'true' || token === 'yes' || token === 'on') return true;
  if (token === '0' || token === 'false' || token === 'no' || token === 'off') return false;
  return fallback;
}

function formatMetric(value: unknown, digits = 2): string {
  const numeric = readNumber(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric.toFixed(digits);
}

function sortScore(...values: unknown[]): number[] {
  return values.map((value) => readNumber(value));
}

function asRecord(value: unknown): Record<string, unknown> {
  return (value && typeof value === 'object' ? (value as Record<string, unknown>) : {});
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({ ...(item as Record<string, unknown>) }));
}

function normalizePlddtValue(value: number): number {
  if (!Number.isFinite(value)) return 0;
  const normalized = value >= 0 && value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, normalized));
}

function normalizePlddtArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item))
    .map((item) => normalizePlddtValue(item));
}

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function findLigandAtomPlddts(confidence: Record<string, unknown>, ligandChain: string): number[] {
  const preferred = String(ligandChain || '').trim();
  const byChainRaw = confidence.ligand_atom_plddts_by_chain;
  if (byChainRaw && typeof byChainRaw === 'object' && !Array.isArray(byChainRaw)) {
    const byChain = byChainRaw as Record<string, unknown>;
    if (preferred) {
      const direct = normalizePlddtArray(byChain[preferred] ?? byChain[preferred.toUpperCase()] ?? byChain[preferred.toLowerCase()]);
      if (direct.length > 0) return direct;
    }
    const entries = Object.values(byChain).map((item) => normalizePlddtArray(item)).filter((item) => item.length > 0);
    if (entries.length > 0) {
      entries.sort((a, b) => b.length - a.length);
      return entries[0];
    }
  }
  const direct = normalizePlddtArray(confidence.ligand_atom_plddts ?? confidence.ligand_atom_plddt);
  if (direct.length > 0) return direct;
  return [];
}

function normalizeIptm(value: unknown): number | null {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  if (numeric >= 0 && numeric <= 1) return numeric;
  if (numeric > 1 && numeric <= 100) return numeric / 100;
  return null;
}

function normalizePae(value: unknown): number | null {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  if (numeric < 0) return null;
  return numeric;
}

function uniqueChainHints(...values: unknown[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    const token = String(value || '').trim();
    if (!token) continue;
    const normalized = token.toUpperCase();
    if (seen.has(normalized)) continue;
    seen.add(normalized);
    out.push(token);
  }
  return out;
}

function toChainList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => String(item || '').trim())
    .filter(Boolean);
}

function chainVariants(chain: string): string[] {
  const token = String(chain || '').trim();
  if (!token) return [];
  const out = [token];
  const upper = token.toUpperCase();
  const lower = token.toLowerCase();
  if (!out.includes(upper)) out.push(upper);
  if (!out.includes(lower)) out.push(lower);
  return out;
}

function chainTokenEquals(left: string, right: string): boolean {
  return String(left || '').trim().toUpperCase() === String(right || '').trim().toUpperCase();
}

function isNumericToken(value: string): boolean {
  return /^\d+$/.test(String(value || '').trim());
}

function readPairValueFromNestedMap(
  mapValue: unknown,
  chainA: string,
  chainB: string
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const rowA = byChain[chainA];
  const rowB = byChain[chainB];
  const v1 =
    rowA && typeof rowA === 'object' && !Array.isArray(rowA)
      ? Number((rowA as Record<string, unknown>)[chainB])
      : Number.NaN;
  const v2 =
    rowB && typeof rowB === 'object' && !Array.isArray(rowB)
      ? Number((rowB as Record<string, unknown>)[chainA])
      : Number.NaN;
  const c1 = Number.isFinite(v1) ? v1 : null;
  const c2 = Number.isFinite(v2) ? v2 : null;
  if (c1 === null && c2 === null) return null;
  return Math.max(c1 ?? Number.NEGATIVE_INFINITY, c2 ?? Number.NEGATIVE_INFINITY);
}

function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex((item) => chainTokenEquals(item, chainA));
  const idxB = chainOrderHints.findIndex((item) => chainTokenEquals(item, chainB));
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const mapped = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (mapped !== null) return mapped;
  }

  if (keys.length === 2) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const inferredPairValue = readPairValueFromNestedMap(byChain, first, second);
    if (inferredPairValue !== null) return inferredPairValue;
  }
  return null;
}

function readPairIptmForChainsFlexible(
  confidence: Record<string, unknown>,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  const direct = readPairIptmForChains(confidence, chainA, chainB, chainOrderHints);
  if (direct !== null) return normalizeIptm(direct);
  const pairMapRaw = readObjectPath(confidence, 'pair_chains_iptm');
  const mapped = readPairValueFromNumericMap(pairMapRaw, chainA, chainB, chainOrderHints);
  return mapped !== null ? normalizeIptm(mapped) : null;
}

function readPairPaeForChains(
  confidence: Record<string, unknown>,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  if (!chainA || !chainB || chainA === chainB) return null;

  const pairMapRaw = readObjectPath(confidence, 'pair_chains_pae');
  if (pairMapRaw && typeof pairMapRaw === 'object' && !Array.isArray(pairMapRaw)) {
    const direct = readPairValueFromNestedMap(pairMapRaw, chainA, chainB);
    if (direct !== null) return normalizePae(direct);
    const numericMapped = readPairValueFromNumericMap(pairMapRaw, chainA, chainB, chainOrderHints);
    if (numericMapped !== null) return normalizePae(numericMapped);
  }

  const matrixCandidates = ['chain_pair_pae', 'chain_pair_gpde', 'chain_pair_pde'];
  for (const path of matrixCandidates) {
    const matrix = readObjectPath(confidence, path);
    if (!Array.isArray(matrix)) continue;
    const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
    const chainIds =
      Array.isArray(chainIdsRaw) && chainIdsRaw.every((value) => typeof value === 'string')
        ? (chainIdsRaw as string[])
        : chainOrderHints;
    const i = chainIds.findIndex((value) => value === chainA);
    const j = chainIds.findIndex((value) => value === chainB);
    if (i < 0 || j < 0) continue;
    const rowI = matrix[i];
    const rowJ = matrix[j];
    const m1 = Array.isArray(rowI) ? normalizePae(rowI[j]) : null;
    const m2 = Array.isArray(rowJ) ? normalizePae(rowJ[i]) : null;
    if (m1 !== null || m2 !== null) return Math.min(m1 ?? Number.POSITIVE_INFINITY, m2 ?? Number.POSITIVE_INFINITY);
  }

  const scalar = readFirstFiniteMetric(confidence, ['complex_pde', 'complex_pae', 'gpde', 'pae']);
  if (scalar !== null) return normalizePae(scalar);
  return null;
}

function findPairIptm(confidence: Record<string, unknown>, targetChain: string, ligandChain: string): number | null {
  const chainIds = toChainList(confidence.chain_ids);
  const ligandHints = uniqueChainHints(
    ligandChain,
    confidence.model_ligand_chain_id,
    confidence.requested_ligand_chain_id,
    confidence.ligand_chain_id,
    ...toChainList(confidence.ligand_chain_ids)
  );
  const targetHints = uniqueChainHints(
    targetChain,
    confidence.target_chain_id,
    confidence.requested_target_chain_id,
    confidence.protein_chain_id,
    ...toChainList(confidence.target_chain_ids),
    ...chainIds.filter((candidate) => !ligandHints.some((hint) => hint.toUpperCase() === candidate.toUpperCase()))
  );
  if (targetHints.length === 0 || ligandHints.length === 0) return null;
  const chainOrderHints = Array.from(new Set([...chainIds, ...targetHints, ...ligandHints]));

  for (const target of targetHints) {
    for (const ligand of ligandHints) {
      if (target.toUpperCase() === ligand.toUpperCase()) continue;
      for (const targetCandidate of chainVariants(target)) {
        for (const ligandCandidate of chainVariants(ligand)) {
          const pairValue = readPairIptmForChainsFlexible(
            confidence,
            targetCandidate,
            ligandCandidate,
            chainOrderHints
          );
          if (pairValue !== null) return pairValue;
        }
      }
    }
  }
  const scalar = readFirstFiniteMetric(confidence, ['pair_iptm', 'iptm', 'ligand_iptm', 'protein_iptm']);
  return scalar !== null ? normalizeIptm(scalar) : null;
}

function findPairPae(confidence: Record<string, unknown>, targetChain: string, ligandChain: string): number | null {
  const chainIds = toChainList(confidence.chain_ids);
  const ligandHints = uniqueChainHints(
    ligandChain,
    confidence.model_ligand_chain_id,
    confidence.requested_ligand_chain_id,
    confidence.ligand_chain_id,
    ...toChainList(confidence.ligand_chain_ids)
  );
  const targetHints = uniqueChainHints(
    targetChain,
    confidence.target_chain_id,
    confidence.requested_target_chain_id,
    confidence.protein_chain_id,
    ...toChainList(confidence.target_chain_ids),
    ...chainIds.filter((candidate) => !ligandHints.some((hint) => hint.toUpperCase() === candidate.toUpperCase()))
  );
  if (targetHints.length === 0 || ligandHints.length === 0) return null;
  const chainOrderHints = Array.from(new Set([...chainIds, ...targetHints, ...ligandHints]));

  for (const target of targetHints) {
    for (const ligand of ligandHints) {
      if (target.toUpperCase() === ligand.toUpperCase()) continue;
      for (const targetCandidate of chainVariants(target)) {
        for (const ligandCandidate of chainVariants(ligand)) {
          const pairValue = readPairPaeForChains(confidence, targetCandidate, ligandCandidate, chainOrderHints);
          if (pairValue !== null) return pairValue;
        }
      }
    }
  }
  const scalar = readFirstFiniteMetric(confidence, ['pair_pae', 'complex_pde', 'complex_pae', 'gpde', 'pae']);
  return scalar !== null ? normalizePae(scalar) : null;
}

function extractPredictionResultPayload(
  parsed: ParsedResultBundle | null,
  targetChain: string,
  ligandChain: string
): {
  pairIptm: number | null;
  pairPae: number | null;
  ligandPlddt: number | null;
  ligandAtomPlddts: number[];
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  structureName: string;
} {
  const confidence = asRecord(parsed?.confidence);
  const affinity = asRecord(parsed?.affinity);
  const pairIptm = findPairIptm(confidence, targetChain, ligandChain) ?? findPairIptm(affinity, targetChain, ligandChain);
  const pairPae = findPairPae(confidence, targetChain, ligandChain) ?? findPairPae(affinity, targetChain, ligandChain);
  const ligandAtomPlddts = findLigandAtomPlddts(confidence, ligandChain);
  const structureText = readText(parsed?.structureText);
  return {
    pairIptm,
    pairPae,
    ligandPlddt: mean(ligandAtomPlddts),
    ligandAtomPlddts,
    structureText,
    structureFormat: readText(parsed?.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(parsed?.structureName)
  };
}

export function useLeadOptMmpQueryMachine({
  proteinSequence,
  targetChain,
  ligandChain,
  backend,
  onError,
  onPredictionQueued,
  onPredictionStateChange
}: UseLeadOptMmpQueryMachineParams) {
  const [loading, setLoading] = useState(false);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [queryNotice, setQueryNotice] = useState('');

  const [queryId, setQueryId] = useState('');
  const [activeQueryMode, setActiveQueryMode] = useState<QueryMode>('one-to-many');
  const [clusterGroupBy, setClusterGroupBy] = useState<ClusterGroupBy>('to');
  const [queryMinPairs, setQueryMinPairs] = useState(1);
  const [globalCount, setGlobalCount] = useState(0);
  const [queryStats, setQueryStats] = useState<Record<string, unknown>>({});

  const [transforms, setTransforms] = useState<Array<Record<string, unknown>>>([]);
  const [clusters, setClusters] = useState<Array<Record<string, unknown>>>([]);
  const [activeTransformId, setActiveTransformId] = useState('');
  const [activeEvidence, setActiveEvidence] = useState<LeadOptMmpEvidenceResponse | null>(null);

  const [selectedTransformIds, setSelectedTransformIds] = useState<string[]>([]);
  const [selectedClusterIds, setSelectedClusterIds] = useState<string[]>([]);
  const [enumeratedCandidates, setEnumeratedCandidates] = useState<Array<Record<string, unknown>>>([]);
  const [lastPredictionTaskId, setLastPredictionTaskId] = useState('');
  const [lastMmpTaskId, setLastMmpTaskId] = useState('');
  const [mmpRunVersion, setMmpRunVersion] = useState(0);
  const [predictionBySmiles, setPredictionBySmiles] = useState<Record<string, LeadOptPredictionRecord>>({});
  const [referencePredictionByBackend, setReferencePredictionByBackend] = useState<Record<string, LeadOptPredictionRecord>>({});

  const mmpQueryInFlightRef = useRef(false);
  const lastMmpQueryAtRef = useRef(0);
  const queryResultCacheRef = useRef<Map<string, Record<string, unknown>>>(new Map());
  const predictionHydrationRetryCountRef = useRef<Record<string, number>>({});
  const predictionHydrationRetryTimerRef = useRef<Record<string, number>>({});
  const predictionHydrationInFlightRef = useRef<Set<string>>(new Set());
  const referenceHydrationRetryCountRef = useRef<Record<string, number>>({});
  const referenceHydrationRetryTimerRef = useRef<Record<string, number>>({});
  const referenceHydrationInFlightRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    return () => {
      for (const timerId of Object.values(predictionHydrationRetryTimerRef.current)) {
        window.clearTimeout(timerId);
      }
      for (const timerId of Object.values(referenceHydrationRetryTimerRef.current)) {
        window.clearTimeout(timerId);
      }
      predictionHydrationRetryTimerRef.current = {};
      referenceHydrationRetryTimerRef.current = {};
      predictionHydrationRetryCountRef.current = {};
      referenceHydrationRetryCountRef.current = {};
      predictionHydrationInFlightRef.current.clear();
      referenceHydrationInFlightRef.current.clear();
    };
  }, []);

  const hasSelection = selectedTransformIds.length > 0;

  const clearSelections = useCallback(() => {
    setSelectedTransformIds([]);
    setSelectedClusterIds([]);
  }, []);

  useEffect(() => {
    const pendingEntries = Object.entries(predictionBySmiles)
      .filter(([, record]) => {
        const state = String(record.state || '').toUpperCase();
        if (state !== 'QUEUED' && state !== 'RUNNING') return false;
        const taskId = readText(record.taskId).trim();
        return taskId.length > 0 && !taskId.startsWith('local:');
      })
      .sort((a, b) => Number(a[1]?.updatedAt || 0) - Number(b[1]?.updatedAt || 0))
      .slice(0, 8);
    if (pendingEntries.length === 0) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        for (const [smiles, record] of pendingEntries) {
          if (cancelled) return;
          try {
            const status = await getTaskStatus(record.taskId);
            if (cancelled) return;
            const runtimeState = mapPredictionRuntimeState(status.state);
            if (runtimeState === 'SUCCESS') {
              const retryTimer = predictionHydrationRetryTimerRef.current[smiles];
              if (retryTimer) {
                window.clearTimeout(retryTimer);
                delete predictionHydrationRetryTimerRef.current[smiles];
              }
              delete predictionHydrationRetryCountRef.current[smiles];
              setPredictionBySmiles((prev) => ({
                ...prev,
                [smiles]: {
                  ...(prev[smiles] || record),
                  state: 'SUCCESS',
                  error: '',
                  updatedAt: Date.now()
                }
              }));
              continue;
            }
            if (runtimeState === 'FAILURE') {
              const retryTimer = predictionHydrationRetryTimerRef.current[smiles];
              if (retryTimer) {
                window.clearTimeout(retryTimer);
                delete predictionHydrationRetryTimerRef.current[smiles];
              }
              delete predictionHydrationRetryCountRef.current[smiles];
              const errorText = readText(asRecord(status.info).error || asRecord(status.info).message || status.state);
              setPredictionBySmiles((prev) => ({
                ...prev,
                [smiles]: {
                  ...(prev[smiles] || record),
                  state: 'FAILURE',
                  error: errorText || 'Prediction failed.',
                  updatedAt: Date.now()
                }
              }));
              continue;
            }
            if (runtimeState !== 'QUEUED' && runtimeState !== 'RUNNING') continue;
            setPredictionBySmiles((prev) => {
              const current = prev[smiles] || record;
              if (String(current.state || '').toUpperCase() === runtimeState) {
                return {
                  ...prev,
                  [smiles]: {
                    ...current,
                    updatedAt: Date.now()
                  }
                };
              }
              return {
                ...prev,
                [smiles]: {
                  ...current,
                  state: runtimeState,
                  updatedAt: Date.now()
                }
              };
            });
          } catch {
            // On transient status errors, still bump timestamp so polling can rotate
            // through other queued/running records instead of starving after the first slice.
            setPredictionBySmiles((prev) => {
              const current = prev[smiles] || record;
              if (!current) return prev;
              return {
                ...prev,
                [smiles]: {
                  ...current,
                  updatedAt: Date.now()
                }
              };
            });
          }
        }
      })();
    }, 2200);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [ligandChain, predictionBySmiles, targetChain]);

  useEffect(() => {
    const pendingEntries = Object.entries(referencePredictionByBackend)
      .filter(([, record]) => {
        const state = String(record.state || '').toUpperCase();
        if (state !== 'QUEUED' && state !== 'RUNNING') return false;
        const taskId = readText(record.taskId).trim();
        return taskId.length > 0 && !taskId.startsWith('local:');
      })
      .sort((a, b) => Number(a[1]?.updatedAt || 0) - Number(b[1]?.updatedAt || 0))
      .slice(0, 4);
    if (pendingEntries.length === 0) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        for (const [backendKey, record] of pendingEntries) {
          if (cancelled) return;
          try {
            const status = await getTaskStatus(record.taskId);
            if (cancelled) return;
            const runtimeState = mapPredictionRuntimeState(status.state);
            if (runtimeState === 'SUCCESS') {
              const retryTimer = referenceHydrationRetryTimerRef.current[backendKey];
              if (retryTimer) {
                window.clearTimeout(retryTimer);
                delete referenceHydrationRetryTimerRef.current[backendKey];
              }
              delete referenceHydrationRetryCountRef.current[backendKey];
              setReferencePredictionByBackend((prev) => ({
                ...prev,
                [backendKey]: {
                  ...(prev[backendKey] || record),
                  state: 'SUCCESS',
                  error: '',
                  updatedAt: Date.now()
                }
              }));
              continue;
            }
            if (runtimeState === 'FAILURE') {
              const retryTimer = referenceHydrationRetryTimerRef.current[backendKey];
              if (retryTimer) {
                window.clearTimeout(retryTimer);
                delete referenceHydrationRetryTimerRef.current[backendKey];
              }
              delete referenceHydrationRetryCountRef.current[backendKey];
              const errorText = readText(asRecord(status.info).error || asRecord(status.info).message || status.state);
              setReferencePredictionByBackend((prev) => ({
                ...prev,
                [backendKey]: {
                  ...(prev[backendKey] || record),
                  state: 'FAILURE',
                  error: errorText || 'Prediction failed.',
                  updatedAt: Date.now()
                }
              }));
              continue;
            }
            if (runtimeState !== 'QUEUED' && runtimeState !== 'RUNNING') continue;
            setReferencePredictionByBackend((prev) => {
              const current = prev[backendKey] || record;
              if (String(current.state || '').toUpperCase() === runtimeState) {
                return {
                  ...prev,
                  [backendKey]: {
                    ...current,
                    updatedAt: Date.now()
                  }
                };
              }
              return {
                ...prev,
                [backendKey]: {
                  ...current,
                  state: runtimeState,
                  updatedAt: Date.now()
                }
              };
            });
          } catch {
            // Same rotation strategy for reference predictions.
            setReferencePredictionByBackend((prev) => {
              const current = prev[backendKey] || record;
              if (!current) return prev;
              return {
                ...prev,
                [backendKey]: {
                  ...current,
                  updatedAt: Date.now()
                }
              };
            });
          }
        }
      })();
    }, 2200);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [referencePredictionByBackend]);

  useEffect(() => {
    const hydrationEntries = Object.entries(predictionBySmiles)
      .filter(([, record]) => shouldHydratePredictionRecord(record))
      .filter(([smiles]) => !predictionHydrationInFlightRef.current.has(smiles))
      .sort((a, b) => Number(a[1]?.updatedAt || 0) - Number(b[1]?.updatedAt || 0))
      .slice(0, 3);
    if (hydrationEntries.length === 0) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        for (const [smiles, record] of hydrationEntries) {
          if (cancelled) return;
          const taskId = readText(record.taskId).trim();
          if (!taskId || taskId.startsWith('local:')) continue;
          if (predictionHydrationInFlightRef.current.has(smiles)) continue;
          predictionHydrationInFlightRef.current.add(smiles);
          try {
            const blob = await downloadResultBlob(taskId, { mode: 'view' });
            if (cancelled) return;
            const parsed = await parseResultBundle(blob);
            if (!parsed) continue;
            const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain);
            setPredictionBySmiles((prev) => {
              const current = prev[smiles] || record;
              if (!current) return prev;
              return {
                ...prev,
                [smiles]: {
                  ...current,
                  state: 'SUCCESS',
                  pairIptm: resultPayload.pairIptm,
                  pairPae: resultPayload.pairPae,
                  pairIptmResolved: true,
                  ligandPlddt: resultPayload.ligandPlddt,
                  ligandAtomPlddts: resultPayload.ligandAtomPlddts,
                  ...(resultPayload.structureText.trim()
                    ? {
                        structureText: resultPayload.structureText,
                        structureFormat: resultPayload.structureFormat,
                        structureName: resultPayload.structureName
                      }
                    : {}),
                  error: '',
                  updatedAt: Date.now()
                }
              };
            });
            const retryTimer = predictionHydrationRetryTimerRef.current[smiles];
            if (retryTimer) {
              window.clearTimeout(retryTimer);
              delete predictionHydrationRetryTimerRef.current[smiles];
            }
            delete predictionHydrationRetryCountRef.current[smiles];
          } catch (error) {
            if (isResultArchivePendingError(error)) {
              const attempt = Number(predictionHydrationRetryCountRef.current[smiles] || 0) + 1;
              predictionHydrationRetryCountRef.current[smiles] = attempt;
              if (!predictionHydrationRetryTimerRef.current[smiles]) {
                const delayMs = computeHydrationRetryDelayMs(attempt);
                predictionHydrationRetryTimerRef.current[smiles] = window.setTimeout(() => {
                  delete predictionHydrationRetryTimerRef.current[smiles];
                  setPredictionBySmiles((prev) => {
                    const current = prev[smiles];
                    if (!current) return prev;
                    return {
                      ...prev,
                      [smiles]: {
                        ...current,
                        updatedAt: Date.now()
                      }
                    };
                  });
                }, delayMs);
              }
              continue;
            }
            setPredictionBySmiles((prev) => {
              const current = prev[smiles] || record;
              if (!current) return prev;
              return {
                ...prev,
                [smiles]: {
                  ...current,
                  error: readText(error instanceof Error ? error.message : error).trim() || current.error || '',
                  updatedAt: Date.now()
                }
              };
            });
          } finally {
            predictionHydrationInFlightRef.current.delete(smiles);
          }
        }
      })();
    }, 900);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [ligandChain, predictionBySmiles, targetChain]);

  useEffect(() => {
    const hydrationEntries = Object.entries(referencePredictionByBackend)
      .filter(([, record]) => shouldHydratePredictionRecord(record))
      .filter(([backendKey]) => !referenceHydrationInFlightRef.current.has(backendKey))
      .sort((a, b) => Number(a[1]?.updatedAt || 0) - Number(b[1]?.updatedAt || 0))
      .slice(0, 2);
    if (hydrationEntries.length === 0) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        for (const [backendKey, record] of hydrationEntries) {
          if (cancelled) return;
          const taskId = readText(record.taskId).trim();
          if (!taskId || taskId.startsWith('local:')) continue;
          if (referenceHydrationInFlightRef.current.has(backendKey)) continue;
          referenceHydrationInFlightRef.current.add(backendKey);
          try {
            const blob = await downloadResultBlob(taskId, { mode: 'view' });
            if (cancelled) return;
            const parsed = await parseResultBundle(blob);
            if (!parsed) continue;
            const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain);
            setReferencePredictionByBackend((prev) => {
              const current = prev[backendKey] || record;
              if (!current) return prev;
              return {
                ...prev,
                [backendKey]: {
                  ...current,
                  state: 'SUCCESS',
                  pairIptm: resultPayload.pairIptm,
                  pairPae: resultPayload.pairPae,
                  pairIptmResolved: true,
                  ligandPlddt: resultPayload.ligandPlddt,
                  ligandAtomPlddts: resultPayload.ligandAtomPlddts,
                  ...(resultPayload.structureText.trim()
                    ? {
                        structureText: resultPayload.structureText,
                        structureFormat: resultPayload.structureFormat,
                        structureName: resultPayload.structureName
                      }
                    : {}),
                  error: '',
                  updatedAt: Date.now()
                }
              };
            });
            const retryTimer = referenceHydrationRetryTimerRef.current[backendKey];
            if (retryTimer) {
              window.clearTimeout(retryTimer);
              delete referenceHydrationRetryTimerRef.current[backendKey];
            }
            delete referenceHydrationRetryCountRef.current[backendKey];
          } catch (error) {
            if (isResultArchivePendingError(error)) {
              const attempt = Number(referenceHydrationRetryCountRef.current[backendKey] || 0) + 1;
              referenceHydrationRetryCountRef.current[backendKey] = attempt;
              if (!referenceHydrationRetryTimerRef.current[backendKey]) {
                const delayMs = computeHydrationRetryDelayMs(attempt);
                referenceHydrationRetryTimerRef.current[backendKey] = window.setTimeout(() => {
                  delete referenceHydrationRetryTimerRef.current[backendKey];
                  setReferencePredictionByBackend((prev) => {
                    const current = prev[backendKey];
                    if (!current) return prev;
                    return {
                      ...prev,
                      [backendKey]: {
                        ...current,
                        updatedAt: Date.now()
                      }
                    };
                  });
                }, delayMs);
              }
              continue;
            }
            setReferencePredictionByBackend((prev) => {
              const current = prev[backendKey] || record;
              if (!current) return prev;
              return {
                ...prev,
                [backendKey]: {
                  ...current,
                  error: readText(error instanceof Error ? error.message : error).trim() || current.error || '',
                  updatedAt: Date.now()
                }
              };
            });
          } finally {
            referenceHydrationInFlightRef.current.delete(backendKey);
          }
        }
      })();
    }, 900);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [ligandChain, referencePredictionByBackend, targetChain]);

  useEffect(() => {
    if (typeof onPredictionStateChange !== 'function') return;
    const records = predictionBySmiles;
    const summary = summarizePredictionRecords(records);
    void onPredictionStateChange({ records, referenceRecords: referencePredictionByBackend, summary });
  }, [onPredictionStateChange, predictionBySmiles, referencePredictionByBackend]);

  const runCluster = useCallback(
    async (
      id: string,
      minPairs: number,
      groupBy: ClusterGroupBy = clusterGroupBy
    ): Promise<Array<Record<string, unknown>>> => {
      if (!id) return [];
      try {
        const clusterResponse = await clusterLeadOptimizationMmp({
          query_id: id,
          group_by: groupBy,
          min_pairs: minPairs
        });
        const rows = Array.isArray(clusterResponse.clusters)
          ? (clusterResponse.clusters as Array<Record<string, unknown>>)
          : [];
        setClusterGroupBy(groupBy);
        setQueryMinPairs(Math.max(1, minPairs));
        setClusters(rows);
        const cached = queryResultCacheRef.current.get(id);
        if (cached) {
          queryResultCacheRef.current.set(id, {
            ...cached,
            clusters: rows,
            min_pairs: Math.max(1, minPairs),
            cluster_group_by: groupBy
          });
        }
        return rows;
      } catch (e) {
        onError(e instanceof Error ? e.message : 'MMP cluster failed.');
        return [];
      }
    },
    [clusterGroupBy, onError]
  );

  const runMmpQuery = useCallback(
    async ({
      canQuery,
      effectiveLigandSmiles,
      variableItems,
      constantQuery,
      direction,
      queryProperty,
      mmpDatabaseId,
      queryMode,
      groupedByEnvironment,
      minPairs,
      envRadius,
      onTaskQueued,
      onTaskCompleted,
      onTaskFailed
    }: RunMmpQueryInput): Promise<RunMmpQueryResult | null> => {
      if (!canQuery) {
        onError('Ligand SMILES is missing or editing is disabled, cannot run query.');
        return null;
      }
      if (variableItems.length === 0) {
        onError('Please select a fragment or input variable query first.');
        return null;
      }
      const now = Date.now();
      if (mmpQueryInFlightRef.current) return null;
      if (now - lastMmpQueryAtRef.current < 350) return null;
      lastMmpQueryAtRef.current = now;
      mmpQueryInFlightRef.current = true;

      onError(null);
      setQueryNotice('Running MMP query...');
      setLoading(true);
      const startedAt = Date.now();
      let queuedTaskId = '';
      try {
        const selectedProperty = readText(queryProperty).trim();
        const selectedDirection = readText(direction).trim();
        const aggregationType = queryMode === 'many-to-many' ? 'group_by_fragment' : 'individual_transforms';
        const groupedByEnvironmentFlag =
          queryMode !== 'many-to-many'
            ? undefined
            : groupedByEnvironment === 'on'
              ? true
              : groupedByEnvironment === 'off'
                ? false
                : undefined;
        const propertyTargets: Record<string, unknown> = {};
        if (selectedProperty) {
          propertyTargets.property = selectedProperty;
          if (selectedDirection === 'increase' || selectedDirection === 'decrease') {
            propertyTargets.direction = selectedDirection;
          }
        }
        const requestPayload = {
          query_mol: effectiveLigandSmiles,
          variable_spec: {
            mode: variableItems[0]?.mode || 'substructure',
            items: variableItems
          },
          constant_spec: constantQuery.trim() ? { query: constantQuery.trim(), mode: 'substructure' } : {},
          property_targets: propertyTargets,
          mmp_database_id: mmpDatabaseId,
          query_mode: queryMode,
          aggregation_type: aggregationType,
          ...(groupedByEnvironmentFlag === undefined ? {} : { grouped_by_environment: groupedByEnvironmentFlag }),
          min_pairs: minPairs,
          rule_env_radius: envRadius,
          max_results: queryMode === 'many-to-many' ? 600 : 400
        } as Record<string, unknown>;
        const response = await queryLeadOptimizationMmp(requestPayload, {
          onEnqueued: async (taskId) => {
            queuedTaskId = String(taskId || '').trim();
            if (!queuedTaskId) return;
            setLastMmpTaskId(queuedTaskId);
            if (typeof onTaskQueued === 'function') {
              try {
                await onTaskQueued({ taskId: queuedTaskId, requestPayload });
              } catch (queueErr) {
                onError(queueErr instanceof Error ? queueErr.message : 'Failed to persist MMP task row.');
              }
            }
          }
        });
        const nextTransforms = Array.isArray(response.transforms)
          ? (response.transforms as Array<Record<string, unknown>>)
          : [];
        const nextClusters = Array.isArray(response.clusters)
          ? (response.clusters as Array<Record<string, unknown>>)
          : [];
        const responseRecord = asRecord(response);
        const responseAggregationType = readText(responseRecord.aggregation_type).trim() || aggregationType;
        const responseGroupedByEnvironment = readBoolean(responseRecord.grouped_by_environment, false);
        const nextQueryId = readText(response.query_id);
        setQueryId(nextQueryId);
        setActiveQueryMode(queryMode);
        setQueryMinPairs(Math.max(1, minPairs));
        setLastMmpTaskId(readText(response.task_id));
        setMmpRunVersion((prev) => prev + 1);
        setTransforms(nextTransforms);
        setClusters(nextClusters);
        setGlobalCount(readNumber(response.global_count));
        setQueryStats((response.stats as Record<string, unknown>) || {});
        setActiveEvidence(null);
        setActiveTransformId('');
        clearSelections();
        setEnumeratedCandidates([]);
        setPredictionBySmiles({});
        if (nextQueryId) {
          const cachePayload = {
            query_id: nextQueryId,
            query_mode: readText(response.query_mode),
            aggregation_type: responseAggregationType,
            grouped_by_environment: responseGroupedByEnvironment,
            property_targets: propertyTargets,
            rule_env_radius: Math.max(0, envRadius),
            mmp_database_id: readText(response.mmp_database_id),
            mmp_database_label: readText(response.mmp_database_label),
            mmp_database_schema: readText(response.mmp_database_schema),
            variable_spec: responseRecord.variable_spec || {},
            constant_spec: responseRecord.constant_spec || {},
            transforms: nextTransforms,
            global_transforms: Array.isArray(response.global_transforms) ? response.global_transforms : nextTransforms,
            clusters: nextClusters,
            count: readNumber(response.count),
            global_count: readNumber(response.global_count),
            stats: (response.stats as Record<string, unknown>) || {},
            min_pairs: Math.max(1, minPairs),
            cluster_group_by: clusterGroupBy,
          } as Record<string, unknown>;
          queryResultCacheRef.current.set(nextQueryId, cachePayload);
        }
        let candidateCount = 0;
        let persistedCandidates: Array<Record<string, unknown>> = [];
        if (nextTransforms.length === 0) {
          setQueryNotice(`MMP query complete (query_id=${response.query_id}) with 0 transforms.`);
          onError('MMP query returned no transforms. Try smaller fragment, min_pairs=1, larger env radius, or clear constant.');
        } else {
          setQueryNotice(
            readText(response.task_id)
              ? `MMP complete. task=${readText(response.task_id).slice(0, 12)} query=${readText(response.query_id).slice(0, 12)}`
              : `MMP complete. query=${readText(response.query_id).slice(0, 12)}`
          );
          try {
            const enumerate = await enumerateLeadOptimizationMmp({
              query_id: nextQueryId,
              property_constraints: {},
              max_candidates: 360
            });
            const rows = Array.isArray(enumerate.candidates)
              ? (enumerate.candidates as Array<Record<string, unknown>>)
              : [];
            setEnumeratedCandidates(rows);
            candidateCount = rows.length;
            persistedCandidates = rows;
          } catch (enumerateError) {
            onError(enumerateError instanceof Error ? enumerateError.message : 'Failed to build result rows.');
          }
        }
        const persistedQueryResult = {
          query_id: nextQueryId,
          query_mode: queryMode,
          aggregation_type: responseAggregationType,
          grouped_by_environment: responseGroupedByEnvironment,
          property_targets: propertyTargets,
          rule_env_radius: Math.max(0, envRadius),
          mmp_database_id: readText(response.mmp_database_id),
          mmp_database_label: readText(response.mmp_database_label),
          mmp_database_schema: readText(response.mmp_database_schema),
          transforms: nextTransforms,
          global_transforms: Array.isArray(response.global_transforms) ? response.global_transforms : nextTransforms,
          clusters: nextClusters,
          count: readNumber(response.count),
          global_count: readNumber(response.global_count),
          min_pairs: Math.max(1, minPairs),
          stats: (response.stats as Record<string, unknown>) || {}
        } as Record<string, unknown>;
        const completedTaskId = queuedTaskId || readText(response.task_id);
        if (completedTaskId && typeof onTaskCompleted === 'function') {
          await onTaskCompleted({
            taskId: completedTaskId,
            queryId: nextQueryId,
            transformCount: nextTransforms.length,
            candidateCount,
            elapsedSeconds: Math.max(0, (Date.now() - startedAt) / 1000),
            resultSnapshot: {
              query_result: persistedQueryResult,
              enumerated_candidates: persistedCandidates
            }
          });
        }
        return {
          queryId: nextQueryId,
          transformCount: nextTransforms.length,
          candidateCount
        };
      } catch (e) {
        setQueryNotice('');
        const message = e instanceof Error ? e.message : 'MMP query failed.';
        onError(message);
        if (queuedTaskId && typeof onTaskFailed === 'function') {
          await onTaskFailed({ taskId: queuedTaskId, error: message });
        }
        return null;
      } finally {
        mmpQueryInFlightRef.current = false;
        setLoading(false);
      }
      return null;
    },
    [clearSelections, clusterGroupBy, onError]
  );

  const loadQueryRun = useCallback(
    async (nextQueryId: string) => {
      const normalizedId = readText(nextQueryId).trim();
      if (!normalizedId) return;
      setLoading(true);
      onError(null);
      try {
        const queryResult = await fetchLeadOptimizationMmpQueryResult(normalizedId);
        const queryResultRecord = asRecord(queryResult);
        const nextTransforms = Array.isArray(queryResult.transforms)
          ? (queryResult.transforms as Array<Record<string, unknown>>)
          : [];
        const nextMinPairs = Math.max(1, readNumber(asRecord(queryResult).min_pairs || 1));
        setQueryMinPairs(nextMinPairs);
        const nextClusters = Array.isArray(queryResult.clusters)
          ? (queryResult.clusters as Array<Record<string, unknown>>)
          : [];
        setClusters(nextClusters);
        setQueryId(normalizedId);
        const nextMode = readText(queryResult.query_mode) === 'many-to-many' ? 'many-to-many' : 'one-to-many';
        setActiveQueryMode(nextMode);
        const responseAggregationType =
          readText(queryResultRecord.aggregation_type).trim() || (nextMode === 'many-to-many' ? 'group_by_fragment' : 'individual_transforms');
        const responseGroupedByEnvironment = readBoolean(queryResultRecord.grouped_by_environment, false);
        const savedGroupBy = readText(queryResultRecord.cluster_group_by).toLowerCase();
        const nextGroupBy = savedGroupBy === 'from' || savedGroupBy === 'rule_env_radius' ? savedGroupBy : 'to';
        setClusterGroupBy(nextGroupBy as ClusterGroupBy);
        setTransforms(nextTransforms);
        setGlobalCount(readNumber(queryResult.global_count));
        setQueryStats((queryResult.stats as Record<string, unknown>) || {});
        setActiveTransformId('');
        setActiveEvidence(null);
        clearSelections();
        queryResultCacheRef.current.set(normalizedId, {
          query_id: normalizedId,
          query_mode: nextMode,
          aggregation_type: responseAggregationType,
          grouped_by_environment: responseGroupedByEnvironment,
          mmp_database_id: readText(queryResult.mmp_database_id),
          mmp_database_label: readText(queryResult.mmp_database_label),
          mmp_database_schema: readText(queryResult.mmp_database_schema),
          transforms: nextTransforms,
          global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : nextTransforms,
          clusters: nextClusters,
          count: readNumber(queryResult.count),
          global_count: readNumber(queryResult.global_count),
          min_pairs: nextMinPairs,
          cluster_group_by: nextGroupBy,
          stats: (queryResult.stats as Record<string, unknown>) || {}
        });

        const enumerate = await enumerateLeadOptimizationMmp({
          query_id: normalizedId,
          property_constraints: {},
          max_candidates: 360
        });
        const nextCandidates = Array.isArray(enumerate.candidates)
          ? (enumerate.candidates as Array<Record<string, unknown>>)
          : [];
        setEnumeratedCandidates(nextCandidates);
        setQueryNotice(`Loaded MMP rows (${nextCandidates.length}).`);
      } catch (error) {
        onError(error instanceof Error ? error.message : 'Failed to load saved MMP query.');
      } finally {
        setLoading(false);
      }
    },
    [clearSelections, onError]
  );

  const loadEvidence = useCallback(
    async (transformId: string) => {
      if (!transformId) {
        setActiveEvidence(null);
        return;
      }
      setEvidenceLoading(true);
      try {
        const evidence = await fetchLeadOptimizationMmpEvidence(transformId);
        setActiveEvidence(evidence);
      } catch (e) {
        setActiveEvidence(null);
        onError(e instanceof Error ? e.message : 'Failed to load transform evidence.');
      } finally {
        setEvidenceLoading(false);
      }
    },
    [onError]
  );

  const handleTransformClick = useCallback(
    async (row: Record<string, unknown>) => {
      const transformId = readText(row.transform_id);
      if (!transformId) return;
      setActiveTransformId(transformId);
      await loadEvidence(transformId);
    },
    [loadEvidence]
  );

  const runEnumerate = useCallback(async () => {
    if (!queryId) {
      onError('Please run MMP query first.');
      return;
    }
    onError(null);
    setLoading(true);
    try {
      const result = await enumerateLeadOptimizationMmp({
        query_id: queryId,
        transform_ids: selectedTransformIds,
        cluster_ids: selectedClusterIds,
        property_constraints: {},
        max_candidates: 200
      });
      const rows = Array.isArray(result.candidates) ? (result.candidates as Array<Record<string, unknown>>) : [];
      setEnumeratedCandidates(rows);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'MMP enumeration failed.');
    } finally {
      setLoading(false);
    }
  }, [onError, queryId, selectedClusterIds, selectedTransformIds]);

  const setClusterGrouping = useCallback(
    async (groupBy: ClusterGroupBy) => {
      const normalized: ClusterGroupBy =
        groupBy === 'from' || groupBy === 'rule_env_radius' ? groupBy : 'to';
      setClusterGroupBy(normalized);
      if (!queryId) return;
      await runCluster(queryId, queryMinPairs, normalized);
    },
    [queryId, queryMinPairs, runCluster]
  );

  const submitPredictCandidateTask = useCallback(
    async ({
      candidateSmiles,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues
    }: {
      candidateSmiles: string;
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
    }) => {
      const nextSmiles = String(candidateSmiles || '').trim();
      if (!nextSmiles) {
        throw new Error('Candidate SMILES is required.');
      }
      if (!referenceReady) {
        throw new Error('Please upload reference target+ligand first.');
      }
      const effectiveProteinSequence = String(referenceProteinSequence || '').trim() || String(proteinSequence || '').trim();
      const templateStructureText = String(referenceTemplateStructureText || '').trim();
      if (!effectiveProteinSequence && !templateStructureText) {
        throw new Error('Protein sequence/template is unavailable. Upload reference target first or provide sequence.');
      }
      const normalizedTargetChain = String(targetChain || '').trim();
      const normalizedLigandChain = String(ligandChain || '').trim();
      if (!normalizedTargetChain || !normalizedLigandChain) {
        throw new Error('Target chain and ligand chain are required for lead optimization prediction.');
      }
      if (normalizedTargetChain.toUpperCase() === normalizedLigandChain.toUpperCase()) {
        throw new Error(
          `Target chain and ligand chain cannot be the same ('${normalizedTargetChain}'). Re-upload reference to resolve chain mapping.`
        );
      }
      const selectedBackend = String(backendOverride || backend || 'boltz').trim().toLowerCase();
      if (selectedBackend === 'protenix' && !effectiveProteinSequence) {
        throw new Error('Protenix backend requires protein sequence. Please provide sequence or use Boltz/AlphaFold3 template mode.');
      }
      const taskId = await predictLeadOptimizationCandidate({
        candidateSmiles: nextSmiles,
        proteinSequence: effectiveProteinSequence,
        backend: selectedBackend,
        targetChain: normalizedTargetChain,
        ligandChain: normalizedLigandChain,
        referenceTemplateStructureText: templateStructureText,
        referenceTemplateFormat: referenceTemplateFormat === 'pdb' ? 'pdb' : 'cif',
        pocketResidues,
        useMsaServer: true,
        seed: null
      });
      return taskId;
    },
    [backend, ligandChain, proteinSequence, targetChain]
  );

  const runPredictCandidate = useCallback(
    async ({
      candidateSmiles,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues
    }: {
      candidateSmiles: string;
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
    }) => {
      onError(null);
      setLoading(true);
      try {
        const normalizedSmiles = String(candidateSmiles || '').trim();
        const selectedBackend = String(backendOverride || backend || 'boltz').trim().toLowerCase();
        const retryTimer = predictionHydrationRetryTimerRef.current[normalizedSmiles];
        if (retryTimer) {
          window.clearTimeout(retryTimer);
          delete predictionHydrationRetryTimerRef.current[normalizedSmiles];
        }
        delete predictionHydrationRetryCountRef.current[normalizedSmiles];
        const previousRecord = predictionBySmiles[normalizedSmiles];
        const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
        setPredictionBySmiles((prev) => ({
          ...prev,
          [normalizedSmiles]: {
            ...(prev[normalizedSmiles] || previousRecord || {}),
            ...buildQueuedPredictionRecord(localTaskId, selectedBackend)
          }
        }));
        const taskId = await submitPredictCandidateTask({
          candidateSmiles: normalizedSmiles,
          backend: selectedBackend,
          referenceReady,
          referenceProteinSequence,
          referenceTemplateStructureText,
          referenceTemplateFormat,
          pocketResidues
        });
        if (typeof onPredictionQueued === 'function') {
          await onPredictionQueued({ taskId, backend: selectedBackend, candidateSmiles: normalizedSmiles });
        }
        setLastPredictionTaskId(taskId);
        setPredictionBySmiles((prev) => ({
          ...prev,
          [normalizedSmiles]: {
            ...buildQueuedPredictionRecord(taskId, selectedBackend)
          }
        }));
      } catch (e) {
        const normalizedSmiles = String(candidateSmiles || '').trim();
        const previousRecord = predictionBySmiles[normalizedSmiles];
        setPredictionBySmiles((prev) => {
          const current = prev[normalizedSmiles];
          if (!current || !String(current.taskId || '').startsWith('local:')) return prev;
          if (previousRecord) {
            return {
              ...prev,
              [normalizedSmiles]: previousRecord
            };
          }
          const next = { ...prev };
          delete next[normalizedSmiles];
          return next;
        });
        onError(e instanceof Error ? e.message : 'Candidate prediction failed.');
      } finally {
        setLoading(false);
      }
    },
    [backend, onError, onPredictionQueued, predictionBySmiles, submitPredictCandidateTask]
  );

  const runPredictReferenceForBackend = useCallback(
    async ({
      candidateSmiles,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues
    }: {
      candidateSmiles: string;
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
    }) => {
      const selectedBackend = normalizeBackendKey(backendOverride || backend || 'boltz');
      const referenceSmiles = String(candidateSmiles || '').trim();
      if (!referenceSmiles) return;
      const retryTimer = referenceHydrationRetryTimerRef.current[selectedBackend];
      if (retryTimer) {
        window.clearTimeout(retryTimer);
        delete referenceHydrationRetryTimerRef.current[selectedBackend];
      }
      delete referenceHydrationRetryCountRef.current[selectedBackend];
      const existing = referencePredictionByBackend[selectedBackend];
      const existingState = String(existing?.state || '').toUpperCase();
      if (existingState === 'QUEUED' || existingState === 'RUNNING') return;
      if (existingState === 'SUCCESS') {
        if (existing && existing.pairIptmResolved === true && readText(existing.structureText).trim()) return;
      }

      const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
      setReferencePredictionByBackend((prev) => ({
        ...prev,
        [selectedBackend]: {
          ...(prev[selectedBackend] || existing || {}),
          ...buildQueuedPredictionRecord(localTaskId, selectedBackend)
        }
      }));
      try {
        const taskId = await submitPredictCandidateTask({
          candidateSmiles: referenceSmiles,
          backend: selectedBackend,
          referenceReady,
          referenceProteinSequence,
          referenceTemplateStructureText,
          referenceTemplateFormat,
          pocketResidues
        });
        setLastPredictionTaskId(taskId);
        setReferencePredictionByBackend((prev) => ({
          ...prev,
          [selectedBackend]: {
            ...buildQueuedPredictionRecord(taskId, selectedBackend)
          }
        }));
      } catch (error) {
        setReferencePredictionByBackend((prev) => {
          const current = prev[selectedBackend];
          if (!current || !String(current.taskId || '').startsWith('local:')) return prev;
          const next = { ...prev };
          delete next[selectedBackend];
          return next;
        });
        onError(error instanceof Error ? error.message : 'Reference prediction failed.');
      }
    },
    [backend, onError, referencePredictionByBackend, submitPredictCandidateTask]
  );

  const runPredictBatch = useCallback(
    async ({
      candidateSmilesList,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues
    }: {
      candidateSmilesList: string[];
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
    }) => {
      const batch = Array.from(
        new Set((candidateSmilesList || []).map((item) => String(item || '').trim()).filter(Boolean))
      ).slice(0, 24);
      if (batch.length === 0) {
        onError('Please select at least one candidate first.');
        return;
      }
      onError(null);
      setLoading(true);
      let success = 0;
      let failure = 0;
      let lastTaskId = '';
      const selectedBackend = String(backendOverride || backend || 'boltz').trim().toLowerCase();
      try {
        for (const smiles of batch) {
          const previousRecord = predictionBySmiles[smiles];
          const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
          setPredictionBySmiles((prev) => ({
            ...prev,
            [smiles]: {
              ...(prev[smiles] || previousRecord || {}),
              ...buildQueuedPredictionRecord(localTaskId, selectedBackend)
            }
          }));
          try {
            const taskId = await submitPredictCandidateTask({
              candidateSmiles: smiles,
              backend: selectedBackend,
              referenceReady,
              referenceProteinSequence,
              referenceTemplateStructureText,
              referenceTemplateFormat,
              pocketResidues
            });
            if (typeof onPredictionQueued === 'function') {
              await onPredictionQueued({ taskId, backend: selectedBackend, candidateSmiles: smiles });
            }
            success += 1;
            lastTaskId = taskId;
            setPredictionBySmiles((prev) => ({
              ...prev,
              [smiles]: {
                ...buildQueuedPredictionRecord(taskId, selectedBackend)
              }
            }));
          } catch (_error) {
            failure += 1;
            setPredictionBySmiles((prev) => {
              const current = prev[smiles];
              if (!current || !String(current.taskId || '').startsWith('local:')) return prev;
              if (previousRecord) {
                return {
                  ...prev,
                  [smiles]: previousRecord
                };
              }
              const next = { ...prev };
              delete next[smiles];
              return next;
            });
          }
        }
      } finally {
        setLoading(false);
      }
      if (lastTaskId) setLastPredictionTaskId(lastTaskId);
      if (failure > 0) {
        onError(`${failure}/${batch.length} prediction submissions failed. ${success} submitted.`);
      } else {
        onError(null);
      }
    },
    [backend, onError, onPredictionQueued, predictionBySmiles, submitPredictCandidateTask]
  );

  const ensurePredictionResult = useCallback(
    async (candidateSmiles: string): Promise<LeadOptPredictionRecord | null> => {
      const normalizedSmiles = String(candidateSmiles || '').trim();
      if (!normalizedSmiles) return null;
      const existing = predictionBySmiles[normalizedSmiles];
      if (!existing) return null;
      if (String(existing.state || '').toUpperCase() !== 'SUCCESS') return existing;
      if (readText(existing.structureText).trim() && existing.pairIptmResolved === true) {
        return existing;
      }

      const taskId = readText(existing.taskId).trim();
      if (!taskId) return existing;
      try {
        const blob = await downloadResultBlob(taskId, { mode: 'view' });
        const parsed = await parseResultBundle(blob);
        const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain);
        const nextRecord: LeadOptPredictionRecord = {
          ...existing,
          state: 'SUCCESS',
          pairIptm: resultPayload.pairIptm,
          pairPae: resultPayload.pairPae,
          pairIptmResolved: true,
          ligandPlddt: resultPayload.ligandPlddt,
          ligandAtomPlddts: resultPayload.ligandAtomPlddts,
          ...(resultPayload.structureText.trim()
            ? {
                structureText: resultPayload.structureText,
                structureFormat: resultPayload.structureFormat,
                structureName: resultPayload.structureName
              }
            : {}),
          error: '',
          updatedAt: Date.now()
        };
        setPredictionBySmiles((prev) => ({
          ...prev,
          [normalizedSmiles]: nextRecord
        }));
        const retryTimer = predictionHydrationRetryTimerRef.current[normalizedSmiles];
        if (retryTimer) {
          window.clearTimeout(retryTimer);
          delete predictionHydrationRetryTimerRef.current[normalizedSmiles];
        }
        delete predictionHydrationRetryCountRef.current[normalizedSmiles];
        return nextRecord;
      } catch (error) {
        if (isResultArchivePendingError(error)) {
          const attempt = Number(predictionHydrationRetryCountRef.current[normalizedSmiles] || 0) + 1;
          predictionHydrationRetryCountRef.current[normalizedSmiles] = attempt;
          if (!predictionHydrationRetryTimerRef.current[normalizedSmiles]) {
            const delayMs = computeHydrationRetryDelayMs(attempt);
            predictionHydrationRetryTimerRef.current[normalizedSmiles] = window.setTimeout(() => {
              delete predictionHydrationRetryTimerRef.current[normalizedSmiles];
              setPredictionBySmiles((prev) => {
                const current = prev[normalizedSmiles];
                if (!current) return prev;
                return {
                  ...prev,
                  [normalizedSmiles]: {
                    ...current,
                    updatedAt: Date.now()
                  }
                };
              });
            }, delayMs);
          }
          return existing;
        }
        onError(error instanceof Error ? error.message : 'Failed to load prediction result.');
        return existing;
      }
    },
    [ligandChain, onError, predictionBySmiles, targetChain]
  );

  const ensureReferencePredictionResult = useCallback(
    async (backendKeyInput: string): Promise<LeadOptPredictionRecord | null> => {
      const backendKey = normalizeBackendKey(backendKeyInput);
      const existing = referencePredictionByBackend[backendKey];
      if (!existing) return null;
      if (String(existing.state || '').toUpperCase() !== 'SUCCESS') return existing;
      if (readText(existing.structureText).trim() && existing.pairIptmResolved === true) {
        return existing;
      }

      const taskId = readText(existing.taskId).trim();
      if (!taskId) return existing;
      try {
        const blob = await downloadResultBlob(taskId, { mode: 'view' });
        const parsed = await parseResultBundle(blob);
        const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain);
        const nextRecord: LeadOptPredictionRecord = {
          ...existing,
          state: 'SUCCESS',
          pairIptm: resultPayload.pairIptm,
          pairPae: resultPayload.pairPae,
          pairIptmResolved: true,
          ligandPlddt: resultPayload.ligandPlddt,
          ligandAtomPlddts: resultPayload.ligandAtomPlddts,
          ...(resultPayload.structureText.trim()
            ? {
                structureText: resultPayload.structureText,
                structureFormat: resultPayload.structureFormat,
                structureName: resultPayload.structureName
              }
            : {}),
          error: '',
          updatedAt: Date.now()
        };
        setReferencePredictionByBackend((prev) => ({
          ...prev,
          [backendKey]: nextRecord
        }));
        const retryTimer = referenceHydrationRetryTimerRef.current[backendKey];
        if (retryTimer) {
          window.clearTimeout(retryTimer);
          delete referenceHydrationRetryTimerRef.current[backendKey];
        }
        delete referenceHydrationRetryCountRef.current[backendKey];
        return nextRecord;
      } catch (error) {
        if (isResultArchivePendingError(error)) {
          const attempt = Number(referenceHydrationRetryCountRef.current[backendKey] || 0) + 1;
          referenceHydrationRetryCountRef.current[backendKey] = attempt;
          if (!referenceHydrationRetryTimerRef.current[backendKey]) {
            const delayMs = computeHydrationRetryDelayMs(attempt);
            referenceHydrationRetryTimerRef.current[backendKey] = window.setTimeout(() => {
              delete referenceHydrationRetryTimerRef.current[backendKey];
              setReferencePredictionByBackend((prev) => {
                const current = prev[backendKey];
                if (!current) return prev;
                return {
                  ...prev,
                  [backendKey]: {
                    ...current,
                    updatedAt: Date.now()
                  }
                };
              });
            }, delayMs);
          }
          return existing;
        }
        onError(error instanceof Error ? error.message : 'Failed to load prediction result.');
        return existing;
      }
    },
    [ligandChain, onError, referencePredictionByBackend, targetChain]
  );

  // Keep result-bundle hydration on-demand to avoid heavy background downloads
  // when users are only browsing candidate/task lists.

  const toggleTransformSelection = useCallback((transformId: string) => {
    if (!transformId) return;
    setSelectedTransformIds((prev) => {
      if (prev.includes(transformId)) return prev.filter((item) => item !== transformId);
      return [...prev, transformId];
    });
  }, []);

  const toggleClusterSelection = useCallback((clusterId: string) => {
    if (!clusterId) return;
    setSelectedClusterIds((prev) => {
      if (prev.includes(clusterId)) return prev.filter((item) => item !== clusterId);
      return [...prev, clusterId];
    });
  }, []);

  const selectTopTransforms = useCallback(
    (limit = 12) => {
      const next = [...transforms]
        .sort((a, b) => {
          const [be, bp, bd] = sortScore(b.evidence_strength, b.n_pairs, b.median_delta);
          const [ae, ap, ad] = sortScore(a.evidence_strength, a.n_pairs, a.median_delta);
          if (be !== ae) return be - ae;
          if (bp !== ap) return bp - ap;
          return Math.abs(bd) - Math.abs(ad);
        })
        .slice(0, Math.max(1, limit))
        .map((item) => readText(item.transform_id))
        .filter(Boolean);
      setSelectedTransformIds(next);
    },
    [transforms]
  );

  const selectTopClusters = useCallback(
    (limit = 6) => {
      const next = [...clusters]
        .sort((a, b) => {
          const [bs, bi, bd] = sortScore(b.cluster_size, b['%median_improved'], b.median_delta);
          const [as, ai, ad] = sortScore(a.cluster_size, a['%median_improved'], a.median_delta);
          if (bs !== as) return bs - as;
          if (bi !== ai) return bi - ai;
          return Math.abs(bd) - Math.abs(ad);
        })
        .slice(0, Math.max(1, limit))
        .map((item) => readText(item.cluster_id) || readText(item.group_key))
        .filter(Boolean);
      setSelectedClusterIds(next);
    },
    [clusters]
  );

  const evidencePairs = useMemo(() => {
    if (!activeEvidence) return [] as Array<Record<string, unknown>>;
    return Array.isArray(activeEvidence.pairs) ? activeEvidence.pairs : [];
  }, [activeEvidence]);

  const activeTransformSummary = useMemo(() => {
    const transform = (activeEvidence?.transform as Record<string, unknown>) || {};
    return {
      nPairs: readText(transform.n_pairs) || readText(activeEvidence?.n_pairs) || String(evidencePairs.length),
      medianDelta: formatMetric(transform.median_delta),
      iqr: formatMetric(transform.iqr),
      std: formatMetric(transform.std),
      percentImproved: formatMetric(transform.percent_improved || transform['%improved']),
      directionality: formatMetric(transform.directionality),
      evidenceStrength: formatMetric(transform.evidence_strength)
    };
  }, [activeEvidence, evidencePairs.length]);

  const hydrateFromSnapshot = useCallback((snapshot: LeadOptMmpPersistedSnapshot | null | undefined) => {
    const payload = asRecord(snapshot);
    const queryResult = asRecord(payload.query_result);
    const nextQueryId = readText(queryResult.query_id);
    if (!nextQueryId) return;
    const nextTransforms = asRecordArray(queryResult.transforms);
    const nextClusters = asRecordArray(queryResult.clusters);
    const nextCandidates = asRecordArray(payload.enumerated_candidates);
    const nextPredictions = normalizePredictionMap(payload.prediction_by_smiles);
    const nextReferenceByBackend = normalizePredictionMap(payload.reference_prediction_by_backend);

    setQueryId(nextQueryId);
    const nextMode = readText(queryResult.query_mode).toLowerCase() === 'many-to-many' ? 'many-to-many' : 'one-to-many';
    const nextAggregationType =
      readText(queryResult.aggregation_type).trim() || (nextMode === 'many-to-many' ? 'group_by_fragment' : 'individual_transforms');
    const nextGroupedByEnvironment = readBoolean(queryResult.grouped_by_environment, false);
    setActiveQueryMode(nextMode);
    const nextMinPairs = Math.max(1, readNumber(queryResult.min_pairs || 1));
    setQueryMinPairs(nextMinPairs);
    const savedGroupBy = readText(queryResult.cluster_group_by).toLowerCase();
    setClusterGroupBy(
      savedGroupBy === 'from' || savedGroupBy === 'rule_env_radius' ? (savedGroupBy as ClusterGroupBy) : 'to'
    );
    setTransforms(nextTransforms);
    setClusters(nextClusters);
    setEnumeratedCandidates(nextCandidates);
    setGlobalCount(readNumber(queryResult.global_count));
    setQueryStats(asRecord(queryResult.stats));
    setActiveTransformId('');
    setActiveEvidence(null);
    setSelectedTransformIds([]);
    setSelectedClusterIds([]);
    setPredictionBySmiles(nextPredictions);
    setReferencePredictionByBackend(nextReferenceByBackend);
    setQueryNotice(`Loaded saved MMP rows (${nextCandidates.length}).`);
    queryResultCacheRef.current.set(nextQueryId, {
      query_id: nextQueryId,
      query_mode: nextMode,
      aggregation_type: nextAggregationType,
      grouped_by_environment: nextGroupedByEnvironment,
      mmp_database_id: readText(queryResult.mmp_database_id),
      mmp_database_label: readText(queryResult.mmp_database_label),
      mmp_database_schema: readText(queryResult.mmp_database_schema),
      transforms: nextTransforms,
      global_transforms: asRecordArray(queryResult.global_transforms),
      clusters: nextClusters,
      count: readNumber(queryResult.count),
      global_count: readNumber(queryResult.global_count),
      min_pairs: nextMinPairs,
      cluster_group_by:
        savedGroupBy === 'from' || savedGroupBy === 'rule_env_radius' ? savedGroupBy : 'to',
      stats: asRecord(queryResult.stats)
    });
  }, []);

  return {
    loading,
    evidenceLoading,
    queryNotice,
    queryId,
    activeQueryMode,
    clusterGroupBy,
    queryMinPairs,
    globalCount,
    queryStats,
    transforms,
    clusters,
    activeTransformId,
    activeEvidence,
    selectedTransformIds,
    selectedClusterIds,
    enumeratedCandidates,
    predictionBySmiles,
    referencePredictionByBackend,
    lastPredictionTaskId,
    lastMmpTaskId,
    mmpRunVersion,
    hasSelection,
    runMmpQuery,
    loadQueryRun,
    runCluster,
    setClusterGrouping,
    runEnumerate,
    runPredictCandidate,
    runPredictReferenceForBackend,
    runPredictBatch,
    ensurePredictionResult,
    ensureReferencePredictionResult,
    handleTransformClick,
    toggleTransformSelection,
    toggleClusterSelection,
    selectTopTransforms,
    selectTopClusters,
    clearSelections,
    hydrateFromSnapshot,
    evidencePairs,
    activeTransformSummary
  };
}
