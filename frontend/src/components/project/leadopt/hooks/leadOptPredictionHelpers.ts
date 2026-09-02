import {
  readFirstFiniteMetric,
  readIpsaeDomMetric,
  readLigandIpsaeMaxMetric,
  readObjectPath,
  readPairIptmForChains,
  resolvePreferredInterfaceMetricFromValues
} from '../../../../pages/projectDetail/projectMetrics';
import type { ParsedResultBundle } from '../../../../types/models';

interface LeadOptPredictionRenderPayload {
  ligandRenderSmiles: string;
  ligandRenderAtomPlddts: number[];
}

type PredictionState = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE';
const LEADOPT_PREDICTION_RECORD_KEY_SEPARATOR = '::';
const RESULT_HYDRATION_RETRY_BASE_MS = 1200;
const RESULT_HYDRATION_RETRY_MAX_MS = 10000;
const RESULT_HYDRATION_MAX_RETRIES = 8;
const ENABLE_BACKGROUND_CANDIDATE_RESULT_HYDRATION = true;
const ENABLE_BACKGROUND_REFERENCE_RESULT_HYDRATION = true;
const RUNTIME_STATUS_RUNNING_POLL_DELAY_MS = 3500;
const RUNTIME_STATUS_QUEUED_POLL_DELAY_MS = 6500;
const RUNTIME_STATUS_IDLE_POLL_DELAY_MS = 12000;
const RUNTIME_STATUS_HIDDEN_POLL_DELAY_MULTIPLIER = 2;
const RUNTIME_STATUS_CANDIDATE_BATCH_SIZE = 1;
const RUNTIME_STATUS_REFERENCE_BATCH_SIZE = 1;
const RUNTIME_STATUS_MIN_TASK_REPOLL_GAP_MS = 2000;

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

function hasExactPredictionRenderContract(
  value: { ligandRenderSmiles?: string; ligandRenderAtomPlddts?: number[] } | null | undefined
): boolean {
  if (!value) return false;
  const renderSmiles = readText(value.ligandRenderSmiles).trim();
  return renderSmiles.length > 0 && Array.isArray(value.ligandRenderAtomPlddts) && value.ligandRenderAtomPlddts.length > 0;
}

function pickPredictionRenderContract(
  primary: { ligandRenderSmiles?: string; ligandRenderAtomPlddts?: number[] } | null | undefined,
  secondary: { ligandRenderSmiles?: string; ligandRenderAtomPlddts?: number[] } | null | undefined
): LeadOptPredictionRenderPayload {
  if (hasExactPredictionRenderContract(primary)) {
    return {
      ligandRenderSmiles: readText(primary?.ligandRenderSmiles).trim(),
      ligandRenderAtomPlddts: Array.isArray(primary?.ligandRenderAtomPlddts) ? primary!.ligandRenderAtomPlddts : []
    };
  }
  if (hasExactPredictionRenderContract(secondary)) {
    return {
      ligandRenderSmiles: readText(secondary?.ligandRenderSmiles).trim(),
      ligandRenderAtomPlddts: Array.isArray(secondary?.ligandRenderAtomPlddts) ? secondary!.ligandRenderAtomPlddts : []
    };
  }
  return {
    ligandRenderSmiles: '',
    ligandRenderAtomPlddts: []
  };
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
  const normalizedSmiles = readText(candidateSmilesInput).trim();
  if (!normalizedSmiles) return '';
  return `${backendKey}${LEADOPT_PREDICTION_RECORD_KEY_SEPARATOR}${encodeURIComponent(normalizedSmiles)}`;
}

export function parseLeadOptPredictionRecordKey(keyInput: unknown): { backend: string; smiles: string } {
  const key = readText(keyInput).trim();
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
  const normalizedPreferredInterfaceMetric = resolveLeadOptPreferredInterfaceMetric({
    compact: raw,
    pairIptm
  });
  const pairPae = normalizePae(
    raw.pairPae ?? raw.pair_pae ?? raw.pair_pde ?? raw.pair_gpde ?? raw.complex_pde ?? raw.complex_pae ?? raw.gpde ?? raw.pae
  );
  const ligandPlddtRaw = Number(raw.ligandPlddt ?? raw.ligand_plddt);
  const ligandPlddt = Number.isFinite(ligandPlddtRaw) ? normalizePlddtValue(ligandPlddtRaw) : null;
  const ligandAtomPlddts = normalizePlddtArray(raw.ligandAtomPlddts ?? raw.ligand_atom_plddts);
  const ligandRenderSmiles = readText(raw.ligandRenderSmiles ?? raw.ligand_render_smiles ?? raw.ligand_display_smiles).trim();
  const ligandRenderAtomPlddts = normalizePlddtArray(
    raw.ligandRenderAtomPlddts ?? raw.ligand_render_atom_plddts ?? raw.ligand_display_atom_plddts
  );
  const hasExactRenderContract = ligandRenderSmiles.length > 0 && ligandRenderAtomPlddts.length > 0;
  const hasResolvedMetrics = pairIptm !== null || pairPae !== null || ligandPlddt !== null || ligandAtomPlddts.length > 0;
  const pairIptmResolvedRaw = raw.pairIptmResolved ?? raw.pair_iptm_resolved;
  const resultBundleHydratedRaw = raw.resultBundleHydrated ?? raw.result_bundle_hydrated;
  return {
    taskId,
    state: normalizedState,
    backend: readText(raw.backend).trim().toLowerCase(),
    pairIptm,
    interfaceMetricValue:
      normalizeIptm(raw.interfaceMetricValue ?? raw.interface_metric_value) ?? normalizedPreferredInterfaceMetric.interfaceMetricValue,
    interfaceMetricLabel:
      readText(raw.interfaceMetricLabel ?? raw.interface_metric_label).trim() === 'ipTM'
        ? 'ipTM'
        : normalizedPreferredInterfaceMetric.interfaceMetricLabel,
    interfaceMetricSource:
      readText(raw.interfaceMetricSource ?? raw.interface_metric_source).trim().toLowerCase() === 'ipsae'
        ? 'ipsae'
        : readText(raw.interfaceMetricSource ?? raw.interface_metric_source).trim().toLowerCase() === 'iptm'
          ? 'iptm'
          : normalizedPreferredInterfaceMetric.interfaceMetricSource,
    pairPae,
    pairIptmResolved: pairIptmResolvedRaw === true && hasResolvedMetrics ? true : hasResolvedMetrics,
    ligandPlddt,
    ligandAtomPlddts,
    ...(hasExactRenderContract ? { ligandRenderSmiles, ligandRenderAtomPlddts } : {}),
    ...(structureText.trim()
      ? {
          structureText,
          structureFormat,
          structureName
        }
      : {}),
    resultBundleHydrated: resultBundleHydratedRaw === true || structureText.trim().length > 0,
    error: readText(raw.error),
    updatedAt: Number.isFinite(Number(raw.updatedAt ?? raw.updated_at))
      ? Number(raw.updatedAt ?? raw.updated_at)
      : 0
  };
}

function normalizePredictionMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [rawKey, rawRecord] of Object.entries(value as Record<string, unknown>)) {
    const parsedKey = parseLeadOptPredictionRecordKey(rawKey);
    const normalizedSmiles = readText(parsedKey.smiles).trim();
    if (!normalizedSmiles) continue;
    const record = normalizePredictionRecord(rawRecord);
    if (!record) continue;
    const backendFromKey = normalizePredictionBackendStrict(parsedKey.backend);
    // Candidate predictions are strictly keyed by `backend::smiles`.
    const normalizedBackend = backendFromKey;
    if (!normalizedBackend) continue;
    const canonicalKey = buildLeadOptPredictionRecordKey(normalizedBackend, normalizedSmiles);
    if (!canonicalKey) continue;
    const nextRecord: LeadOptPredictionRecord = {
      ...record,
      backend: normalizedBackend
    };
    const merged = mergePredictionRecordNonRegressive(out[canonicalKey], nextRecord);
    if (!merged) continue;
    out[canonicalKey] = merged;
  }
  return out;
}

function normalizeReferencePredictionMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [rawKey, rawRecord] of Object.entries(value as Record<string, unknown>)) {
    const record = normalizePredictionRecord(rawRecord);
    if (!record) continue;
    const backendFromKey = normalizePredictionBackendStrict(rawKey);
    // Reference predictions are strictly keyed by backend token only.
    const normalizedBackend = backendFromKey;
    if (!normalizedBackend) continue;
    const nextRecord: LeadOptPredictionRecord = {
      ...record,
      backend: normalizedBackend
    };
    const merged = mergePredictionRecordNonRegressive(out[normalizedBackend], nextRecord);
    if (!merged) continue;
    out[normalizedBackend] = merged;
  }
  return out;
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
  const resultFile = readText(info.result_file || info.resultFile).trim();
  if (resultFile) return 'SUCCESS';
  if (info.result && typeof info.result === 'object') return 'SUCCESS';
  const explicitError = readText(info.error || info.exc_message || info.exc_type).trim();
  if (explicitError) return 'FAILURE';
  const tracker = asRecord(info.tracker);
  const statusText = readText(info.status || info.message || tracker.details || tracker.status).trim().toLowerCase();
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

function normalizePredictionStateToken(value: unknown): PredictionState | null {
  const token = String(value || '').trim().toUpperCase();
  if (token === 'QUEUED' || token === 'RUNNING' || token === 'SUCCESS' || token === 'FAILURE') return token;
  return null;
}

function resolveNonRegressiveRuntimeState(
  currentStateInput: unknown,
  incomingState: PredictionState | null
): PredictionState | null {
  if (!incomingState) return null;
  const currentState = normalizePredictionStateToken(currentStateInput);
  if (!currentState) return incomingState;
  if (currentState === 'RUNNING' && incomingState === 'QUEUED') return 'RUNNING';
  if (currentState === 'SUCCESS' && (incomingState === 'QUEUED' || incomingState === 'RUNNING')) {
    return currentState;
  }
  return incomingState;
}

function predictionStatePriority(value: unknown): number {
  const state = normalizePredictionStateToken(value);
  if (state === 'SUCCESS' || state === 'FAILURE') return 3;
  if (state === 'RUNNING') return 2;
  if (state === 'QUEUED') return 1;
  return 0;
}

function readPredictionUpdatedAt(record: LeadOptPredictionRecord | null | undefined): number {
  const value = Number(record?.updatedAt ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function mergePredictionRecordNonRegressive(
  currentInput: LeadOptPredictionRecord | null | undefined,
  incomingInput: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  const current = currentInput || null;
  const incoming = incomingInput || null;
  if (!current && !incoming) return null;
  if (!current) return incoming;
  if (!incoming) return current;
  const currentTaskId = readText(current.taskId).trim();
  const incomingTaskId = readText(incoming.taskId).trim();
  if (currentTaskId && incomingTaskId && currentTaskId !== incomingTaskId) {
    const currentPriority = predictionStatePriority(current.state);
    const incomingPriority = predictionStatePriority(incoming.state);
    if (currentPriority !== incomingPriority) {
      return incomingPriority > currentPriority ? incoming : current;
    }
    const currentHasMetrics = hasResolvedPredictionMetrics(current) ? 1 : 0;
    const incomingHasMetrics = hasResolvedPredictionMetrics(incoming) ? 1 : 0;
    if (currentHasMetrics !== incomingHasMetrics) {
      return incomingHasMetrics > currentHasMetrics ? incoming : current;
    }
    return readPredictionUpdatedAt(incoming) >= readPredictionUpdatedAt(current) ? incoming : current;
  }
  const mergedState = resolveNonRegressiveRuntimeState(current.state, incoming.state) || current.state;
  const incomingHasMetrics = hasResolvedPredictionMetrics(incoming);
  const currentHasMetrics = hasResolvedPredictionMetrics(current);
  const renderContract = pickPredictionRenderContract(incoming, current);
  return {
    ...current,
    ...incoming,
    state: mergedState,
    backend: readText(incoming.backend).trim().toLowerCase() || readText(current.backend).trim().toLowerCase(),
    pairIptm: incoming.pairIptm ?? current.pairIptm,
    interfaceMetricValue:
      (current.interfaceMetricSource !== 'ipsae' && incoming.interfaceMetricSource === 'ipsae') ||
      current.interfaceMetricSource === 'none'
        ? incoming.interfaceMetricValue
        : incoming.interfaceMetricValue ?? current.interfaceMetricValue,
    interfaceMetricLabel:
      (current.interfaceMetricSource !== 'ipsae' && incoming.interfaceMetricSource === 'ipsae') ||
      current.interfaceMetricSource === 'none'
        ? incoming.interfaceMetricLabel
        : incoming.interfaceMetricLabel ?? current.interfaceMetricLabel,
    interfaceMetricSource:
      (current.interfaceMetricSource !== 'ipsae' && incoming.interfaceMetricSource === 'ipsae') ||
      current.interfaceMetricSource === 'none'
        ? incoming.interfaceMetricSource
        : incoming.interfaceMetricSource === 'none'
          ? current.interfaceMetricSource
          : incoming.interfaceMetricSource ?? current.interfaceMetricSource,
    pairPae: incoming.pairPae ?? current.pairPae,
    ligandPlddt: incoming.ligandPlddt ?? current.ligandPlddt,
    ligandAtomPlddts:
      Array.isArray(incoming.ligandAtomPlddts) && incoming.ligandAtomPlddts.length > 0
        ? incoming.ligandAtomPlddts
        : current.ligandAtomPlddts,
    ...(renderContract.ligandRenderSmiles ? { ligandRenderSmiles: renderContract.ligandRenderSmiles } : {}),
    ...(renderContract.ligandRenderAtomPlddts.length > 0 ? { ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts } : {}),
    structureText: readText(incoming.structureText).trim() ? incoming.structureText : current.structureText,
    structureFormat: readText(incoming.structureText).trim() ? incoming.structureFormat : current.structureFormat,
    structureName: readText(incoming.structureText).trim() ? incoming.structureName : current.structureName,
    resultBundleHydrated: incoming.resultBundleHydrated === true || current.resultBundleHydrated === true,
    error: readText(incoming.error).trim() || current.error,
    pairIptmResolved:
      incoming.pairIptmResolved === true ||
      current.pairIptmResolved === true ||
      incomingHasMetrics ||
      currentHasMetrics,
    updatedAt: Math.max(readPredictionUpdatedAt(current), readPredictionUpdatedAt(incoming))
  };
}

function mergePredictionRecordMapsNonRegressive(
  currentRecords: Record<string, LeadOptPredictionRecord>,
  incomingRecords: Record<string, LeadOptPredictionRecord>
): Record<string, LeadOptPredictionRecord> {
  if (!Object.keys(currentRecords).length) return incomingRecords;
  if (!Object.keys(incomingRecords).length) return currentRecords;
  const merged: Record<string, LeadOptPredictionRecord> = { ...currentRecords };
  for (const [key, incomingRecord] of Object.entries(incomingRecords)) {
    const nextRecord = mergePredictionRecordNonRegressive(merged[key], incomingRecord);
    if (nextRecord) merged[key] = nextRecord;
  }
  return merged;
}

function normalizePredictionBackendStrict(value: unknown): string {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'boltz2') return 'boltz';
  if (token === 'boltz' || token === 'alphafold3' || token === 'protenix' || token === 'pocketxmol') return token;
  return '';
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
  const normalizedBackend = normalizePredictionBackendStrict(backend);
  return {
    taskId,
    state: 'QUEUED',
    backend: normalizedBackend,
    pairIptm: null,
    interfaceMetricValue: null,
    interfaceMetricLabel: 'IPSAE',
    interfaceMetricSource: 'none',
    pairPae: null,
    pairIptmResolved: false,
    ligandPlddt: null,
    ligandAtomPlddts: [],
    resultBundleHydrated: false,
    error: '',
    updatedAt: Date.now()
  };
}

function hasResolvedPredictionMetrics(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  const pairIptm = typeof record.pairIptm === 'number' && Number.isFinite(record.pairIptm);
  const interfaceMetric = typeof record.interfaceMetricValue === 'number' && Number.isFinite(record.interfaceMetricValue);
  const pairPae = typeof record.pairPae === 'number' && Number.isFinite(record.pairPae);
  const ligandPlddt = typeof record.ligandPlddt === 'number' && Number.isFinite(record.ligandPlddt);
  const ligandAtomPlddts = Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
  const backend = normalizePredictionBackendStrict(record.backend);
  if (backend === 'alphafold3' && !ligandAtomPlddts) {
    return false;
  }
  return record.pairIptmResolved === true && (pairIptm || interfaceMetric || pairPae || ligandPlddt || ligandAtomPlddts);
}

function hasHydratedPredictionVisualization(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  const ligandAtomPlddts = Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
  if (!ligandAtomPlddts) return false;
  return hasExactPredictionRenderContract(record);
}

function hasHydratedPredictionIpsae(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (readText(record.interfaceMetricSource).trim().toLowerCase() !== 'ipsae') return false;
  return typeof record.interfaceMetricValue === 'number' && Number.isFinite(record.interfaceMetricValue);
}

function hasHydratedPredictionResult(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  return (
    record.resultBundleHydrated === true &&
    hasResolvedPredictionMetrics(record) &&
    hasHydratedPredictionVisualization(record) &&
    hasHydratedPredictionIpsae(record)
  );
}

function shouldProbeTaskStatus(
  tracker: Record<string, number>,
  taskIdInput: unknown,
  minGapMs = RUNTIME_STATUS_MIN_TASK_REPOLL_GAP_MS
): boolean {
  const taskId = readText(taskIdInput).trim();
  if (!taskId) return false;
  const now = Date.now();
  const last = Number(tracker[taskId] || 0);
  if (Number.isFinite(last) && now - last < minGapMs) return false;
  tracker[taskId] = now;
  return true;
}

function shouldHydratePredictionRecord(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (String(record.state || '').toUpperCase() !== 'SUCCESS') return false;
  const taskId = String(record.taskId || '').trim();
  if (!taskId || taskId.startsWith('local:')) return false;
  return !hasHydratedPredictionResult(record);
}

function isSyntheticStaleFailureMessage(error: unknown): boolean {
  const message = readText(error).trim().toLowerCase();
  return message.includes('runtime status became stale') || message.includes('stale after');
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
  const candidateSmiles = readText(statusInfo.candidate_smiles ?? statusInfo.smiles ?? compact.smiles).trim();
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

function computeHydrationRetryDelayMs(attempt: number): number {
  const safeAttempt = Math.max(0, Math.min(6, attempt));
  const delay = RESULT_HYDRATION_RETRY_BASE_MS * (2 ** safeAttempt);
  return Math.min(RESULT_HYDRATION_RETRY_MAX_MS, delay);
}

function unresolvedPredictionSort(
  left: [string, LeadOptPredictionRecord],
  right: [string, LeadOptPredictionRecord]
): number {
  const leftState = String(left[1]?.state || '').toUpperCase();
  const rightState = String(right[1]?.state || '').toUpperCase();
  const leftPriority = leftState === 'RUNNING' ? 0 : leftState === 'QUEUED' ? 1 : leftState === 'SUCCESS' ? 2 : 3;
  const rightPriority = rightState === 'RUNNING' ? 0 : rightState === 'QUEUED' ? 1 : rightState === 'SUCCESS' ? 2 : 3;
  if (leftPriority !== rightPriority) return leftPriority - rightPriority;
  return String(left[0] || '').localeCompare(String(right[0] || ''));
}

function buildPendingPredictionEntries(
  records: Record<string, LeadOptPredictionRecord>
): Array<[string, LeadOptPredictionRecord]> {
  return Object.entries(records)
    .filter(([, record]) => {
      const state = String(record.state || '').toUpperCase();
      const shouldPoll =
        state === 'QUEUED' ||
        state === 'RUNNING' ||
        (state === 'FAILURE' && isSyntheticStaleFailureMessage(record.error));
      if (!shouldPoll) return false;
      const taskId = readText(record.taskId).trim();
      return taskId.length > 0 && !taskId.startsWith('local:');
    })
    .sort(unresolvedPredictionSort);
}

function buildPendingPredictionSignature(entries: Array<[string, LeadOptPredictionRecord]>): string {
  if (!Array.isArray(entries) || entries.length === 0) return '';
  return entries
    .map(([key, record]) => {
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      return `${readText(key).trim()}|${taskId}|${state}`;
    })
    .join('||');
}

function computeRuntimePollDelayMs(options: { hasRunning: boolean; hasQueued: boolean }): number {
  if (options.hasRunning) return RUNTIME_STATUS_RUNNING_POLL_DELAY_MS;
  if (options.hasQueued) return RUNTIME_STATUS_QUEUED_POLL_DELAY_MS;
  return RUNTIME_STATUS_IDLE_POLL_DELAY_MS;
}

function computeRuntimePollBatchSize(totalPending: number, maxBatchSize: number): number {
  const safeTotal = Math.max(0, Math.floor(Number(totalPending) || 0));
  if (safeTotal <= 0) return 0;
  if (safeTotal <= 4) return 1;
  return Math.min(Math.max(1, maxBatchSize), Math.max(1, Math.ceil(safeTotal / 8)));
}

function sliceRoundRobin<T>(
  entries: T[],
  limit: number,
  cursor: number
): { items: T[]; nextCursor: number } {
  if (!Array.isArray(entries) || entries.length === 0 || limit <= 0) {
    return { items: [], nextCursor: 0 };
  }
  if (entries.length <= limit) {
    return { items: entries, nextCursor: 0 };
  }
  const safeCursor = ((cursor % entries.length) + entries.length) % entries.length;
  const out: T[] = [];
  for (let i = 0; i < Math.min(limit, entries.length); i += 1) {
    out.push(entries[(safeCursor + i) % entries.length]);
  }
  return {
    items: out,
    nextCursor: (safeCursor + limit) % entries.length
  };
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

function readExactRenderSmiles(payload: Record<string, unknown>): string {
  const candidates = [
    payload.ligand_display_smiles,
    payload.ligandDisplaySmiles,
    readObjectPath(payload, 'ligand.display_smiles'),
    readObjectPath(payload, 'ligandDisplaySmiles')
  ];
  for (const candidate of candidates) {
    const text = readText(candidate).trim();
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
    const text = readText(candidate).trim();
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
  const candidateSmiles = readText(candidateSmilesInput).trim();
  const renderPayload = extractPredictionRenderPayload(confidence, asRecord({}), ligandChain, candidateSmiles);
  const pairIptm = findPairIptm(confidence, targetChain, ligandChain) ?? findPairIptm(affinity, targetChain, ligandChain);
  const preferredInterfaceMetric = resolveLeadOptPreferredInterfaceMetric({
    confidence,
    affinity,
    pairIptm
  });
  const pairPae = findPairPae(confidence, targetChain, ligandChain) ?? findPairPae(affinity, targetChain, ligandChain);
  const ligandAtomPlddts = findLigandAtomPlddts(confidence, ligandChain);
  const structureText = readText(parsed?.structureText);
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
    structureFormat: readText(parsed?.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(parsed?.structureName)
  };
}

export {
  hasExactPredictionRenderContract,
  pickPredictionRenderContract,
  resolveLeadOptPreferredInterfaceMetric,
  normalizePredictionRecord,
  normalizePredictionMap,
  normalizeReferencePredictionMap,
  mapPredictionRuntimeState,
  inferPredictionRuntimeStateFromStatusPayload,
  normalizePredictionStateToken,
  resolveNonRegressiveRuntimeState,
  predictionStatePriority,
  readPredictionUpdatedAt,
  mergePredictionRecordNonRegressive,
  mergePredictionRecordMapsNonRegressive,
  normalizePredictionBackendStrict,
  summarizePredictionRecords,
  buildQueuedPredictionRecord,
  hasResolvedPredictionMetrics,
  hasHydratedPredictionVisualization,
  hasHydratedPredictionIpsae,
  hasHydratedPredictionResult,
  shouldProbeTaskStatus,
  shouldHydratePredictionRecord,
  isSyntheticStaleFailureMessage,
  extractPredictionMetricsFromStatusInfo,
  computeHydrationRetryDelayMs,
  unresolvedPredictionSort,
  buildPendingPredictionEntries,
  buildPendingPredictionSignature,
  computeRuntimePollDelayMs,
  computeRuntimePollBatchSize,
  sliceRoundRobin,
  readText,
  readNumber,
  readBoolean,
  formatMetric,
  sortScore,
  asRecord,
  asRecordArray,
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
  extractPredictionResultPayload,
  RESULT_HYDRATION_RETRY_BASE_MS,
  RESULT_HYDRATION_RETRY_MAX_MS,
  RESULT_HYDRATION_MAX_RETRIES,
  ENABLE_BACKGROUND_CANDIDATE_RESULT_HYDRATION,
  ENABLE_BACKGROUND_REFERENCE_RESULT_HYDRATION,
  RUNTIME_STATUS_RUNNING_POLL_DELAY_MS,
  RUNTIME_STATUS_QUEUED_POLL_DELAY_MS,
  RUNTIME_STATUS_IDLE_POLL_DELAY_MS,
  RUNTIME_STATUS_HIDDEN_POLL_DELAY_MULTIPLIER,
  RUNTIME_STATUS_CANDIDATE_BATCH_SIZE,
  RUNTIME_STATUS_REFERENCE_BATCH_SIZE,
  RUNTIME_STATUS_MIN_TASK_REPOLL_GAP_MS
};

export type { PredictionState, LeadOptPredictionRenderPayload };
