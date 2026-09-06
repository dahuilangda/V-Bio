import {
  readFirstFiniteMetric,
  readIpsaeDomMetric,
  readLigandIpsaeMaxMetric,
  readObjectPath,
  readPairIptmForChains,
  resolvePreferredInterfaceMetricFromValues
} from '../../../../pages/projectDetail/projectMetrics';
import {
  asRecord as asRecordStrict,
  asString
} from '../../../../pages/projectTasks/recordReaders';
import type { ParsedResultBundle } from '../../../../types/models';
import { isNumericToken } from '../../../../pages/projectTasks/taskDataConfidence';

interface LeadOptPredictionRenderPayload {
  ligandRenderSmiles: string;
  ligandRenderAtomPlddts: number[];
}

type PredictionState = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE';
const LEADOPT_PREDICTION_RECORD_KEY_SEPARATOR = '::';

export interface LeadOptPredictionRecord {
  taskId: string;
  state: PredictionState;
  backend: string;
  pairIptm: number | null;
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  interfaceMetricSource: 'ipsae' | 'iptm' | 'none';
  pairPae: number | null;
  pairIptmResolved?: boolean;
  ligandPlddt: number | null;
  ligandAtomPlddts: number[];
  ligandRenderSmiles?: string;
  ligandRenderAtomPlddts?: number[];
  structureText?: string;
  structureFormat?: 'cif' | 'pdb';
  structureName?: string;
  resultBundleHydrated?: boolean;
  error: string;
  updatedAt: number;
}

function resolveLeadOptPreferredInterfaceMetric(params: {
  confidence?: Record<string, unknown>;
  affinity?: Record<string, unknown>;
  compact?: Record<string, unknown>;
  pairIptm: number | null;
}): {
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  interfaceMetricSource: 'ipsae' | 'iptm' | 'none';
} {
  const confidence = params.confidence || {};
  const affinity = params.affinity || {};
  const compact = params.compact || {};
  const ligandIpsaeMax =
    readLigandIpsaeMaxMetric(confidence) ??
    readLigandIpsaeMaxMetric(affinity) ??
    normalizeIptm(compact.ligand_ipsae_max ?? compact.ligandIpsaeMax);
  const ipsaeDom =
    readIpsaeDomMetric(confidence) ??
    readIpsaeDomMetric(affinity) ??
    normalizeIptm(compact.ipsae_dom ?? compact.ipsaeDom);
  const preferred = resolvePreferredInterfaceMetricFromValues({
    pairIptm: params.pairIptm,
    iptm: params.pairIptm ?? normalizeIptm(compact.pair_iptm ?? compact.pairIptm ?? compact.iptm),
    ipsaeDom,
    ligandIpsaeMax
  });
  return {
    interfaceMetricValue: preferred.value,
    interfaceMetricLabel: preferred.label,
    interfaceMetricSource: preferred.source
  };
}

export function buildLeadOptPredictionRecordKey(backendInput: unknown, candidateSmilesInput: unknown): string {
  const backendKey = normalizePredictionBackendStrict(backendInput);
  if (!backendKey) return '';
  const normalizedSmiles = asString(candidateSmilesInput).trim();
  if (!normalizedSmiles) return '';
  return `${backendKey}${LEADOPT_PREDICTION_RECORD_KEY_SEPARATOR}${encodeURIComponent(normalizedSmiles)}`;
}

export function parseLeadOptPredictionRecordKey(keyInput: unknown): { backend: string; smiles: string } {
  const key = asString(keyInput).trim();
  if (!key) return { backend: '', smiles: '' };
  const separatorIndex = key.indexOf(LEADOPT_PREDICTION_RECORD_KEY_SEPARATOR);
  if (separatorIndex < 0) {
    return { backend: '', smiles: key };
  }
  const backendKey = normalizePredictionBackendStrict(key.slice(0, separatorIndex));
  const encodedSmiles = key.slice(separatorIndex + LEADOPT_PREDICTION_RECORD_KEY_SEPARATOR.length);
  if (!encodedSmiles) {
    return { backend: backendKey, smiles: '' };
  }
  try {
    return {
      backend: backendKey,
      smiles: decodeURIComponent(encodedSmiles)
    };
  } catch (err) {
    console.error('decodeURIComponent failed for prediction key smiles; keeping encoded value.', err);
    return {
      backend: backendKey,
      smiles: encodedSmiles
    };
  }
}



function mapPredictionRuntimeState(raw: unknown): PredictionState | null {
  const token = String(raw || '').trim().toUpperCase();
  if (!token) return null;
  if (token === 'SUCCESS' || token === 'SUCCEEDED' || token === 'COMPLETED' || token === 'COMPLETE' || token === 'DONE' || token === 'FINISHED') return 'SUCCESS';
  if (token === 'FAILURE' || token === 'FAILED' || token === 'ERROR' || token === 'TIMEOUT' || token === 'REVOKED' || token === 'CANCELED' || token === 'CANCELLED' || token === 'TERMINATED') return 'FAILURE';
  if (token === 'PENDING' || token === 'RECEIVED' || token === 'RETRY' || token === 'QUEUED' || token === 'WAITING') return 'QUEUED';
  if (
    token === 'STARTED' ||
    token === 'RUNNING' ||
    token === 'PROGRESS' ||
    token === 'STARTING' ||
    token === 'PREPARING' ||
    token === 'ACQUIRING_GPU' ||
    token === 'GPU_ACQUIRED' ||
    token === 'UPLOADING' ||
    token === 'PROCESSING' ||
    token === 'PACKAGING'
  ) return 'RUNNING';
  return null;
}

function inferPredictionRuntimeStateFromStatusPayload(status: { state?: unknown; info?: unknown }): PredictionState | null {
  const direct = mapPredictionRuntimeState(status?.state);
  if (direct === 'SUCCESS' || direct === 'FAILURE') return direct;
  const info = asRecord(status?.info);
  const resultFile = asString(info.result_file || info.resultFile).trim();
  if (resultFile) return 'SUCCESS';
  if (info.result && typeof info.result === 'object') return 'SUCCESS';
  const explicitError = asString(info.error || info.exc_message || info.exc_type).trim();
  if (explicitError) return 'FAILURE';
  const tracker = asRecord(info.tracker);
  const statusText = asString(info.status || info.message || tracker.details || tracker.status).trim().toLowerCase();
  if (statusText) {
    if (
      statusText.includes('non-existent') ||
      statusText.includes('does not exist') ||
      statusText.includes('not found')
    ) {
      return 'FAILURE';
    }
    if (statusText.includes('failed') || statusText.includes('error') || statusText.includes('timeout')) {
      return 'FAILURE';
    }
    if (
      statusText.includes('complete') ||
      statusText.includes('completed') ||
      statusText.includes('success') ||
      statusText.includes('succeeded') ||
      statusText.includes('done') ||
      statusText.includes('finished')
    ) {
      return 'SUCCESS';
    }
    if (
      statusText.includes('running') ||
      statusText.includes('starting') ||
      statusText.includes('acquiring') ||
      statusText.includes('gpu') ||
      statusText.includes('preparing') ||
      statusText.includes('uploading') ||
      statusText.includes('processing') ||
      statusText.includes('packaging')
    ) {
      return 'RUNNING';
    }
  }
  return direct;
}

function readRecordUpdatedAt(value: unknown): number {
  const record = asRecordStrict(value);
  const raw = record.updatedAt ?? record.updated_at;
  const numeric = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isFinite(numeric) ? numeric : 0;
}

/**
 * Merge two prediction-record maps keyed by `backend::smiles`, keeping the newer
 * record per key (updatedAt/updated_at wins; ties keep the incoming record).
 * Single shared implementation replacing the byte-identical local copies that
 * used to live in taskRowSync, useProjectTasksDataLoader and
 * useProjectDetailRuntimeContext.
 */
export function mergeLeadOptPredictionMapsByKey(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecordStrict(nextValue);
  const prev = asRecordStrict(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = { ...prev };
  for (const [key, nextRecord] of Object.entries(next)) {
    const prevRecord = merged[key];
    if (!prevRecord) {
      merged[key] = nextRecord;
      continue;
    }
    const nextUpdatedAt = readRecordUpdatedAt(nextRecord);
    const prevUpdatedAt = readRecordUpdatedAt(prevRecord);
    merged[key] = nextUpdatedAt >= prevUpdatedAt ? nextRecord : prevRecord;
  }
  return merged;
}


function normalizePredictionBackendStrict(value: unknown): string {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'boltz2') return 'boltz';
  if (token === 'boltz' || token === 'alphafold3' || token === 'protenix' || token === 'pocketxmol') return token;
  return '';
}





function extractPredictionMetricsFromStatusInfo(
  statusInfoInput: unknown,
  targetChain: string,
  ligandChain: string
): {
  pairIptm: number | null;
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  interfaceMetricSource: 'ipsae' | 'iptm' | 'none';
  pairPae: number | null;
  ligandPlddt: number | null;
  ligandAtomPlddts: number[];
  ligandRenderSmiles: string;
  ligandRenderAtomPlddts: number[];
  hasMetrics: boolean;
} {
  const statusInfo = asRecord(statusInfoInput);
  const compact = asRecord(statusInfo.lead_opt_metrics || statusInfo.compact_metrics || statusInfo.prediction_metrics);
  const confidence = asRecord(statusInfo.confidence);
  const affinity = asRecord(statusInfo.affinity);
  const candidateSmiles = asString(statusInfo.candidate_smiles ?? statusInfo.smiles ?? compact.smiles).trim();
  const renderPayload = extractPredictionRenderPayload(confidence, compact, ligandChain, candidateSmiles);
  const pairIptm =
    findPairIptm(confidence, targetChain, ligandChain) ??
    findPairIptm(affinity, targetChain, ligandChain) ??
    normalizeIptm(compact.pair_iptm ?? compact.pairIptm ?? compact.iptm);
  const preferredInterfaceMetric = resolveLeadOptPreferredInterfaceMetric({
    confidence,
    affinity,
    compact,
    pairIptm
  });
  const pairPae =
    findPairPae(confidence, targetChain, ligandChain) ??
    findPairPae(affinity, targetChain, ligandChain) ??
    normalizePae(
      compact.pair_pae ??
        compact.pairPae ??
        compact.pair_pde ??
        compact.pair_gpde ??
        compact.complex_pde ??
        compact.complex_pae ??
        compact.gpde ??
        compact.pae
    );
  const confidenceLigandAtomPlddts = findLigandAtomPlddts(confidence, ligandChain);
  const compactLigandAtomPlddts = normalizePlddtArray(compact.ligand_atom_plddts ?? compact.ligandAtomPlddts);
  const ligandAtomPlddts = confidenceLigandAtomPlddts.length > 0 ? confidenceLigandAtomPlddts : compactLigandAtomPlddts;
  const compactLigandPlddt = Number(compact.ligand_plddt ?? compact.ligandPlddt ?? compact.ligand_mean_plddt);
  const ligandPlddt =
    mean(ligandAtomPlddts) ??
    (Number.isFinite(compactLigandPlddt) ? normalizePlddtValue(compactLigandPlddt) : null);
  const hasMetrics = pairIptm !== null || pairPae !== null || ligandPlddt !== null || ligandAtomPlddts.length > 0;
  return {
    pairIptm,
    interfaceMetricValue: preferredInterfaceMetric.interfaceMetricValue,
    interfaceMetricLabel: preferredInterfaceMetric.interfaceMetricLabel,
    interfaceMetricSource: preferredInterfaceMetric.interfaceMetricSource,
    pairPae,
    ligandPlddt,
    ligandAtomPlddts,
    ligandRenderSmiles: renderPayload.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderPayload.ligandRenderAtomPlddts,
    hasMetrics
  };
}










function asRecord(value: unknown): Record<string, unknown> {
  return (value && typeof value === 'object' ? (value as Record<string, unknown>) : {});
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

function readExactRenderSmiles(payload: Record<string, unknown>): string {
  const candidates = [
    payload.ligand_display_smiles,
    payload.ligandDisplaySmiles,
    readObjectPath(payload, 'ligand.display_smiles'),
    readObjectPath(payload, 'ligandDisplaySmiles')
  ];
  for (const candidate of candidates) {
    const text = asString(candidate).trim();
    if (text) return text;
  }
  return '';
}

function readAlignedLigandSmiles(payload: Record<string, unknown>): string {
  const candidates = [
    payload.ligand_smiles,
    payload.ligandSmiles,
    readObjectPath(payload, 'ligand.smiles'),
    readObjectPath(payload, 'ligandSmiles')
  ];
  for (const candidate of candidates) {
    const text = asString(candidate).trim();
    if (text) return text;
  }
  return '';
}

function findLigandRenderAtomPlddts(payload: Record<string, unknown>, ligandChain: string): number[] {
  const preferred = String(ligandChain || '').trim();
  const byChainRaw =
    payload.ligand_display_atom_plddts_by_chain ??
    readObjectPath(payload, 'ligand.display_atom_plddts_by_chain') ??
    readObjectPath(payload, 'ligand_display.atom_plddts_by_chain');
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
  const direct = normalizePlddtArray(
    payload.ligand_display_atom_plddts ??
      payload.ligandDisplayAtomPlddts ??
      readObjectPath(payload, 'ligand.display_atom_plddts') ??
      readObjectPath(payload, 'ligandDisplayAtomPlddts')
  );
  if (direct.length > 0) return direct;
  return [];
}

function extractPredictionRenderPayload(
  confidence: Record<string, unknown>,
  compact: Record<string, unknown>,
  ligandChain: string,
  candidateSmilesInput?: unknown
): LeadOptPredictionRenderPayload {
  void candidateSmilesInput;
  const confidenceRenderSmiles = readExactRenderSmiles(confidence);
  const confidenceRenderAtomPlddts = findLigandRenderAtomPlddts(confidence, ligandChain);
  if (confidenceRenderSmiles && confidenceRenderAtomPlddts.length > 0) {
    return {
      ligandRenderSmiles: confidenceRenderSmiles,
      ligandRenderAtomPlddts: confidenceRenderAtomPlddts
    };
  }
  const compactRenderSmiles = readExactRenderSmiles(compact);
  const compactRenderAtomPlddts = findLigandRenderAtomPlddts(compact, ligandChain);
  if (compactRenderSmiles && compactRenderAtomPlddts.length > 0) {
    return {
      ligandRenderSmiles: compactRenderSmiles,
      ligandRenderAtomPlddts: compactRenderAtomPlddts
    };
  }
  const confidenceAlignedSmiles = readAlignedLigandSmiles(confidence);
  const confidenceAlignedAtomPlddts = findLigandAtomPlddts(confidence, ligandChain);
  if (confidenceAlignedSmiles && confidenceAlignedAtomPlddts.length > 0) {
    return {
      ligandRenderSmiles: confidenceAlignedSmiles,
      ligandRenderAtomPlddts: confidenceAlignedAtomPlddts
    };
  }
  const compactAlignedSmiles = readAlignedLigandSmiles(compact);
  const compactAlignedAtomPlddts = normalizePlddtArray(compact.ligand_atom_plddts ?? compact.ligandAtomPlddts);
  if (compactAlignedSmiles && compactAlignedAtomPlddts.length > 0) {
    return {
      ligandRenderSmiles: compactAlignedSmiles,
      ligandRenderAtomPlddts: compactAlignedAtomPlddts
    };
  }
  return {
    ligandRenderSmiles: '',
    ligandRenderAtomPlddts: []
  };
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
  const pairMapRaw = readObjectPath(confidence, 'pair_chains_iptm');
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
  // Pair-only fallback: when confidence only exposes a 2x2 numeric pair map and
  // chain labels are unavailable/mismatched, infer the off-diagonal pair directly.
  if (pairMapRaw && typeof pairMapRaw === 'object' && !Array.isArray(pairMapRaw)) {
    const keys = Object.keys(pairMapRaw).map((item) => String(item || '').trim()).filter(Boolean);
    if (keys.length === 2 && keys.every((item) => isNumericToken(item))) {
      const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
      const inferred = readPairValueFromNestedMap(pairMapRaw, first, second);
      if (inferred !== null) return normalizeIptm(inferred);
    }
  }
  const scalar = readFirstFiniteMetric(confidence, ['pair_iptm']);
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
  ligandChain: string,
  candidateSmilesInput?: unknown
): {
  pairIptm: number | null;
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  interfaceMetricSource: 'ipsae' | 'iptm' | 'none';
  pairPae: number | null;
  ligandPlddt: number | null;
  ligandAtomPlddts: number[];
  ligandRenderSmiles: string;
  ligandRenderAtomPlddts: number[];
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  structureName: string;
} {
  const confidence = asRecord(parsed?.confidence);
  const affinity = asRecord(parsed?.affinity);
  const candidateSmiles = asString(candidateSmilesInput).trim();
  const renderPayload = extractPredictionRenderPayload(confidence, asRecord({}), ligandChain, candidateSmiles);
  const pairIptm = findPairIptm(confidence, targetChain, ligandChain) ?? findPairIptm(affinity, targetChain, ligandChain);
  const preferredInterfaceMetric = resolveLeadOptPreferredInterfaceMetric({
    confidence,
    affinity,
    pairIptm
  });
  const pairPae = findPairPae(confidence, targetChain, ligandChain) ?? findPairPae(affinity, targetChain, ligandChain);
  const ligandAtomPlddts = findLigandAtomPlddts(confidence, ligandChain);
  const structureText = asString(parsed?.structureText);
  return {
    pairIptm,
    interfaceMetricValue: preferredInterfaceMetric.interfaceMetricValue,
    interfaceMetricLabel: preferredInterfaceMetric.interfaceMetricLabel,
    interfaceMetricSource: preferredInterfaceMetric.interfaceMetricSource,
    pairPae,
    ligandPlddt: mean(ligandAtomPlddts),
    ligandAtomPlddts,
    ligandRenderSmiles: renderPayload.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderPayload.ligandRenderAtomPlddts,
    structureText,
    structureFormat: asString(parsed?.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: asString(parsed?.structureName)
  };
}

export {
  resolveLeadOptPreferredInterfaceMetric,
  mapPredictionRuntimeState,
  inferPredictionRuntimeStateFromStatusPayload,
  normalizePredictionBackendStrict,
  extractPredictionMetricsFromStatusInfo,
  asString,
  asRecord,
  normalizePlddtValue,
  normalizePlddtArray,
  mean,
  readExactRenderSmiles,
  readAlignedLigandSmiles,
  findLigandRenderAtomPlddts,
  extractPredictionRenderPayload,
  findLigandAtomPlddts,
  normalizeIptm,
  normalizePae,
  uniqueChainHints,
  toChainList,
  chainVariants,
  chainTokenEquals,
  isNumericToken,
  readPairValueFromNestedMap,
  readPairValueFromNumericMap,
  readPairIptmForChainsFlexible,
  readPairPaeForChains,
  findPairIptm,
  findPairPae,
  extractPredictionResultPayload
};

export type { PredictionState, LeadOptPredictionRenderPayload };
