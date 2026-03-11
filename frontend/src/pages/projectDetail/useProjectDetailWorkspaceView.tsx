import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type { PredictionConstraint, ProjectTask } from '../../types/models';
import { downloadResultBlob, downloadResultFile, terminateTask as terminateBackendTask } from '../../api/backendApi';
import { createInputComponent } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';
import { ProjectDetailLayout } from './ProjectDetailLayout';
import {
  computeUseMsaFlag,
  filterConstraintsByBackend,
} from './projectDraftUtils';
import { useProjectResultDisplay } from './useProjectResultDisplay';
import { useProjectRunHandlers } from './useProjectRunHandlers';
import {
  constraintLabel,
  formatConstraintCombo as formatConstraintComboForWorkspace,
  formatConstraintDetail as formatConstraintDetailForWorkspace
} from './constraintWorkspaceUtils';
import { useConstraintWorkspaceActions } from './useConstraintWorkspaceActions';
import { scrollToEditorBlock } from './editorActions';
import { useProjectEditorHandlers } from './useProjectEditorHandlers';
import { useProjectSidebarActions } from './useProjectSidebarActions';
import { useProjectWorkflowSectionProps } from './useProjectWorkflowSectionProps';
import { useProjectRunState } from './useProjectRunState';
import { usePredictionWorkspaceProps } from './usePredictionWorkspaceProps';
import { useProjectDetailRuntimeContext } from './useProjectDetailRuntimeContext';
import { useAuth } from '../../hooks/useAuth';
import { buildLeadOptUploadSnapshotComponents, type LeadOptPersistedUploads } from './projectTaskSnapshot';
import {
  buildLeadOptCandidatesUiStateSignature,
  normalizeLeadOptCandidatesUiState,
  type LeadOptCandidatesUiState
} from '../../components/project/leadopt/LeadOptCandidatesPanel';
import {
  buildLeadOptPredictionRecordKey,
  parseLeadOptPredictionRecordKey,
  type LeadOptPredictionRecord
} from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';
import { readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function asPredictionRecordMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, LeadOptPredictionRecord>;
}

function normalizePredictionState(value: unknown): 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' {
  const token = readText(value).trim().toUpperCase();
  if (token === 'RUNNING' || token === 'SUCCESS' || token === 'FAILURE') return token;
  return 'QUEUED';
}

function predictionStatePriority(state: unknown): number {
  const normalized = normalizePredictionState(state);
  if (normalized === 'QUEUED') return 1;
  if (normalized === 'RUNNING') return 2;
  return 3;
}

function hasPredictionRecordMetrics(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (toFiniteNumber(record.pairIptm) !== null) return true;
  if (toFiniteNumber(record.pairPae) !== null) return true;
  if (toFiniteNumber(record.ligandPlddt) !== null) return true;
  return Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
}

function hasPredictionRecordStructure(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  return readText(record.structureText).trim().length > 0;
}

function hasExactPredictionRenderContract(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  const renderSmiles = readText(record.ligandRenderSmiles).trim();
  return renderSmiles.length > 0 && Array.isArray(record.ligandRenderAtomPlddts) && record.ligandRenderAtomPlddts.length > 0;
}

function pickPredictionRenderContract(
  primary: LeadOptPredictionRecord | null | undefined,
  secondary: LeadOptPredictionRecord | null | undefined
): { ligandRenderSmiles: string; ligandRenderAtomPlddts: number[] } {
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

function choosePreferredPredictionRecord(
  left: LeadOptPredictionRecord,
  right: LeadOptPredictionRecord
): LeadOptPredictionRecord {
  const leftPriority = predictionStatePriority(left.state);
  const rightPriority = predictionStatePriority(right.state);
  if (leftPriority !== rightPriority) {
    return rightPriority > leftPriority ? right : left;
  }
  const leftMetrics = hasPredictionRecordMetrics(left) ? 1 : 0;
  const rightMetrics = hasPredictionRecordMetrics(right) ? 1 : 0;
  if (leftMetrics !== rightMetrics) {
    return rightMetrics > leftMetrics ? right : left;
  }
  const leftStructure = hasPredictionRecordStructure(left) ? 1 : 0;
  const rightStructure = hasPredictionRecordStructure(right) ? 1 : 0;
  if (leftStructure !== rightStructure) {
    return rightStructure > leftStructure ? right : left;
  }
  const leftTs = Number.isFinite(Number(left.updatedAt)) ? Number(left.updatedAt) : 0;
  const rightTs = Number.isFinite(Number(right.updatedAt)) ? Number(right.updatedAt) : 0;
  if (leftTs !== rightTs) {
    return rightTs > leftTs ? right : left;
  }
  return readText(right.error).trim() ? right : left;
}

function mergePredictionRecordPair(
  fromConfidence: LeadOptPredictionRecord | null | undefined,
  fromProperties: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  if (!fromConfidence && !fromProperties) return null;
  if (!fromConfidence) return fromProperties || null;
  if (!fromProperties) return fromConfidence;
  const primary = choosePreferredPredictionRecord(fromConfidence, fromProperties);
  const secondary = primary === fromConfidence ? fromProperties : fromConfidence;
  const renderContract = pickPredictionRenderContract(primary, secondary);
  return {
    ...secondary,
    ...primary,
    taskId: readText(primary.taskId || secondary.taskId).trim(),
    state: normalizePredictionState(primary.state),
    backend: readText(primary.backend || secondary.backend).trim().toLowerCase(),
    pairIptm: toFiniteNumber(primary.pairIptm) ?? toFiniteNumber(secondary.pairIptm),
    pairPae: toFiniteNumber(primary.pairPae) ?? toFiniteNumber(secondary.pairPae),
    pairIptmResolved:
      primary.pairIptmResolved === true ||
      secondary.pairIptmResolved === true ||
      hasPredictionRecordMetrics(primary) ||
      hasPredictionRecordMetrics(secondary),
    ligandPlddt: toFiniteNumber(primary.ligandPlddt) ?? toFiniteNumber(secondary.ligandPlddt),
    ligandAtomPlddts: Array.isArray(primary.ligandAtomPlddts)
      ? primary.ligandAtomPlddts
      : Array.isArray(secondary.ligandAtomPlddts)
        ? secondary.ligandAtomPlddts
        : [],
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts,
    updatedAt: Math.max(
      Number.isFinite(Number(primary.updatedAt)) ? Number(primary.updatedAt) : 0,
      Number.isFinite(Number(secondary.updatedAt)) ? Number(secondary.updatedAt) : 0
    )
  };
}

function mergePredictionRecordMaps(
  confidenceInput: unknown,
  propertiesInput: unknown,
  preferredBackendInput?: unknown
): Record<string, LeadOptPredictionRecord> {
  const confidence = compactLeadOptPredictionMap(asPredictionRecordMap(confidenceInput), preferredBackendInput);
  const properties = compactLeadOptPredictionMap(asPredictionRecordMap(propertiesInput), preferredBackendInput);
  const merged: Record<string, LeadOptPredictionRecord> = {};
  const keys = new Set<string>([...Object.keys(confidence), ...Object.keys(properties)]);
  for (const key of keys) {
    const normalizedKey = readText(key).trim();
    if (!normalizedKey) continue;
    const mergedRecord = mergePredictionRecordPair(confidence[normalizedKey], properties[normalizedKey]);
    if (!mergedRecord) continue;
    merged[normalizedKey] = mergedRecord;
  }
  return merged;
}

function summarizeLeadOptPredictions(records: Record<string, LeadOptPredictionRecord>) {
  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  for (const record of Object.values(records)) {
    const token = String(record.state || '').toUpperCase();
    if (token === 'QUEUED') queued += 1;
    else if (token === 'RUNNING') running += 1;
    else if (token === 'SUCCESS') success += 1;
    else if (token === 'FAILURE') failure += 1;
  }
  return {
    total: Object.keys(records).length,
    queued,
    running,
    success,
    failure
  };
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Array<Record<string, unknown>>;
}

function toFiniteNumber(value: unknown): number | null {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(numeric)) return null;
  return numeric;
}

function normalizePlddtMetric(value: unknown): number | null {
  const numeric = toFiniteNumber(value);
  if (numeric === null) return null;
  const scaled = numeric >= 0 && numeric <= 1 ? numeric * 100 : numeric;
  if (!Number.isFinite(scaled)) return null;
  return Math.max(0, Math.min(100, scaled));
}

function compactLigandAtomPlddts(values: unknown): number[] {
  if (!Array.isArray(values)) return [];
  const out: number[] = [];
  for (const item of values) {
    const normalized = normalizePlddtMetric(item);
    if (normalized === null) continue;
    out.push(Math.round(normalized * 100) / 100);
    if (out.length >= 256) break;
  }
  return out;
}

function hydratePredictionRecordMetricsFromHistory(
  current: LeadOptPredictionRecord | null | undefined,
  historical: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  if (!current && !historical) return null;
  if (!current) return historical || null;
  if (!historical) return current;
  const renderContract = pickPredictionRenderContract(current, historical);
  return {
    ...current,
    pairIptm: toFiniteNumber(current.pairIptm) ?? toFiniteNumber(historical.pairIptm),
    pairPae: toFiniteNumber(current.pairPae) ?? toFiniteNumber(historical.pairPae),
    pairIptmResolved:
      current.pairIptmResolved === true ||
      historical.pairIptmResolved === true ||
      hasPredictionRecordMetrics(current) ||
      hasPredictionRecordMetrics(historical),
    ligandPlddt: toFiniteNumber(current.ligandPlddt) ?? toFiniteNumber(historical.ligandPlddt),
    ligandAtomPlddts:
      Array.isArray(current.ligandAtomPlddts) && current.ligandAtomPlddts.length > 0
        ? current.ligandAtomPlddts
        : Array.isArray(historical.ligandAtomPlddts)
          ? historical.ligandAtomPlddts
          : [],
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts,
    structureText: readText(current.structureText).trim() || readText(historical.structureText).trim(),
    structureFormat:
      readText(current.structureText).trim()
        ? readText(current.structureFormat).toLowerCase() === 'pdb'
          ? 'pdb'
          : 'cif'
        : readText(historical.structureFormat).toLowerCase() === 'pdb'
          ? 'pdb'
          : readText(current.structureFormat).toLowerCase() === 'pdb'
            ? 'pdb'
            : 'cif',
    structureName: readText(current.structureName).trim() || readText(historical.structureName).trim(),
    updatedAt: Math.max(
      Number.isFinite(Number(current.updatedAt)) ? Number(current.updatedAt) : 0,
      Number.isFinite(Number(historical.updatedAt)) ? Number(historical.updatedAt) : 0
    )
  };
}

function hydratePredictionRecordMapFromHistory(
  currentInput: unknown,
  historicalInput: unknown
): Record<string, LeadOptPredictionRecord> {
  const current = asPredictionRecordMap(currentInput);
  const historical = asPredictionRecordMap(historicalInput);
  const out: Record<string, LeadOptPredictionRecord> = {};
  const keys = new Set([...Object.keys(historical), ...Object.keys(current)]);
  for (const key of keys) {
    const next = hydratePredictionRecordMetricsFromHistory(current[key], historical[key]);
    if (!next) continue;
    out[key] = next;
  }
  return out;
}

function readBooleanToken(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  const token = readText(value).trim().toLowerCase();
  if (!token) return null;
  if (token === '1' || token === 'true' || token === 'yes' || token === 'on') return true;
  if (token === '0' || token === 'false' || token === 'no' || token === 'off') return false;
  return null;
}

function normalizePredictionBackendStrict(value: unknown): string {
  const token = readText(value).trim().toLowerCase();
  if (token === 'boltz2') return 'boltz';
  if (token === 'boltz' || token === 'alphafold3' || token === 'protenix' || token === 'pocketxmol') return token;
  return '';
}

const LEAD_OPT_UI_STATE_STORAGE_KEY = 'vbio:lead_opt:results_ui_state:v1';
const SESSION_KEY = 'vbio_session';

function readSessionIdentityFromLocalStorage(): string {
  if (typeof window === 'undefined') return '';
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (!raw) return '';
    const payload = JSON.parse(raw) as Record<string, unknown>;
    const userId = readText(payload.userId).trim();
    const username = readText(payload.username).trim().toLowerCase();
    return userId || username;
  } catch {
    return '';
  }
}

function buildLeadOptUiStateScopeKey(params: {
  sessionIdentity: string;
  projectId: string;
  taskRowId: string;
  queryId: string;
}): string {
  const sessionIdentity = readText(params.sessionIdentity).trim().toLowerCase();
  const projectId = readText(params.projectId).trim();
  const taskRowId = readText(params.taskRowId).trim();
  const queryId = readText(params.queryId).trim();
  if (!sessionIdentity || !projectId || !taskRowId) return '';
  return [sessionIdentity, projectId, taskRowId, queryId || '__query__'].join('|');
}

function readLeadOptUiStateStoreFromLocal(): Record<string, unknown> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(LEAD_OPT_UI_STATE_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return asRecord(parsed);
  } catch {
    return {};
  }
}

function readLeadOptUiStateFromLocal(scopeKey: string): LeadOptCandidatesUiState | null {
  const normalizedScopeKey = readText(scopeKey).trim();
  if (!normalizedScopeKey) return null;
  const store = readLeadOptUiStateStoreFromLocal();
  const payload = store[normalizedScopeKey];
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  return compactLeadOptCandidatesUiState(normalizeLeadOptCandidatesUiState(payload, 'pocketxmol'));
}

function writeLeadOptUiStateToLocal(scopeKey: string, uiState: LeadOptCandidatesUiState): void {
  if (typeof window === 'undefined') return;
  const normalizedScopeKey = readText(scopeKey).trim();
  if (!normalizedScopeKey) return;
  const store = readLeadOptUiStateStoreFromLocal();
  const compactUiState = compactLeadOptCandidatesUiState(uiState);
  const nextStore = {
    ...store,
    [normalizedScopeKey]: compactUiState
  };
  try {
    window.localStorage.setItem(LEAD_OPT_UI_STATE_STORAGE_KEY, JSON.stringify(nextStore));
  } catch {
    // Ignore local storage write failures (quota / privacy mode).
  }
}

function compactLeadOptPredictionRecord(value: LeadOptPredictionRecord): LeadOptPredictionRecord {
  const backend = normalizePredictionBackendStrict(value.backend);
  const renderContract = pickPredictionRenderContract(value, null);
  return {
    taskId: readText(value.taskId).trim(),
    state: value.state,
    backend,
    pairIptm: toFiniteNumber(value.pairIptm),
    pairPae: toFiniteNumber(value.pairPae),
    pairIptmResolved: value.pairIptmResolved === true,
    ligandPlddt: normalizePlddtMetric(value.ligandPlddt),
    ligandAtomPlddts: compactLigandAtomPlddts(value.ligandAtomPlddts),
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: compactLigandAtomPlddts(renderContract.ligandRenderAtomPlddts),
    structureText: '',
    structureFormat: readText(value.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(value.structureName).trim(),
    error: readText(value.error),
    updatedAt: Number.isFinite(Number(value.updatedAt)) ? Number(value.updatedAt) : 0
  };
}

function compactLeadOptPredictionMap(
  value: Record<string, LeadOptPredictionRecord>,
  _preferredBackendInput?: unknown
): Record<string, LeadOptPredictionRecord> {
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [rawKey, record] of Object.entries(value)) {
    const normalizedRawKey = readText(rawKey).trim();
    const parsedKey = parseLeadOptPredictionRecordKey(rawKey);
    const backendFromKey = normalizePredictionBackendStrict(parsedKey.backend);
    const backendFromRawKey = normalizePredictionBackendStrict(normalizedRawKey);
    const parsedSmiles = readText(parsedKey.smiles).trim();

    // Reference prediction map uses backend-only keys.
    if (!backendFromKey && backendFromRawKey && parsedSmiles.toLowerCase() === backendFromRawKey) {
      const compactRecord = compactLeadOptPredictionRecord({
        ...record,
        backend: backendFromRawKey
      });
      const merged = mergePredictionRecordPair(out[backendFromRawKey], compactRecord);
      if (!merged) continue;
      out[backendFromRawKey] = merged;
      continue;
    }

    if (!parsedSmiles) continue;
    // Candidate prediction map uses `backend::smiles` keys only.
    const backend = backendFromKey;
    if (!backend) continue;
    const key = buildLeadOptPredictionRecordKey(backend, parsedSmiles);
    if (!key) continue;
    const compactRecord = compactLeadOptPredictionRecord({
      ...record,
      backend
    });
    const merged = mergePredictionRecordPair(out[key], compactRecord);
    if (!merged) continue;
    out[key] = merged;
  }
  return out;
}

function compactLeadOptEnumeratedCandidateRow(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const smiles = readText(row.smiles || row.candidate_smiles || row.predicted_smiles).trim();
  if (!smiles) return null;
  const nPairs = toFiniteNumber(row.n_pairs);
  const medianDelta = toFiniteNumber(row.median_delta);
  const propertiesRaw = asRecord(row.properties);
  const propertyDeltasRaw = asRecord(row.property_deltas);
  const properties: Record<string, unknown> = {};
  const propertyDeltas: Record<string, unknown> = {};
  const mw = toFiniteNumber(propertiesRaw.molecular_weight);
  const logp = toFiniteNumber(propertiesRaw.logp);
  const tpsa = toFiniteNumber(propertiesRaw.tpsa);
  const deltaMw = toFiniteNumber(propertyDeltasRaw.mw);
  const deltaLogp = toFiniteNumber(propertyDeltasRaw.logp);
  const deltaTpsa = toFiniteNumber(propertyDeltasRaw.tpsa);
  if (mw !== null) properties.molecular_weight = mw;
  if (logp !== null) properties.logp = logp;
  if (tpsa !== null) properties.tpsa = tpsa;
  if (deltaMw !== null) propertyDeltas.mw = deltaMw;
  if (deltaLogp !== null) propertyDeltas.logp = deltaLogp;
  if (deltaTpsa !== null) propertyDeltas.tpsa = deltaTpsa;
  const highlightAtomIndices = Array.isArray(row.final_highlight_atom_indices)
    ? Array.from(
        new Set(
          row.final_highlight_atom_indices
            .map((item) => Number(item))
            .filter((item) => Number.isFinite(item) && item >= 0)
            .map((item) => Math.floor(item))
        )
      )
    : [];
  const constantSmiles = readText(row.constant_smiles).trim();
  return {
    smiles,
    ...(nPairs === null ? {} : { n_pairs: nPairs }),
    ...(medianDelta === null ? {} : { median_delta: medianDelta }),
    properties,
    property_deltas: propertyDeltas,
    final_highlight_atom_indices: highlightAtomIndices,
    ...(constantSmiles ? { constant_smiles: constantSmiles } : {})
  };
}

function compactLeadOptEnumeratedCandidates(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  const rows: Array<Record<string, unknown>> = [];
  for (const item of value) {
    const compact = compactLeadOptEnumeratedCandidateRow(item);
    if (!compact) continue;
    rows.push(compact);
  }
  return rows;
}

function readLeadOptStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => readText(item).trim())
        .filter(Boolean)
    )
  );
}

function readLeadOptIntegerArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item) && item >= 0)
        .map((item) => Math.floor(item))
    )
  );
}

function compactLeadOptVariableItems(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const record = asRecord(item);
      const query = readText(record.query).trim();
      const fragmentId = readText(record.fragment_id).trim();
      const atomIndices = readLeadOptIntegerArray(record.atom_indices);
      if (!query && !fragmentId && atomIndices.length === 0) return null;
      return {
        query,
        mode: readText(record.mode).trim() || 'substructure',
        fragment_id: fragmentId,
        atom_indices: atomIndices
      } as Record<string, unknown>;
    })
    .filter((item): item is Record<string, unknown> => Boolean(item));
}

function compactLeadOptQueryPayload(value: unknown): Record<string, unknown> {
  const payload = asRecord(value);
  const variableSpec = asRecord(payload.variable_spec);
  return {
    query_mol: readText(payload.query_mol).trim(),
    variable_spec: {
      mode: readText(variableSpec.mode).trim() || 'substructure',
      items: compactLeadOptVariableItems(variableSpec.items)
    },
    selected_fragment_ids: readLeadOptStringArray(payload.selected_fragment_ids),
    selected_fragment_atom_indices: readLeadOptIntegerArray(payload.selected_fragment_atom_indices),
    constant_spec: asRecord(payload.constant_spec),
    property_targets: asRecord(payload.property_targets),
    mmp_database_id: readText(payload.mmp_database_id).trim(),
    mmp_database_label: readText(payload.mmp_database_label).trim(),
    mmp_database_schema: readText(payload.mmp_database_schema).trim(),
    query_mode: readText(payload.query_mode).trim(),
    aggregation_type: readText(payload.aggregation_type).trim(),
    grouped_by_environment: readBooleanToken(payload.grouped_by_environment),
    min_pairs: toFiniteNumber(payload.min_pairs),
    rule_env_radius: toFiniteNumber(payload.rule_env_radius),
    max_results: toFiniteNumber(payload.max_results)
  };
}

function buildLeadOptListMeta(leadOptMmpInput: unknown): Record<string, unknown> {
  const leadOptMmp = asRecord(leadOptMmpInput);
  const queryResult = asRecord(leadOptMmp.query_result);
  const selection = asRecord(leadOptMmp.selection);
  const predictionSummary = asRecord(leadOptMmp.prediction_summary);
  const predictionMap = asRecord(leadOptMmp.prediction_by_smiles);
  const compactCandidates = compactLeadOptEnumeratedCandidates(
    leadOptMmp.enumerated_candidates ?? asRecord(leadOptMmp.result_snapshot).enumerated_candidates
  );
  const compactQueryResult = {
    query_id: readText(leadOptMmp.query_id || queryResult.query_id).trim(),
    task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
    query_mode: readText(queryResult.query_mode).trim() || 'one-to-many',
    aggregation_type: readText(queryResult.aggregation_type).trim(),
    property_targets: asRecord(queryResult.property_targets),
    rule_env_radius: toFiniteNumber(queryResult.rule_env_radius),
    grouped_by_environment: readBooleanToken(queryResult.grouped_by_environment),
    mmp_database_id: readText(leadOptMmp.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOptMmp.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    cluster_group_by: readText(queryResult.cluster_group_by).trim(),
    min_pairs: toFiniteNumber(queryResult.min_pairs),
    stats: asRecord(queryResult.stats)
  } as Record<string, unknown>;
  const predictionTotal = toFiniteNumber(predictionSummary.total);
  const selectedFragmentIds = readLeadOptStringArray(
    selection.selected_fragment_ids ?? leadOptMmp.selected_fragment_ids
  );
  const selectedFragmentAtomIndices = readLeadOptIntegerArray(
    selection.selected_fragment_atom_indices ?? leadOptMmp.selected_fragment_atom_indices
  );
  const variableItems = compactLeadOptVariableItems(selection.variable_items ?? leadOptMmp.variable_items);
  const selectedFragmentQuery =
    readLeadOptStringArray(selection.variable_queries ?? leadOptMmp.variable_queries)[0] ||
    readText(leadOptMmp.selected_fragment_query).trim();
  const directionToken = readText(selection.direction ?? leadOptMmp.direction).trim().toLowerCase();
  const direction = directionToken === 'increase' || directionToken === 'decrease' ? directionToken : '';
  return {
    stage: readText(leadOptMmp.stage).trim(),
    prediction_stage: readText(leadOptMmp.prediction_stage).trim(),
    query_id: readText(leadOptMmp.query_id || queryResult.query_id).trim(),
    task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
    transform_count: toFiniteNumber(leadOptMmp.transform_count),
    candidate_count: toFiniteNumber(leadOptMmp.candidate_count),
    bucket_count:
      toFiniteNumber(leadOptMmp.bucket_count) ??
      predictionTotal ??
      Object.keys(predictionMap).length,
    mmp_database_id: readText(leadOptMmp.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOptMmp.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    selection: {
      selected_fragment_ids: selectedFragmentIds,
      selected_fragment_atom_indices: selectedFragmentAtomIndices,
      variable_queries: selectedFragmentQuery ? [selectedFragmentQuery] : [],
      variable_items: variableItems,
      grouped_by_environment_mode: readText(selection.grouped_by_environment_mode).trim().toLowerCase(),
      query_property: readText(selection.query_property).trim(),
      direction
    },
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    selected_fragment_query: selectedFragmentQuery,
    query_payload: compactLeadOptQueryPayload(leadOptMmp.query_payload),
    prediction_summary: {
      total: predictionTotal,
      queued: toFiniteNumber(predictionSummary.queued),
      running: toFiniteNumber(predictionSummary.running),
      success: toFiniteNumber(predictionSummary.success),
      failure: toFiniteNumber(predictionSummary.failure)
    },
    query_result: compactQueryResult,
    enumerated_candidates: compactCandidates,
    target_chain: readText(leadOptMmp.target_chain).trim(),
    ligand_chain: readText(leadOptMmp.ligand_chain).trim()
  };
}

function buildLeadOptStateMeta(leadOptInput: unknown): Record<string, unknown> {
  const leadOpt = asRecord(leadOptInput);
  const predictionSummary = asRecord(leadOpt.prediction_summary);
  const selectedBackend = normalizePredictionBackendStrict(leadOpt.selected_backend);
  return {
    stage: readText(leadOpt.stage).trim(),
    prediction_stage: readText(leadOpt.prediction_stage).trim(),
    query_id: readText(leadOpt.query_id || asRecord(leadOpt.query_result).query_id).trim(),
    task_id: readText(leadOpt.task_id || asRecord(leadOpt.query_result).task_id).trim(),
    prediction_task_id: readText(leadOpt.prediction_task_id).trim(),
    prediction_candidate_smiles: readText(leadOpt.prediction_candidate_smiles).trim(),
    prediction_summary: {
      total: toFiniteNumber(predictionSummary.total) ?? 0,
      queued: toFiniteNumber(predictionSummary.queued) ?? 0,
      running: toFiniteNumber(predictionSummary.running) ?? 0,
      success: toFiniteNumber(predictionSummary.success) ?? 0,
      failure: toFiniteNumber(predictionSummary.failure) ?? 0,
      latest_task_id: readText(predictionSummary.latest_task_id).trim()
    },
    prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.prediction_by_smiles)),
    reference_prediction_by_backend: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.reference_prediction_by_backend)),
    ...(selectedBackend ? { selected_backend: selectedBackend } : {}),
    target_chain: readText(leadOpt.target_chain).trim(),
    ligand_chain: readText(leadOpt.ligand_chain).trim()
  };
}

function mergeLeadOptStateMetaIntoProperties(
  propertiesInput: unknown,
  leadOptInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_state: buildLeadOptStateMeta(leadOptInput)
  };
}

function mergeLeadOptMetaIntoProperties(
  propertiesInput: unknown,
  leadOptInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_list: buildLeadOptListMeta(leadOptInput),
    lead_opt_state: buildLeadOptStateMeta(leadOptInput)
  };
}

function compactLeadOptForConfidenceWrite(leadOptInput: unknown): Record<string, unknown> {
  const leadOpt = asRecord(leadOptInput);
  const queryResult = asRecord(leadOpt.query_result);
  const predictionSummary = asRecord(leadOpt.prediction_summary);
  const compactPredictionSummary = {
    total: toFiniteNumber(predictionSummary.total) ?? 0,
    queued: toFiniteNumber(predictionSummary.queued) ?? 0,
    running: toFiniteNumber(predictionSummary.running) ?? 0,
    success: toFiniteNumber(predictionSummary.success) ?? 0,
    failure: toFiniteNumber(predictionSummary.failure) ?? 0,
    latest_task_id: readText(predictionSummary.latest_task_id).trim()
  };
  return {
    stage: readText(leadOpt.stage).trim(),
    prediction_stage: readText(leadOpt.prediction_stage).trim(),
    query_id: readText(leadOpt.query_id || queryResult.query_id).trim(),
    task_id: readText(leadOpt.task_id || queryResult.task_id).trim(),
    transform_count: toFiniteNumber(leadOpt.transform_count),
    candidate_count: toFiniteNumber(leadOpt.candidate_count),
    bucket_count: toFiniteNumber(leadOpt.bucket_count),
    mmp_database_id: readText(leadOpt.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOpt.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOpt.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    target_chain: readText(leadOpt.target_chain).trim(),
    ligand_chain: readText(leadOpt.ligand_chain).trim(),
    prediction_summary: compactPredictionSummary,
    prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.prediction_by_smiles)),
    reference_prediction_by_backend: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.reference_prediction_by_backend))
  };
}

function buildLeadOptPredictionPersistSignature(records: Record<string, LeadOptPredictionRecord>): string {
  return Object.entries(records)
    .map(([key, record]) => {
      const normalizedKey = readText(key).trim();
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      const backend = readText(record.backend).trim().toLowerCase();
      const pairIptm = toFiniteNumber(record.pairIptm);
      const pairPae = toFiniteNumber(record.pairPae);
      const ligandPlddt = normalizePlddtMetric(record.ligandPlddt);
      const atomPlddts = compactLigandAtomPlddts(record.ligandAtomPlddts);
      const atomPlddtSignature = atomPlddts.length > 0
        ? `${atomPlddts.length}:${atomPlddts[0]?.toFixed(2) || ''}:${atomPlddts[atomPlddts.length - 1]?.toFixed(2) || ''}`
        : '';
      const error = readText(record.error).trim();
      return [
        normalizedKey,
        taskId,
        state,
        backend,
        pairIptm === null ? '' : pairIptm.toFixed(4),
        pairPae === null ? '' : pairPae.toFixed(3),
        record.pairIptmResolved === true ? '1' : '0',
        ligandPlddt === null ? '' : ligandPlddt.toFixed(3),
        atomPlddtSignature,
        error
      ].join('~');
    })
    .sort((a, b) => a.localeCompare(b))
    .join('||');
}

function readLeadOptPersistRecordUpdatedAt(value: unknown): number {
  const record = asRecord(value);
  const raw = record.updatedAt ?? record.updated_at;
  const numeric = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isFinite(numeric) ? numeric : 0;
}

function mergeLeadOptPersistRecordMap(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = { ...prev };
  for (const [key, nextRecord] of Object.entries(next)) {
    const prevRecord = merged[key];
    if (!prevRecord) {
      merged[key] = nextRecord;
      continue;
    }
    const nextUpdatedAt = readLeadOptPersistRecordUpdatedAt(nextRecord);
    const prevUpdatedAt = readLeadOptPersistRecordUpdatedAt(prevRecord);
    merged[key] = nextUpdatedAt >= prevUpdatedAt ? nextRecord : prevRecord;
  }
  return merged;
}

function mergeLeadOptStateForPersist(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  return {
    ...prev,
    ...next,
    prediction_by_smiles: mergeLeadOptPersistRecordMap(next.prediction_by_smiles, prev.prediction_by_smiles),
    reference_prediction_by_backend: mergeLeadOptPersistRecordMap(
      next.reference_prediction_by_backend,
      prev.reference_prediction_by_backend
    )
  };
}

function mergeLeadOptSnapshotForPersist(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  return {
    ...prev,
    ...next,
    query_result:
      Object.keys(asRecord(next.query_result)).length > 0
        ? asRecord(next.query_result)
        : asRecord(prev.query_result),
    selection:
      Object.keys(asRecord(next.selection)).length > 0
        ? asRecord(next.selection)
        : asRecord(prev.selection),
    query_payload:
      Object.keys(asRecord(next.query_payload)).length > 0
        ? asRecord(next.query_payload)
        : asRecord(prev.query_payload),
    enumerated_candidates:
      Array.isArray(next.enumerated_candidates) && next.enumerated_candidates.length > 0
        ? compactLeadOptEnumeratedCandidates(next.enumerated_candidates)
        : Array.isArray(prev.enumerated_candidates)
          ? compactLeadOptEnumeratedCandidates(prev.enumerated_candidates)
          : [],
    prediction_by_smiles: mergeLeadOptPersistRecordMap(next.prediction_by_smiles, prev.prediction_by_smiles),
    reference_prediction_by_backend: mergeLeadOptPersistRecordMap(
      next.reference_prediction_by_backend,
      prev.reference_prediction_by_backend
    )
  };
}

function mergeLeadOptPatchPayloadForPersist(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = {
    ...prev,
    ...next
  };
  const nextProperties = asRecord(next.properties);
  const prevProperties = asRecord(prev.properties);
  if (Object.keys(nextProperties).length > 0 || Object.keys(prevProperties).length > 0) {
    merged.properties = {
      ...prevProperties,
      ...nextProperties,
      lead_opt_state: mergeLeadOptStateForPersist(nextProperties.lead_opt_state, prevProperties.lead_opt_state)
    };
  }
  const nextConfidence = asRecord(next.confidence);
  const prevConfidence = asRecord(prev.confidence);
  if (Object.keys(nextConfidence).length > 0 || Object.keys(prevConfidence).length > 0) {
    merged.confidence = {
      ...prevConfidence,
      ...nextConfidence,
      lead_opt_mmp: mergeLeadOptStateForPersist(nextConfidence.lead_opt_mmp, prevConfidence.lead_opt_mmp)
    };
  }
  return merged;
}

function compactLeadOptCandidatesUiState(value: LeadOptCandidatesUiState): LeadOptCandidatesUiState {
  return {
    selectedBackend: readText(value.selectedBackend).trim().toLowerCase() || 'pocketxmol',
    stateFilter: value.stateFilter,
    showAdvanced: value.showAdvanced === true,
    mwMin: readText(value.mwMin).trim(),
    mwMax: readText(value.mwMax).trim(),
    logpMin: readText(value.logpMin).trim(),
    logpMax: readText(value.logpMax).trim(),
    tpsaMin: readText(value.tpsaMin).trim(),
    tpsaMax: readText(value.tpsaMax).trim(),
    plddtMin: readText(value.plddtMin).trim(),
    plddtMax: readText(value.plddtMax).trim(),
    iptmMin: readText(value.iptmMin).trim(),
    iptmMax: readText(value.iptmMax).trim(),
    paeMin: readText(value.paeMin).trim(),
    paeMax: readText(value.paeMax).trim(),
    structureSearchMode: value.structureSearchMode,
    structureSearchQuery: readText(value.structureSearchQuery).trim(),
    previewRenderMode: value.previewRenderMode
  };
}

function resolveLeadOptSnapshotFromTask(taskInput: unknown): Record<string, unknown> {
  const task = asRecord(taskInput);
  const properties = asRecord(task.properties);
  const fromProperties = asRecord(properties.lead_opt_list);
  const fromPropertiesState = asRecord(properties.lead_opt_state);
  const confidence = asRecord(task.confidence);
  const fromConfidence = asRecord(confidence.lead_opt_mmp);
  if (
    Object.keys(fromProperties).length === 0 &&
    Object.keys(fromPropertiesState).length === 0 &&
    Object.keys(fromConfidence).length === 0
  ) {
    return {};
  }

  const propertiesQueryResult = asRecord(fromProperties.query_result);
  const confidenceQueryResult = asRecord(fromConfidence.query_result);
  const propertiesSelection = asRecord(fromProperties.selection);
  const confidenceSelection = asRecord(fromConfidence.selection);
  const stateSelectedBackend = normalizePredictionBackendStrict(
    fromPropertiesState.selected_backend ?? fromConfidence.selected_backend
  );
  const preferredPredictionBackend = stateSelectedBackend;
  const mergedPredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(fromConfidence.prediction_by_smiles, fromProperties.prediction_by_smiles, preferredPredictionBackend),
    fromPropertiesState.prediction_by_smiles,
    preferredPredictionBackend
  );
  const mergedReferencePredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(
      fromConfidence.reference_prediction_by_backend,
      fromProperties.reference_prediction_by_backend,
      preferredPredictionBackend
    ),
    fromPropertiesState.reference_prediction_by_backend,
    preferredPredictionBackend
  );

  return {
    ...fromConfidence,
    ...fromProperties,
    query_result:
      Object.keys(propertiesQueryResult).length > 0
        ? propertiesQueryResult
        : confidenceQueryResult,
    enumerated_candidates:
      Array.isArray(fromProperties.enumerated_candidates) && fromProperties.enumerated_candidates.length > 0
        ? fromProperties.enumerated_candidates
        : Array.isArray(fromConfidence.enumerated_candidates)
          ? fromConfidence.enumerated_candidates
          : [],
    prediction_by_smiles: mergedPredictions,
    reference_prediction_by_backend: mergedReferencePredictions,
    ...(stateSelectedBackend ? { selected_backend: stateSelectedBackend } : {}),
    stage: readText(fromPropertiesState.stage).trim() || readText(fromProperties.stage).trim() || readText(fromConfidence.stage).trim(),
    prediction_stage:
      readText(fromPropertiesState.prediction_stage).trim() ||
      readText(fromProperties.prediction_stage).trim() ||
      readText(fromConfidence.prediction_stage).trim(),
    prediction_summary:
      Object.keys(asRecord(fromPropertiesState.prediction_summary)).length > 0
        ? asRecord(fromPropertiesState.prediction_summary)
        : Object.keys(asRecord(fromProperties.prediction_summary)).length > 0
          ? asRecord(fromProperties.prediction_summary)
          : asRecord(fromConfidence.prediction_summary),
    selection:
      Object.keys(propertiesSelection).length > 0
        ? propertiesSelection
        : confidenceSelection,
    ui_state: {},
    query_id:
      readText(fromProperties.query_id || propertiesQueryResult.query_id).trim() ||
      readText(fromPropertiesState.query_id).trim() ||
      readText(fromConfidence.query_id || confidenceQueryResult.query_id).trim(),
    task_id:
      readText(fromProperties.task_id || propertiesQueryResult.task_id).trim() ||
      readText(fromPropertiesState.task_id).trim() ||
      readText(fromConfidence.task_id || confidenceQueryResult.task_id).trim(),
    target_chain: readText(fromPropertiesState.target_chain || fromProperties.target_chain || fromConfidence.target_chain).trim(),
    ligand_chain: readText(fromPropertiesState.ligand_chain || fromProperties.ligand_chain || fromConfidence.ligand_chain).trim(),
    prediction_task_id:
      readText(fromPropertiesState.prediction_task_id).trim() ||
      readText(fromProperties.prediction_task_id).trim() ||
      readText(fromConfidence.prediction_task_id).trim(),
    prediction_candidate_smiles:
      readText(fromPropertiesState.prediction_candidate_smiles).trim() ||
      readText(fromProperties.prediction_candidate_smiles).trim() ||
      readText(fromConfidence.prediction_candidate_smiles).trim()
  };
}

function resolveLeadOptDownloadTaskId(taskInput: unknown, structureTaskIdInput: unknown): string {
  const viewerTaskId = readText(structureTaskIdInput).trim();
  if (viewerTaskId) return viewerTaskId;

  const task = asRecord(taskInput);
  if (Object.keys(task).length === 0) return '';

  const snapshot = resolveLeadOptSnapshotFromTask(task);
  const snapshotPredictionTaskId = readText(
    snapshot.prediction_task_id || asRecord(snapshot.prediction_summary).latest_task_id
  ).trim();
  if (snapshotPredictionTaskId) return snapshotPredictionTaskId;

  const taskId = readText(task.task_id).trim();
  const structureName = readText(task.structure_name).trim();
  if (taskId && structureName && Object.keys(snapshot).length === 0) return taskId;

  return '';
}

function sanitizeArchiveNamePart(value: unknown, fallback = 'item'): string {
  const text = readText(value).trim();
  if (!text) return fallback;
  const normalized = text
    .replace(/[\\/:*?"<>|]/g, '_')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  if (!normalized) return fallback;
  return normalized.slice(0, 80);
}

function collectLeadOptDownloadRecords(
  predictionMapInput: unknown,
  preferredBackendInput: unknown
): Array<{ key: string; backend: string; smiles: string; record: LeadOptPredictionRecord }> {
  const predictionMap = asPredictionRecordMap(predictionMapInput);
  const preferredBackend = normalizePredictionBackendStrict(preferredBackendInput);
  const allSuccess = Object.entries(predictionMap)
    .map(([key, record]) => {
      const parsed = parseLeadOptPredictionRecordKey(key);
      const backend = normalizePredictionBackendStrict(record.backend || parsed.backend);
      const smiles = readText(parsed.smiles).trim();
      return { key, backend, smiles, record };
    })
    .filter(({ backend, record }) => {
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      return Boolean(taskId && !taskId.startsWith('local:') && backend && state === 'SUCCESS');
    });
  if (!preferredBackend) return allSuccess;
  const preferred = allSuccess.filter(({ backend }) => backend === preferredBackend);
  return preferred.length > 0 ? preferred : allSuccess;
}

async function downloadLeadOptCombinedArchive(params: {
  predictionMap: unknown;
  preferredBackend?: unknown;
  projectName: string;
  queryId?: string;
  fallbackTaskId?: string;
}): Promise<void> {
  const records = collectLeadOptDownloadRecords(params.predictionMap, params.preferredBackend);
  if (records.length === 0) {
    const fallbackTaskId = readText(params.fallbackTaskId).trim();
    if (!fallbackTaskId) {
      throw new Error('No successful lead-opt prediction results are available for download yet.');
    }
    await downloadResultFile(fallbackTaskId);
    return;
  }

  const { default: JSZipLib } = await import('jszip');
  const bundleZip = new JSZipLib();
  const manifest: Array<Record<string, unknown>> = [];
  const sortedRecords = [...records].sort((left, right) => {
    const leftBackend = readText(left.backend).trim();
    const rightBackend = readText(right.backend).trim();
    if (leftBackend !== rightBackend) return leftBackend.localeCompare(rightBackend);
    return left.smiles.localeCompare(right.smiles);
  });

  for (let index = 0; index < sortedRecords.length; index += 1) {
    const item = sortedRecords[index];
    const taskId = readText(item.record.taskId).trim();
    const sourceBlob = await downloadResultBlob(taskId, { mode: 'full' });
    const sourceZip = await JSZipLib.loadAsync(sourceBlob);
    const folderName = [
      String(index + 1).padStart(3, '0'),
      sanitizeArchiveNamePart(item.backend, 'backend'),
      sanitizeArchiveNamePart(item.smiles, 'compound'),
      sanitizeArchiveNamePart(taskId, 'task')
    ].join('_');
    for (const [path, entry] of Object.entries(sourceZip.files)) {
      if (entry.dir) continue;
      bundleZip.file(`${folderName}/${path}`, await entry.async('blob'));
    }
    manifest.push({
      index: index + 1,
      task_id: taskId,
      backend: item.backend,
      smiles: item.smiles,
      structure_name: readText(item.record.structureName).trim(),
      source_folder: folderName,
    });
  }

  bundleZip.file(
    'manifest.json',
    JSON.stringify(
      {
        project_name: readText(params.projectName).trim(),
        query_id: readText(params.queryId).trim(),
        compound_count: sortedRecords.length,
        generated_at: new Date().toISOString(),
        records: manifest,
      },
      null,
      2
    )
  );

  const archiveBlob = await bundleZip.generateAsync({ type: 'blob' });
  const href = URL.createObjectURL(archiveBlob);
  const anchor = document.createElement('a');
  const projectNamePart = sanitizeArchiveNamePart(params.projectName, 'lead_opt');
  const queryIdPart = sanitizeArchiveNamePart(params.queryId, 'query');
  anchor.href = href;
  anchor.download = `${projectNamePart}_${queryIdPart}_lead_opt_results.zip`;
  anchor.click();
  URL.revokeObjectURL(href);
}

function readLeadOptTaskRowTimestamp(taskInput: unknown): number {
  const task = asRecord(taskInput);
  return new Date(
    readText(task.updated_at || task.completed_at || task.submitted_at || task.created_at).trim()
  ).getTime() || 0;
}

function readLeadOptSnapshotPriority(taskInput: unknown): number {
  const task = taskInput as any;
  const snapshot = resolveLeadOptSnapshotFromTask(task);
  if (Object.keys(snapshot).length === 0) return -1;
  const summary = readLeadOptTaskSummary(task);
  if (!summary) return 0;
  const queued = Math.max(0, Number(summary.predictionQueued || 0));
  const running = Math.max(0, Number(summary.predictionRunning || 0));
  const success = Math.max(0, Number(summary.predictionSuccess || 0));
  const failure = Math.max(0, Number(summary.predictionFailure || 0));
  const stage = readText(summary.stage).trim().toLowerCase();
  const hasMaterializedQuery = Boolean(
    summary.transformCount !== null ||
    summary.candidateCount !== null ||
    summary.databaseId ||
    summary.databaseLabel ||
    summary.databaseSchema
  );
  if (success > 0 || failure > 0 || hasMaterializedQuery || stage === 'prediction_completed' || stage === 'completed') return 4;
  if (running > 0 || stage === 'prediction_running' || stage === 'running') return 3;
  if (queued > 0 || stage === 'prediction_queued' || stage === 'queued') return 2;
  return 1;
}

function readLeadOptListPriority(taskInput: unknown): number {
  const task = taskInput as any;
  const snapshot = resolveLeadOptSnapshotFromTask(taskInput);
  if (Object.keys(snapshot).length === 0) return -1;
  const summary = readLeadOptTaskSummary(task);
  const queryResult = asRecord(snapshot.query_result);
  const queryId = readText(snapshot.query_id || queryResult.query_id).trim();
  const candidateCount = Math.max(
    Number(summary?.candidateCount ?? 0) || 0,
    Number(snapshot.candidate_count || 0) || 0,
    Array.isArray(snapshot.enumerated_candidates) ? snapshot.enumerated_candidates.length : 0
  );
  const transformCount = Math.max(
    Number(summary?.transformCount ?? 0) || 0,
    Number(snapshot.transform_count || 0) || 0,
    Array.isArray(queryResult.transforms) ? queryResult.transforms.length : 0
  );
  const bucketCount = Math.max(
    Number(summary?.bucketCount ?? 0) || 0,
    Number(snapshot.bucket_count || 0) || 0,
    Array.isArray(queryResult.clusters) ? queryResult.clusters.length : 0
  );
  if (queryId && candidateCount > 0) return 4;
  if (queryId && (transformCount > 0 || bucketCount > 0)) return 3;
  if (queryId) return 2;
  return 1;
}

function pickPreferredLeadOptTask(projectTasks: ProjectTask[]): ProjectTask | null {
  let preferred: ProjectTask | null = null;
  for (const row of projectTasks) {
    const snapshot = resolveLeadOptSnapshotFromTask(row);
    if (Object.keys(snapshot).length === 0) continue;
    if (!preferred) {
      preferred = row;
      continue;
    }
    const preferredPriority = readLeadOptListPriority(preferred);
    const candidatePriority = readLeadOptListPriority(row);
    if (candidatePriority > preferredPriority) {
      preferred = row;
      continue;
    }
    if (candidatePriority < preferredPriority) continue;
    if (readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(preferred)) {
      preferred = row;
    }
  }
  return preferred;
}

function readLeadOptQueryIdFromSnapshot(snapshotInput: unknown): string {
  const snapshot = asRecord(snapshotInput);
  const queryResult = asRecord(snapshot.query_result);
  return readText(snapshot.query_id || queryResult.query_id).trim();
}

function buildLeadOptAggregatedSnapshot(params: {
  projectTasks: ProjectTask[];
  requestedTaskRow?: ProjectTask | null;
  preferRequestedQuery?: boolean;
  strictRequestedTaskRow?: boolean;
  preferredListTask?: ProjectTask | null;
  historicalReferenceRecords: Record<string, LeadOptPredictionRecord>;
}): Record<string, unknown> | null {
  const { projectTasks, requestedTaskRow, preferRequestedQuery, strictRequestedTaskRow, preferredListTask, historicalReferenceRecords } = params;
  const requestedSnapshot = resolveLeadOptSnapshotFromTask(requestedTaskRow);
  const requestedTaskRowId = readText((requestedTaskRow as any)?.id).trim();
  const requestedQueryId = readLeadOptQueryIdFromSnapshot(requestedSnapshot);
  const preferredListSnapshot = resolveLeadOptSnapshotFromTask(preferredListTask);
  const preferredListQueryId = readLeadOptQueryIdFromSnapshot(preferredListSnapshot);
  const hasMaterializedLeadOptSnapshot = (snapshotInput: unknown): boolean => {
    const snapshot = asRecord(snapshotInput);
    if (Object.keys(snapshot).length === 0) return false;
    const queryResult = asRecord(snapshot.query_result);
    const queryId = readText(snapshot.query_id || queryResult.query_id).trim();
    if (!queryId) return false;
    if (Array.isArray(snapshot.enumerated_candidates) && snapshot.enumerated_candidates.length > 0) return true;
    if (Object.keys(asPredictionRecordMap(snapshot.prediction_by_smiles)).length > 0) return true;
    if (Number(snapshot.candidate_count || 0) > 0) return true;
    if (Number(snapshot.transform_count || 0) > 0) return true;
    if (Number(snapshot.bucket_count || 0) > 0) return true;
    if (Array.isArray(queryResult.transforms) && queryResult.transforms.length > 0) return true;
    if (Array.isArray(queryResult.clusters) && queryResult.clusters.length > 0) return true;
    if (Number(queryResult.count || 0) > 0) return true;
    if (Number(queryResult.global_count || 0) > 0) return true;
    return false;
  };
  let anchorQueryId = preferredListQueryId || requestedQueryId;
  if (requestedQueryId) {
    const requestedRows = projectTasks.filter((row) => {
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      return readLeadOptQueryIdFromSnapshot(snapshot) === requestedQueryId;
    });
    const requestedHasMaterialized = requestedRows.some((row) =>
      hasMaterializedLeadOptSnapshot(resolveLeadOptSnapshotFromTask(row))
    );
    if (preferRequestedQuery || requestedHasMaterialized || !preferredListQueryId) {
      anchorQueryId = requestedQueryId;
    }
  }
  let relevantRows: ProjectTask[] = [];
  if (strictRequestedTaskRow && requestedTaskRowId) {
    relevantRows = projectTasks.filter((row) => {
      if (readText((row as any)?.id).trim() !== requestedTaskRowId) return false;
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      return Object.keys(snapshot).length > 0;
    });
    if (relevantRows.length === 0 && requestedTaskRow) {
      if (Object.keys(requestedSnapshot).length > 0) {
        relevantRows = [requestedTaskRow];
      }
    }
    if (requestedQueryId) {
      anchorQueryId = requestedQueryId;
    }
  } else {
    relevantRows = projectTasks.filter((row) => {
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      if (Object.keys(snapshot).length === 0) return false;
      if (!anchorQueryId) return true;
      return readLeadOptQueryIdFromSnapshot(snapshot) === anchorQueryId;
    });
  }
  if (relevantRows.length === 0) return null;

  let listSource: ProjectTask | null = null;
  let stateSource: ProjectTask | null = null;
  let mergedPredictions: Record<string, LeadOptPredictionRecord> = {};
  let mergedReferencePredictions: Record<string, LeadOptPredictionRecord> = {};
  const mergedEnumeratedBySmiles: Record<string, Record<string, unknown>> = {};
  for (const row of relevantRows) {
    const snapshot = resolveLeadOptSnapshotFromTask(row);
    mergedPredictions = mergePredictionRecordMaps(mergedPredictions, snapshot.prediction_by_smiles);
    mergedReferencePredictions = mergePredictionRecordMaps(mergedReferencePredictions, snapshot.reference_prediction_by_backend);
    const enumerated = Array.isArray(snapshot.enumerated_candidates) ? snapshot.enumerated_candidates : [];
    for (const candidateRaw of enumerated) {
      const candidate = asRecord(candidateRaw);
      const smiles = readText(candidate.smiles).trim();
      if (!smiles) continue;
      const existing = mergedEnumeratedBySmiles[smiles];
      if (!existing) {
        mergedEnumeratedBySmiles[smiles] = candidate;
        continue;
      }
      const existingScore = Object.keys(existing).length;
      const candidateScore = Object.keys(candidate).length;
      if (candidateScore >= existingScore) {
        mergedEnumeratedBySmiles[smiles] = candidate;
      }
    }
    if (!listSource) {
      listSource = row;
    } else {
      const currentPriority = readLeadOptListPriority(listSource);
      const nextPriority = readLeadOptListPriority(row);
      if (nextPriority > currentPriority || (nextPriority === currentPriority && readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(listSource))) {
        listSource = row;
      }
    }
    if (!stateSource) {
      stateSource = row;
    } else {
      const currentPriority = readLeadOptSnapshotPriority(stateSource);
      const nextPriority = readLeadOptSnapshotPriority(row);
      if (nextPriority > currentPriority || (nextPriority === currentPriority && readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(stateSource))) {
        stateSource = row;
      }
    }
  }
  const listSnapshot = resolveLeadOptSnapshotFromTask(listSource);
  const stateSnapshot = resolveLeadOptSnapshotFromTask(stateSource);
  const mergedEnumeratedCandidates = Object.values(mergedEnumeratedBySmiles);
  const mergedSummary = summarizeLeadOptPredictions(mergedPredictions);
  const basePredictionSummary = asRecord(stateSnapshot.prediction_summary);
  const baseStage = readText(stateSnapshot.stage || stateSnapshot.prediction_stage || listSnapshot.stage || listSnapshot.prediction_stage).trim().toLowerCase();
  const derivedStage =
    mergedSummary.running > 0
      ? 'prediction_running'
      : mergedSummary.queued > 0
        ? 'prediction_queued'
        : mergedSummary.failure > 0 && mergedSummary.success === 0 && mergedSummary.total > 0
          ? 'prediction_failed'
          : mergedSummary.total > 0
            ? 'prediction_completed'
            : baseStage;
  const derivedPredictionStage =
    mergedSummary.running > 0
      ? 'running'
      : mergedSummary.queued > 0
        ? 'queued'
        : mergedSummary.total > 0
          ? 'completed'
          : readText(stateSnapshot.prediction_stage || listSnapshot.prediction_stage).trim();

  return {
    ...listSnapshot,
    ...stateSnapshot,
    query_id: anchorQueryId || readLeadOptQueryIdFromSnapshot(stateSnapshot) || readLeadOptQueryIdFromSnapshot(listSnapshot),
    task_id: readText(
      stateSnapshot.task_id ||
        asRecord(stateSnapshot.query_result).task_id ||
        listSnapshot.task_id ||
        asRecord(listSnapshot.query_result).task_id
    ).trim(),
    query_result:
      Object.keys(asRecord(listSnapshot.query_result)).length > 0
        ? asRecord(listSnapshot.query_result)
        : asRecord(stateSnapshot.query_result),
    enumerated_candidates:
      mergedEnumeratedCandidates.length > 0
        ? mergedEnumeratedCandidates
        : Array.isArray(listSnapshot.enumerated_candidates) && listSnapshot.enumerated_candidates.length > 0
          ? listSnapshot.enumerated_candidates
          : Array.isArray(stateSnapshot.enumerated_candidates)
            ? stateSnapshot.enumerated_candidates
            : [],
    selection:
      Object.keys(asRecord(listSnapshot.selection)).length > 0
        ? asRecord(listSnapshot.selection)
        : asRecord(stateSnapshot.selection),
    ui_state: {},
    query_payload:
      Object.keys(asRecord(listSnapshot.query_payload)).length > 0
        ? asRecord(listSnapshot.query_payload)
        : asRecord(stateSnapshot.query_payload),
    query_cache_state: readText(stateSnapshot.query_cache_state || listSnapshot.query_cache_state).trim().toLowerCase(),
    stage: derivedStage,
    prediction_stage: derivedPredictionStage,
    prediction_summary: {
      ...basePredictionSummary,
      total: Math.max(mergedSummary.total, Number(toFiniteNumber(basePredictionSummary.total) || 0)),
      queued: mergedSummary.queued,
      running: mergedSummary.running,
      success: mergedSummary.success,
      failure: mergedSummary.failure,
      latest_task_id: readText(basePredictionSummary.latest_task_id).trim()
    },
    prediction_by_smiles: mergedPredictions,
    reference_prediction_by_backend: hydratePredictionRecordMapFromHistory(
      mergedReferencePredictions,
      historicalReferenceRecords
    ),
    target_chain: readText(stateSnapshot.target_chain || listSnapshot.target_chain).trim(),
    ligand_chain: readText(stateSnapshot.ligand_chain || listSnapshot.ligand_chain).trim()
  };
}

function buildLeadOptSelectionFromPayload(payload: Record<string, unknown>, context: {
  querySmiles: string;
  targetChain: string;
  ligandChain: string;
}) {
  const variableSpec = asRecord(payload.variable_spec);
  const variableItems = asRecordArray(variableSpec.items).map((item) => {
    const atomIndices = Array.isArray(item.atom_indices)
      ? item.atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
      : [];
    return {
      query: readText(item.query).trim(),
      mode: readText(item.mode).trim() || 'substructure',
      fragment_id: readText(item.fragment_id).trim(),
      atom_indices: atomIndices
    };
  });
  const selectedFragmentIdsFromPayload = Array.isArray(payload.selected_fragment_ids)
    ? payload.selected_fragment_ids
        .map((value) => readText(value).trim())
        .filter(Boolean)
    : [];
  const selectedFragmentAtomIndicesFromPayload = Array.isArray(payload.selected_fragment_atom_indices)
    ? payload.selected_fragment_atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
    : [];
  const selectedFragmentIds = Array.from(
    new Set(
      selectedFragmentIdsFromPayload.length > 0
        ? selectedFragmentIdsFromPayload
        : variableItems.map((item) => readText(item.fragment_id).trim()).filter(Boolean)
    )
  );
  const selectedFragmentAtomIndices = Array.from(
    new Set(
      selectedFragmentAtomIndicesFromPayload.length > 0
        ? selectedFragmentAtomIndicesFromPayload
        : variableItems.flatMap((item) => item.atom_indices || [])
    )
  );
  const variableQueries = Array.from(
    new Set(variableItems.map((item) => readText(item.query).trim()).filter(Boolean))
  );
  const groupedByEnvironmentValue = readBooleanToken(payload.grouped_by_environment);
  const groupedByEnvironmentMode =
    groupedByEnvironmentValue === true ? 'on' : groupedByEnvironmentValue === false ? 'off' : 'auto';
  const propertyTargets = asRecord(payload.property_targets);
  const queryProperty = readText(propertyTargets.property).trim();
  const directionToken = readText(propertyTargets.direction).trim().toLowerCase();
  const direction = directionToken === 'increase' || directionToken === 'decrease' ? directionToken : '';
  const minPairsRaw = Number(payload.min_pairs);
  const minPairs = Number.isFinite(minPairsRaw) ? Math.max(1, Math.floor(minPairsRaw)) : 1;
  const envRadiusRaw = Number(payload.rule_env_radius);
  const envRadius = Number.isFinite(envRadiusRaw) ? Math.max(0, Math.floor(envRadiusRaw)) : 1;
  return {
    query_smiles: readText(context.querySmiles).trim(),
    target_chain: readText(context.targetChain).trim(),
    ligand_chain: readText(context.ligandChain).trim(),
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    variable_queries: variableQueries,
    variable_items: variableItems,
    grouped_by_environment_mode: groupedByEnvironmentMode,
    query_property: queryProperty,
    direction,
    min_pairs: minPairs,
    env_radius: envRadius
  };
}

type WorkspaceRuntime = ReturnType<typeof useProjectDetailRuntimeContext>;
type WorkspaceRuntimeReady = WorkspaceRuntime & {
  project: NonNullable<WorkspaceRuntime['project']>;
  draft: NonNullable<WorkspaceRuntime['draft']>;
};

export function useProjectDetailWorkspaceView() {
  const runtime = useProjectDetailRuntimeContext();
  const { locationSearch, entryRoutingResolved, loading, error, project, draft } = runtime;

  if (!entryRoutingResolved || loading) {
    const query = new URLSearchParams(locationSearch);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const loadingLabel =
      !entryRoutingResolved
        ? 'Loading project...'
        : requestedTaskRowId || query.get('tab') === 'results'
          ? 'Loading current task...'
          : 'Loading project...';
    return <div className="centered-page">{loadingLabel}</div>;
  }

  if (error && !project) {
    return (
      <div className="page-grid">
        <div className="alert error">{error}</div>
        <Link className="btn btn-ghost" to="/projects">
          Back to projects
        </Link>
      </div>
    );
  }

  if (!project || !draft) {
    return null;
  }

  return <ProjectDetailWorkspaceLoaded runtime={runtime as WorkspaceRuntimeReady} />;
}

function ProjectDetailWorkspaceLoaded({ runtime }: { runtime: WorkspaceRuntimeReady }) {
  const { session } = useAuth();
  const [leadOptHeaderRunAction, setLeadOptHeaderRunAction] = useState<(() => void | Promise<void>) | null>(null);
  const [leadOptHeaderRunPending, setLeadOptHeaderRunPending] = useState(false);
  const [headerStopRunPending, setHeaderStopRunPending] = useState(false);
  const explicitRequestedTaskRowId = useMemo(
    () => String(new URLSearchParams(runtime.locationSearch).get('task_row_id') || '').trim(),
    [runtime.locationSearch]
  );
  const handleRegisterLeadOptHeaderRunAction = useCallback((action: (() => void | Promise<void>) | null) => {
    setLeadOptHeaderRunAction(() => action);
  }, []);
  const {
    loading,
    error,
    setError,
    project,
    draft,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    hasIncompleteComponents,
    componentCompletion,
    submitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    chainInfoById,
    componentTypeBuckets,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    activeConstraintId,
    selectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintPickSlotRef,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    structureText,
    structureFormat,
    structureTaskId,
    confidenceBackend,
    projectBackend,
    activeResultTask,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreview,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity,
    snapshotConfidence,
    statusInfo,
    statusContextTaskRow,
    requestedStatusTaskRow,
    projectTasks,
    snapshotLigandAtomPlddts,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponent,
    snapshotLigandResiduePlddts,
    setProteinTemplates,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions,
    pickedResidue,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    allowedConstraintTypes,
    sidebarTypeOpen,
    activeComponentId,
    sidebarConstraintsOpen,
    selectedContactConstraintIdSet,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    affinityEnableDisabledReason,
    isResultsResizing,
    resultsGridRef,
    handleResultsResizerPointerDown,
    handleResultsResizerKeyDown,
    resultsGridStyle,
    resultChainIds,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    leadOptPrimary,
    leadOptChainContext,
    leadOptPersistedUploads,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    handleComponentsResizerPointerDown,
    handleComponentsResizerKeyDown,
    proteinTemplates,
    displayTaskState,
    isActiveRuntime,
    progressPercent,
    waitingSeconds,
    totalRuntimeSeconds,
    hasUnsavedChanges,
    runMenuOpen,
    runSuccessNotice,
    resultError,
    resultChainConsistencyWarning,
    runActionRef,
    topRunButtonRef,
    patchTask,
    pullResultForViewer,
    persistDraftTaskSnapshot,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saveDraft,
    setRunRedirectTaskId,
    navigate,
    affinityLigandFile
  } = runtime;
  const sessionIdentity =
    readText(session?.userId).trim() ||
    readText(session?.username).trim().toLowerCase() ||
    readSessionIdentityFromLocalStorage();
  const leadOptMmpTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptPredictionTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptUploadPersistKeyRef = useRef('');
  const leadOptActiveTaskRowIdRef = useRef('');
  const leadOptPredictionPersistKeyRef = useRef('');
  const leadOptPredictionPersistQueueRef = useRef<Promise<void>>(Promise.resolve());
  const leadOptPredictionPersistTimerRef = useRef<number | null>(null);
  const leadOptPredictionPersistPendingByTaskRowRef = useRef<Record<string, {
    taskRowId: string;
    patchPayload: Record<string, unknown>;
  }>>({});
  const leadOptPredictionPersistShadowByTaskRowRef = useRef<Record<string, Record<string, unknown>>>({});
  const leadOptPersistSnapshotByTaskRowRef = useRef<Record<string, Record<string, unknown>>>({});
  const leadOptUiStatePersistKeyRef = useRef('');
  const leadOptMmpContextByTaskIdRef = useRef<Record<string, Record<string, unknown>>>({});

  const flushLeadOptPredictionPersistQueue = useCallback(() => {
    const pendingEntries = Object.values(leadOptPredictionPersistPendingByTaskRowRef.current);
    leadOptPredictionPersistPendingByTaskRowRef.current = {};
    if (pendingEntries.length === 0) return;
    const nextPersist = leadOptPredictionPersistQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        for (const entry of pendingEntries) {
          await patchTask(entry.taskRowId, entry.patchPayload as any);
        }
      });
    leadOptPredictionPersistQueueRef.current = nextPersist;
  }, [patchTask]);

  const flushLeadOptPredictionPersistQueueNow = useCallback(() => {
    if (leadOptPredictionPersistTimerRef.current !== null) {
      window.clearTimeout(leadOptPredictionPersistTimerRef.current);
      leadOptPredictionPersistTimerRef.current = null;
    }
    flushLeadOptPredictionPersistQueue();
  }, [flushLeadOptPredictionPersistQueue]);

  const queueLeadOptPredictionPersistPatch = useCallback(
    (
      taskRowId: string,
      patchPayload: Record<string, unknown>,
      options?: { immediate?: boolean; debounceMs?: number }
    ) => {
      const normalizedTaskRowId = readText(taskRowId).trim();
      if (!normalizedTaskRowId) return;
      const pendingForRow = leadOptPredictionPersistPendingByTaskRowRef.current[normalizedTaskRowId];
      const shadowForRow = leadOptPredictionPersistShadowByTaskRowRef.current[normalizedTaskRowId];
      const mergedPatchPayload = mergeLeadOptPatchPayloadForPersist(
        patchPayload,
        pendingForRow?.patchPayload || shadowForRow || {}
      );
      leadOptPredictionPersistPendingByTaskRowRef.current[normalizedTaskRowId] = {
        taskRowId: normalizedTaskRowId,
        patchPayload: mergedPatchPayload
      };
      leadOptPredictionPersistShadowByTaskRowRef.current[normalizedTaskRowId] = mergedPatchPayload;
      if (options?.immediate) {
        flushLeadOptPredictionPersistQueueNow();
        return;
      }
      if (leadOptPredictionPersistTimerRef.current !== null) return;
      const debounceMsRaw = Number(options?.debounceMs);
      const debounceMs = Number.isFinite(debounceMsRaw)
        ? Math.max(0, Math.floor(debounceMsRaw))
        : 900;
      leadOptPredictionPersistTimerRef.current = window.setTimeout(() => {
        leadOptPredictionPersistTimerRef.current = null;
        flushLeadOptPredictionPersistQueue();
      }, debounceMs);
    },
    [flushLeadOptPredictionPersistQueue, flushLeadOptPredictionPersistQueueNow]
  );

  useEffect(() => {
    return () => {
      flushLeadOptPredictionPersistQueueNow();
    };
  }, [flushLeadOptPredictionPersistQueueNow]);

  useEffect(() => {
    if (!isLeadOptimizationWorkflow) return;
    if (workspaceTab === 'results' || workspaceTab === 'components') return;
    flushLeadOptPredictionPersistQueueNow();
  }, [flushLeadOptPredictionPersistQueueNow, isLeadOptimizationWorkflow, workspaceTab]);

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState !== 'hidden') return;
      flushLeadOptPredictionPersistQueueNow();
    };
    const handlePageHide = () => {
      flushLeadOptPredictionPersistQueueNow();
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('pagehide', handlePageHide);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('pagehide', handlePageHide);
    };
  }, [flushLeadOptPredictionPersistQueueNow]);

  const preferredLeadOptSnapshotTask = useMemo(
    () => pickPreferredLeadOptTask(projectTasks),
    [projectTasks]
  );

  const resolveLeadOptTaskRowId = useCallback((): string => {
    const explicitRequestedRowId = readText(explicitRequestedTaskRowId).trim();
    if (explicitRequestedRowId) {
      const explicitRequestedRow = projectTasks.find((row) => readText(row?.id).trim() === explicitRequestedRowId) || null;
      if (explicitRequestedRow) return explicitRequestedRowId;
    }

    const requestedRowId = readText(requestedStatusTaskRow?.id).trim();
    if (requestedRowId) return requestedRowId;

    const preferredLeadOptTaskRowId = readText(preferredLeadOptSnapshotTask?.id).trim();
    if (preferredLeadOptTaskRowId) return preferredLeadOptTaskRowId;

    const rememberedRowId = readText(leadOptActiveTaskRowIdRef.current).trim();
    if (rememberedRowId) return rememberedRowId;

    const contextRowId = readText(statusContextTaskRow?.id).trim();
    if (contextRowId) return contextRowId;

    const activeResultRowId = readText(activeResultTask?.id).trim();
    if (activeResultRowId) return activeResultRowId;

    const latestRuntimeTaskRow = projectTasks.find((row) => readText(row?.task_id).trim().length > 0);
    const latestRuntimeTaskRowId = readText(latestRuntimeTaskRow?.id).trim();
    if (latestRuntimeTaskRowId) return latestRuntimeTaskRowId;

    const firstTaskRowId = readText(projectTasks[0]?.id).trim();
    if (firstTaskRowId) return firstTaskRowId;

    return '';
  }, [activeResultTask, explicitRequestedTaskRowId, preferredLeadOptSnapshotTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]);

  const resolveLeadOptSourceTask = useCallback(
    (taskRowId: string) => {
      const id = readText(taskRowId).trim();
      if (!id) return null;
      if (requestedStatusTaskRow && String(requestedStatusTaskRow.id) === id) return requestedStatusTaskRow;
      if (statusContextTaskRow && String(statusContextTaskRow.id) === id) return statusContextTaskRow;
      if (activeResultTask && String(activeResultTask.id) === id) return activeResultTask;
      const projectTask = projectTasks.find((row) => readText(row?.id).trim() === id);
      if (projectTask) return projectTask;
      return null;
    },
    [activeResultTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]
  );

  const resolveLeadOptTaskRowIdByPredictionTaskId = useCallback(
    (predictionTaskIdInput: string): string => {
      const predictionTaskId = readText(predictionTaskIdInput).trim();
      if (!predictionTaskId) return '';
      for (const row of projectTasks) {
        const snapshot = resolveLeadOptSnapshotFromTask(row);
        const predictionMap = asPredictionRecordMap(snapshot.prediction_by_smiles);
        for (const record of Object.values(predictionMap)) {
          if (readText(record?.taskId).trim() === predictionTaskId) {
            return readText(row?.id).trim();
          }
        }
      }
      return '';
    },
    [projectTasks]
  );

  const leadOptHistoricalReferenceRecords = useMemo(() => {
    let merged: Record<string, LeadOptPredictionRecord> = {};
    for (const row of projectTasks) {
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      const records = asPredictionRecordMap(snapshot.reference_prediction_by_backend);
      if (Object.keys(records).length === 0) continue;
      merged = mergePredictionRecordMaps(merged, records);
    }
    return compactLeadOptPredictionMap(merged);
  }, [projectTasks]);

  const leadOptDownloadTaskId = useMemo(
    () => resolveLeadOptDownloadTaskId(activeResultTask, structureTaskId),
    [activeResultTask, structureTaskId]
  );
  const defaultDownloadTaskId = useMemo(() => {
    const viewerTaskId = readText(structureTaskId).trim();
    if (viewerTaskId) return viewerTaskId;
    const activeTaskId = readText(activeResultTask?.task_id).trim();
    const activeStructureName = readText(activeResultTask?.structure_name).trim();
    if (activeStructureName && activeTaskId) return activeTaskId;
    return readText(project.task_id).trim();
  }, [activeResultTask?.structure_name, activeResultTask?.task_id, project.task_id, structureTaskId]);

  const aggregatedLeadOptSnapshot = useMemo(
    () =>
      buildLeadOptAggregatedSnapshot({
        projectTasks,
        requestedTaskRow: requestedStatusTaskRow,
        preferRequestedQuery: Boolean(explicitRequestedTaskRowId || requestedStatusTaskRow?.id),
        strictRequestedTaskRow: Boolean(explicitRequestedTaskRowId || requestedStatusTaskRow?.id),
        preferredListTask: preferredLeadOptSnapshotTask,
        historicalReferenceRecords: leadOptHistoricalReferenceRecords
      }),
    [
      explicitRequestedTaskRowId,
      leadOptHistoricalReferenceRecords,
      preferredLeadOptSnapshotTask,
      projectTasks,
      requestedStatusTaskRow
    ]
  );
  const aggregatedLeadOptSnapshotRecord = asRecord(aggregatedLeadOptSnapshot);
  const leadOptDownloadRecords = useMemo(
    () =>
      collectLeadOptDownloadRecords(
        aggregatedLeadOptSnapshotRecord.prediction_by_smiles,
        aggregatedLeadOptSnapshotRecord.selected_backend
      ),
    [aggregatedLeadOptSnapshotRecord]
  );
  const leadOptActiveTaskRowId = resolveLeadOptTaskRowId();
  const leadOptActiveQueryId = readText(
    aggregatedLeadOptSnapshotRecord.query_id || asRecord(aggregatedLeadOptSnapshotRecord.query_result).query_id
  ).trim();
  const leadOptUiStateScopeKey = buildLeadOptUiStateScopeKey({
    sessionIdentity,
    projectId: project.id,
    taskRowId: leadOptActiveTaskRowId,
    queryId: leadOptActiveQueryId
  });
  const leadOptUserScopedUiState = useMemo(
    () => readLeadOptUiStateFromLocal(leadOptUiStateScopeKey),
    [leadOptUiStateScopeKey]
  );

  const handleLeadOptMmpTaskQueued = async (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
    querySmiles: string;
    referenceUploads: LeadOptPersistedUploads;
  }) => {
    if (!project || !draft) return;
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const effectiveLeadOptLigandSmiles =
      readText(payload.querySmiles).trim() || readText(leadOptPrimary.ligandSmiles).trim();
    const snapshotComponents = buildLeadOptUploadSnapshotComponents(
      draft.inputConfig.components,
      payload.referenceUploads,
      effectiveLeadOptLigandSmiles
    );
    const queuedAt = new Date().toISOString();
    const selection = buildLeadOptSelectionFromPayload(payload.requestPayload || {}, {
      querySmiles: payload.querySmiles || leadOptPrimary.ligandSmiles,
      targetChain: leadOptChainContext.targetChain,
      ligandChain: leadOptChainContext.ligandChain
    });
    const mmpContext = {
      query_payload: payload.requestPayload || {},
      selection,
      target_chain: readText(leadOptChainContext.targetChain).trim(),
      ligand_chain: readText(leadOptChainContext.ligandChain).trim()
    } as Record<string, unknown>;
    const inheritedReferenceRecords = leadOptHistoricalReferenceRecords;
    const draftTaskRow = await persistDraftTaskSnapshot(draft.inputConfig, {
      statusText: 'Lead optimization MMP query queued',
      reuseTaskRowId: null,
      snapshotComponents,
      proteinSequenceOverride: leadOptPrimary.proteinSequence,
      ligandSmilesOverride: effectiveLeadOptLigandSmiles
    });
    leadOptMmpTaskRowMapRef.current[taskId] = draftTaskRow.id;
    leadOptActiveTaskRowIdRef.current = draftTaskRow.id;
    leadOptMmpContextByTaskIdRef.current[taskId] = mmpContext;
    const leadOptPayload = {
      stage: 'queued',
      task_id: taskId,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: inheritedReferenceRecords,
      ...mmpContext
    } as Record<string, unknown>;
    await patchTask(draftTaskRow.id, {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'MMP query queued',
      error_text: '',
      submitted_at: queuedAt,
      completed_at: null,
      duration_seconds: null,
      components: snapshotComponents,
      properties: mergeLeadOptMetaIntoProperties(draft.inputConfig.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    setRunRedirectTaskId(taskId);
  };

  const handleLeadOptMmpTaskCompleted = async (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => {
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const taskRowId = leadOptMmpTaskRowMapRef.current[taskId];
    if (!taskRowId) return;
    leadOptActiveTaskRowIdRef.current = taskRowId;
    const completedAt = new Date().toISOString();
    const mmpContext = asRecord(leadOptMmpContextByTaskIdRef.current[taskId]);
    const snapshot = asRecord(payload.resultSnapshot);
    const queryResult = asRecord(snapshot.query_result);
    const enumeratedCandidates = compactLeadOptEnumeratedCandidates(snapshot.enumerated_candidates);
    const compactQueryResult: Record<string, unknown> = {
      query_id: readText(payload.queryId).trim(),
      task_id: readText(taskId).trim(),
      query_mode: readText(queryResult.query_mode).trim() || 'one-to-many',
      aggregation_type: readText(queryResult.aggregation_type).trim(),
      mmp_database_id: readText(queryResult.mmp_database_id).trim(),
      mmp_database_label: readText(queryResult.mmp_database_label).trim(),
      mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
      property_targets: asRecord(queryResult.property_targets),
      rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
      grouped_by_environment:
        readBooleanToken(queryResult.grouped_by_environment) === null
          ? undefined
          : readBooleanToken(queryResult.grouped_by_environment),
      count: Number.isFinite(Number(queryResult.count)) ? Number(queryResult.count) : payload.transformCount,
      global_count: Number.isFinite(Number(queryResult.global_count)) ? Number(queryResult.global_count) : payload.transformCount,
      min_pairs: Number.isFinite(Number(queryResult.min_pairs)) ? Number(queryResult.min_pairs) : 1,
      cluster_group_by: readText(queryResult.cluster_group_by).trim(),
      stats: asRecord(queryResult.stats)
    };
    const inheritedReferenceRecords = hydratePredictionRecordMapFromHistory(
      asPredictionRecordMap(snapshot.reference_prediction_by_backend),
      leadOptHistoricalReferenceRecords
    );
    const leadOptPayload = {
      stage: 'completed',
      query_id: payload.queryId,
      task_id: taskId,
      transform_count: payload.transformCount,
      candidate_count: payload.candidateCount,
      query_result: compactQueryResult,
      result_storage: 'server_query_cache',
      enumerated_candidates: enumeratedCandidates,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: inheritedReferenceRecords,
      ...mmpContext
    } as Record<string, unknown>;
    leadOptPersistSnapshotByTaskRowRef.current[taskRowId] = mergeLeadOptSnapshotForPersist(
      leadOptPayload,
      leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
    );
    const sourceTask = resolveLeadOptSourceTask(taskRowId);
    await patchTask(taskRowId, {
      task_state: 'SUCCESS',
      status_text: `MMP complete (${payload.transformCount} transforms, ${payload.candidateCount} rows). Scoring not started.`,
      error_text: '',
      completed_at: completedAt,
      duration_seconds: Number.isFinite(payload.elapsedSeconds) ? payload.elapsedSeconds : null,
      properties: mergeLeadOptMetaIntoProperties(sourceTask?.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptMmpTaskFailed = async (payload: { taskId: string; error: string }) => {
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const taskRowId = leadOptMmpTaskRowMapRef.current[taskId];
    if (!taskRowId) return;
    leadOptActiveTaskRowIdRef.current = taskRowId;
    const completedAt = new Date().toISOString();
    const mmpContext = asRecord(leadOptMmpContextByTaskIdRef.current[taskId]);
    const inheritedReferenceRecords = leadOptHistoricalReferenceRecords;
    const leadOptPayload = {
      stage: 'failed',
      task_id: taskId,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: inheritedReferenceRecords,
      ...mmpContext
    } as Record<string, unknown>;
    const sourceTask = resolveLeadOptSourceTask(taskRowId);
    const errorText = readText(payload.error).trim() || 'MMP query failed.';
    const statusText = `MMP query failed${errorText ? `: ${errorText.slice(0, 140)}` : ''}`;
    await patchTask(taskRowId, {
      task_state: 'FAILURE',
      status_text: statusText,
      error_text: errorText,
      completed_at: completedAt,
      properties: mergeLeadOptMetaIntoProperties(sourceTask?.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptPredictionQueued = useCallback(
    async (payload: { taskId: string; backend: string; candidateSmiles: string }) => {
      const taskId = readText(payload.taskId).trim();
      if (!taskId) return;
      const isLocalTaskId = taskId.startsWith('local:');
      const backend = normalizePredictionBackendStrict(payload.backend);
      if (!backend) return;
      const candidateSmiles = readText(payload.candidateSmiles).trim();
      const predictionKey = buildLeadOptPredictionRecordKey(backend, candidateSmiles);
      if (!predictionKey) return;
      const mappedTaskRowId = readText(leadOptPredictionTaskRowMapRef.current[taskId]).trim();
      const rowIdFromSnapshot = !isLocalTaskId ? resolveLeadOptTaskRowIdByPredictionTaskId(taskId) : '';
      const taskRowId = mappedTaskRowId || rowIdFromSnapshot || resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;
      leadOptPredictionTaskRowMapRef.current[taskId] = taskRowId;
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceLeadOpt = mergeLeadOptSnapshotForPersist(
        resolveLeadOptSnapshotFromTask(sourceTask),
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const sourceQueryResult = asRecord(sourceLeadOpt.query_result);
      const sourceLeadOptQueryId = readText(sourceLeadOpt.query_id || sourceQueryResult.query_id).trim();
      const nextPredictionMap = compactLeadOptPredictionMap(
        asPredictionRecordMap(sourceLeadOpt.prediction_by_smiles)
      );
      nextPredictionMap[predictionKey] = {
        taskId,
        state: 'QUEUED',
        backend,
        pairIptm: null,
        pairPae: null,
        pairIptmResolved: false,
        ligandPlddt: null,
        ligandAtomPlddts: [],
        structureText: '',
        structureFormat: 'cif',
        structureName: '',
        error: '',
        updatedAt: Date.now()
      };
      const summary = summarizeLeadOptPredictions(nextPredictionMap);
      const statusText = `Scoring ${Math.max(1, summary.queued + summary.running)} queued (${summary.success}/${Math.max(1, summary.total)} done)`;
      const referenceRecords = compactLeadOptPredictionMap(
        hydratePredictionRecordMapFromHistory(
          asPredictionRecordMap(sourceLeadOpt.reference_prediction_by_backend),
          leadOptHistoricalReferenceRecords
        )
      );
      const nextLeadOpt = {
        ...sourceLeadOpt,
        stage: 'prediction_queued',
        prediction_stage: 'queued',
        prediction_summary: {
          ...summary,
          latest_task_id: taskId
        },
        prediction_task_id: taskId,
        prediction_candidate_smiles: candidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: nextPredictionMap,
        reference_prediction_by_backend: referenceRecords
      } as Record<string, unknown>;
      leadOptPersistSnapshotByTaskRowRef.current[taskRowId] = mergeLeadOptSnapshotForPersist(
        nextLeadOpt,
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const lightweightStateForProperties = {
        stage: nextLeadOpt.stage,
        prediction_stage: nextLeadOpt.prediction_stage,
        query_id: sourceLeadOptQueryId,
        prediction_summary: {
          ...summary,
          latest_task_id: taskId
        },
        prediction_task_id: taskId,
        prediction_candidate_smiles: candidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: nextPredictionMap,
        reference_prediction_by_backend: referenceRecords,
        selected_backend: backend,
        target_chain: readText(sourceLeadOpt.target_chain).trim(),
        ligand_chain: readText(sourceLeadOpt.ligand_chain).trim()
      } as Record<string, unknown>;
      const patchPayload = {
        task_state: 'QUEUED',
        status_text: statusText,
        error_text: '',
        confidence: {
          lead_opt_mmp: compactLeadOptForConfidenceWrite(nextLeadOpt)
        },
        properties: mergeLeadOptStateMetaIntoProperties(sourceTask?.properties, lightweightStateForProperties) as any
      };
      queueLeadOptPredictionPersistPatch(taskRowId, patchPayload, { immediate: !isLocalTaskId });
    },
    [
      leadOptHistoricalReferenceRecords,
      queueLeadOptPredictionPersistPatch,
      resolveLeadOptSourceTask,
      resolveLeadOptTaskRowId,
      resolveLeadOptTaskRowIdByPredictionTaskId
    ]
  );

  const handleLeadOptPredictionStateChange = useCallback(
    async (payload: {
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
    }) => {
      const latestTaskId = readText(payload.summary?.latestTaskId).trim();
      const mappedTaskRowId = latestTaskId ? readText(leadOptPredictionTaskRowMapRef.current[latestTaskId]).trim() : '';
      const rowIdFromSnapshot = latestTaskId ? resolveLeadOptTaskRowIdByPredictionTaskId(latestTaskId) : '';
      const taskRowId = mappedTaskRowId || rowIdFromSnapshot || resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;

      const records = compactLeadOptPredictionMap(asPredictionRecordMap(payload.records));
      const referenceRecords = compactLeadOptPredictionMap(
        hydratePredictionRecordMapFromHistory(
          asPredictionRecordMap(payload.referenceRecords),
          leadOptHistoricalReferenceRecords
        )
      );
      const latestCandidateSmiles = latestTaskId
        ? parseLeadOptPredictionRecordKey(
            Object.entries(records).find(([, record]) => readText(record.taskId).trim() === latestTaskId)?.[0] || ''
          ).smiles
        : '';
      const summary = summarizeLeadOptPredictions(records);
      const latestRecordBackend = latestTaskId
        ? normalizePredictionBackendStrict(
            parseLeadOptPredictionRecordKey(
              Object.entries(records).find(([, record]) => readText(record.taskId).trim() === latestTaskId)?.[0] || ''
            ).backend
          )
        : '';
      const unresolved = summary.queued + summary.running;
      const unresolvedState = summary.running > 0 ? 'RUNNING' : summary.queued > 0 ? 'QUEUED' : null;
      const hasResolvablePendingRecord = Object.values(records).some((record) => {
        const state = readText(record.state).trim().toUpperCase();
        if (state !== 'QUEUED' && state !== 'RUNNING') return false;
        const taskId = readText(record.taskId).trim();
        return taskId.length > 0 && !taskId.startsWith('local:');
      });
      if (unresolved > 0 && !hasResolvablePendingRecord) {
        // Do not persist transient local placeholders; wait for backend-assigned task ids.
        return;
      }
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceLeadOpt = mergeLeadOptSnapshotForPersist(
        resolveLeadOptSnapshotFromTask(sourceTask),
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const sourceQueryResult = asRecord(sourceLeadOpt.query_result);
      const sourceLeadOptQueryId = readText(sourceLeadOpt.query_id || sourceQueryResult.query_id).trim();
      const preferredSelectedBackend =
        latestRecordBackend || normalizePredictionBackendStrict(sourceLeadOpt.selected_backend);
      const nextTaskState: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? 'RUNNING'
            : 'QUEUED'
          : summary.total > 0 && summary.success === 0 && summary.failure > 0
            ? 'FAILURE'
            : 'SUCCESS';
      const persistedTaskState: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' = nextTaskState;
      const statusText =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? `Scoring ${unresolved} running (${summary.success}/${Math.max(1, summary.total)} done)`
            : `Scoring ${unresolved} queued (${summary.success}/${Math.max(1, summary.total)} done)`
          : summary.total > 0
            ? `Scoring complete (${summary.success}/${Math.max(1, summary.total)})`
            : 'MMP complete';
      const errorText =
        summary.total > 0 && summary.success === 0 && summary.failure > 0
          ? 'All candidate scoring jobs failed.'
          : '';

      const nextLeadOpt = {
        ...sourceLeadOpt,
        stage:
          unresolved > 0
            ? unresolvedState === 'RUNNING'
              ? 'prediction_running'
              : 'prediction_queued'
            : summary.failure > 0 && summary.success === 0
              ? 'prediction_failed'
              : 'prediction_completed',
        prediction_stage: unresolved > 0 ? (unresolvedState === 'RUNNING' ? 'running' : 'queued') : 'completed',
        prediction_summary: {
          ...summary,
          latest_task_id: latestTaskId
        },
        prediction_task_id: latestTaskId,
        prediction_candidate_smiles: latestCandidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: records,
        reference_prediction_by_backend: referenceRecords
      } as Record<string, unknown>;
      leadOptPersistSnapshotByTaskRowRef.current[taskRowId] = mergeLeadOptSnapshotForPersist(
        nextLeadOpt,
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const persistKey = [
        taskRowId,
        statusText,
        errorText,
        summary.total,
        summary.queued,
        summary.running,
        summary.success,
        summary.failure,
        buildLeadOptPredictionPersistSignature(records),
        buildLeadOptPredictionPersistSignature(referenceRecords)
      ].join('|');
      if (leadOptPredictionPersistKeyRef.current === persistKey) return;
      leadOptPredictionPersistKeyRef.current = persistKey;

      const lightweightStateForProperties = {
        stage: nextLeadOpt.stage,
        prediction_stage: nextLeadOpt.prediction_stage,
        query_id: sourceLeadOptQueryId,
        prediction_summary: {
          ...summary,
          latest_task_id: latestTaskId
        },
        prediction_task_id: latestTaskId,
        prediction_candidate_smiles: latestCandidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: records,
        reference_prediction_by_backend: referenceRecords,
        ...(preferredSelectedBackend ? { selected_backend: preferredSelectedBackend } : {}),
        target_chain: readText(sourceLeadOpt.target_chain).trim(),
        ligand_chain: readText(sourceLeadOpt.ligand_chain).trim()
      } as Record<string, unknown>;
      const patchPayload = {
        task_state: persistedTaskState,
        status_text: statusText,
        error_text: errorText,
        confidence: {
          lead_opt_mmp: compactLeadOptForConfidenceWrite(nextLeadOpt)
        },
        properties: mergeLeadOptStateMetaIntoProperties(sourceTask?.properties, lightweightStateForProperties) as any
      };
      queueLeadOptPredictionPersistPatch(taskRowId, patchPayload, { immediate: true });
    },
    [
      leadOptHistoricalReferenceRecords,
      queueLeadOptPredictionPersistPatch,
      resolveLeadOptSourceTask,
      resolveLeadOptTaskRowId,
      resolveLeadOptTaskRowIdByPredictionTaskId
    ]
  );

  const handleLeadOptUiStateChange = useCallback(
    (payload: { uiState: LeadOptCandidatesUiState }) => {
      if (!leadOptUiStateScopeKey) return;
      const compactUiState = compactLeadOptCandidatesUiState(payload.uiState);
      const persistKey = [
        leadOptUiStateScopeKey,
        buildLeadOptCandidatesUiStateSignature(compactUiState)
      ].join('|');
      if (leadOptUiStatePersistKeyRef.current === persistKey) return;
      leadOptUiStatePersistKeyRef.current = persistKey;
      writeLeadOptUiStateToLocal(leadOptUiStateScopeKey, compactUiState);
    },
    [leadOptUiStateScopeKey]
  );

  const handleLeadOptReferenceUploadsChange = useCallback(
    async (uploads: LeadOptPersistedUploads) => {
      if (!project || !draft || !canEdit) return;
      if (workspaceTab !== 'components') return;
      const targetName = readText(uploads.target?.fileName).trim();
      const targetSize = readText(uploads.target?.content).length;
      const ligandName = readText(uploads.ligand?.fileName).trim();
      const ligandSize = readText(uploads.ligand?.content).length;
      const contextDraftRowId =
        String(statusContextTaskRow?.task_state || '').toUpperCase() === 'DRAFT'
          ? readText(statusContextTaskRow?.id).trim()
          : '';
      const editableDraftRowId = contextDraftRowId;
      const effectiveLeadOptLigandSmiles = readText(leadOptPrimary.ligandSmiles).trim();
      const dedupeKey = `${project.id}|${editableDraftRowId}|${targetName}:${targetSize}|${ligandName}:${ligandSize}|${effectiveLeadOptLigandSmiles}`;
      if (leadOptUploadPersistKeyRef.current === dedupeKey) return;
      leadOptUploadPersistKeyRef.current = dedupeKey;

      const snapshotComponents = buildLeadOptUploadSnapshotComponents(
        draft.inputConfig.components,
        uploads,
        effectiveLeadOptLigandSmiles
      );
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              inputConfig: {
                ...prev.inputConfig,
                components: snapshotComponents
              }
            }
          : prev
      );

      if (editableDraftRowId) {
        await patchTask(editableDraftRowId, {
          components: snapshotComponents,
          protein_sequence: leadOptPrimary.proteinSequence,
          ligand_smiles: effectiveLeadOptLigandSmiles
        });
      }
    },
    [
      canEdit,
      draft,
      leadOptPrimary.ligandSmiles,
      leadOptPrimary.proteinSequence,
      patchTask,
      project,
      setDraft,
      statusContextTaskRow,
      workspaceTab
    ]
  );

  const workflow = getWorkflowDefinition(project.task_type);
  const affinityUseMsa = computeUseMsaFlag(draft.inputConfig.components, draft.use_msa);
  const runSubmitting = submitting || (isLeadOptimizationWorkflow && leadOptHeaderRunPending);
  const leadOptInitialMmpSnapshot = (() => {
    const leadOptMmp = aggregatedLeadOptSnapshot;
    if (!leadOptMmp || Object.keys(leadOptMmp).length === 0) return null;
    const queryResult = asRecord(leadOptMmp.query_result);
    const queryId = readText(leadOptMmp.query_id || queryResult.query_id).trim();
    if (!queryId) return null;
    return {
      query_result: {
        query_id: queryId,
        task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
        query_mode: readText(queryResult.query_mode || 'one-to-many') || 'one-to-many',
        aggregation_type: readText(queryResult.aggregation_type).trim(),
        property_targets: asRecord(queryResult.property_targets),
        rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
        grouped_by_environment:
          readBooleanToken(queryResult.grouped_by_environment) === null
            ? undefined
            : readBooleanToken(queryResult.grouped_by_environment),
        mmp_database_id: readText(queryResult.mmp_database_id).trim(),
        mmp_database_label: readText(queryResult.mmp_database_label).trim(),
        mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
        cluster_group_by: readText(queryResult.cluster_group_by).trim(),
        transforms: Array.isArray(queryResult.transforms) ? queryResult.transforms : [],
        global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : [],
        clusters: Array.isArray(queryResult.clusters) ? queryResult.clusters : [],
        stats: asRecord(queryResult.stats),
        count: Number(queryResult.count || 0),
        global_count: Number(queryResult.global_count || 0)
      },
      enumerated_candidates: compactLeadOptEnumeratedCandidates(leadOptMmp.enumerated_candidates),
      prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOptMmp.prediction_by_smiles)),
      reference_prediction_by_backend: compactLeadOptPredictionMap(
        hydratePredictionRecordMapFromHistory(
          asPredictionRecordMap(leadOptMmp.reference_prediction_by_backend),
          leadOptHistoricalReferenceRecords
        )
      ),
      ui_state: leadOptUserScopedUiState ? compactLeadOptCandidatesUiState(leadOptUserScopedUiState) : {},
      selection: asRecord(leadOptMmp.selection),
      query_payload: asRecord(leadOptMmp.query_payload),
      task_row_id: leadOptActiveTaskRowId,
      task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
      query_cache_state: readText(leadOptMmp.query_cache_state).trim().toLowerCase(),
      candidate_count: Number.isFinite(Number(leadOptMmp.candidate_count)) ? Number(leadOptMmp.candidate_count) : 0,
      transform_count: Number.isFinite(Number(leadOptMmp.transform_count)) ? Number(leadOptMmp.transform_count) : 0,
      target_chain: readText(leadOptMmp.target_chain).trim(),
      ligand_chain: readText(leadOptMmp.ligand_chain).trim()
    } as Record<string, unknown>;
  })();
  const leadOptSnapshotContext = asRecord(leadOptInitialMmpSnapshot || null);
  const leadOptSnapshotSelection = asRecord(leadOptSnapshotContext.selection);
  const leadOptSnapshotQueryPayload = asRecord(leadOptSnapshotContext.query_payload);
  const leadOptSnapshotTargetChain =
    readText(leadOptSnapshotContext.target_chain).trim() ||
    readText(leadOptSnapshotSelection.target_chain).trim() ||
    readText(leadOptSnapshotQueryPayload.target_chain).trim();
  const leadOptSnapshotLigandChain =
    readText(leadOptSnapshotContext.ligand_chain).trim() ||
    readText(leadOptSnapshotSelection.ligand_chain).trim() ||
    readText(leadOptSnapshotQueryPayload.ligand_chain).trim();
  const leadOptWorkspaceTargetChain = leadOptSnapshotTargetChain || readText(leadOptChainContext.targetChain).trim();
  const leadOptWorkspaceLigandChain = leadOptSnapshotLigandChain || readText(leadOptChainContext.ligandChain).trim();
  const {
    componentStepLabel,
    isRunRedirecting,
    showQuickRunFab,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    runBlockedReason,
    runDisabled,
    canOpenRunMenu,
    sidebarTypeOrder
  } = useProjectRunState({
    workspaceTab,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    submitting: runSubmitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFilePresent: Boolean(affinityTargetFile),
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError: String(affinityPreviewError || ''),
    affinityTargetChainCount: affinityTargetChainIds.length,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    draftBackend: draft.backend
  });
  const formatConstraintCombo = (constraint: PredictionConstraint) =>
    formatConstraintComboForWorkspace(constraint, chainInfoById, componentTypeBuckets);
  const formatConstraintDetail = (constraint: PredictionConstraint) =>
    formatConstraintDetailForWorkspace(constraint);
  const {
    addComponentToDraft,
    addConstraintFromSidebar,
    setAffinityEnabledFromWorkspace,
    setAffinityComponentFromWorkspace,
    jumpToComponent
  } = useProjectSidebarActions({
    draft,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    createInputComponent
  });
  const {
    clearConstraintSelection,
    selectConstraint,
    jumpToConstraint,
    navigateConstraint,
    applyPickToSelectedConstraint
  } = useConstraintWorkspaceActions({
    draft,
    setDraft,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintSelectionAnchorRef,
    setWorkspaceTab,
    setSidebarConstraintsOpen,
    scrollToEditorBlock,
    constraintPickSlotRef,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    ligandChainOptions,
    isBondOnlyBackend
  });
  const {
    displayStructureText,
    displayStructureFormat,
    displayStructureName,
    displayStructureColorMode,
    constraintStructureText,
    constraintStructureFormat,
    hasConstraintStructure,
    snapshotCards,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    affinityResultLigandSmiles,
    affinityResultLigandAtomPlddts,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    hasAffinityDisplayStructure,
  } = useProjectResultDisplay({
    structureText,
    structureFormat,
    confidenceBackend,
    projectBackend,
    activeResultTaskStructureName: activeResultTask?.structure_name || '',
    projectStructureName: project.structure_name || '',
    draftColorMode: draft.color_mode,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedTemplatePreviewContent: selectedTemplatePreview?.content || '',
    selectedTemplatePreviewFormat: selectedTemplatePreview?.format || 'pdb',
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity: snapshotAffinity || null,
    snapshotConfidence: snapshotConfidence || null,
    statusContextLigandSmiles: String(statusContextTaskRow?.ligand_smiles || ''),
    activeResultLigandSmiles: String(activeResultTask?.ligand_smiles || ''),
    snapshotLigandAtomPlddts: snapshotLigandAtomPlddts || [],
    affinityLigandSmiles,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponentType: selectedResultLigandComponent?.type || null,
    snapshotLigandResiduePlddts,
  });
  const {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideMutationRateChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleTaskNameChange,
    handleTaskSummaryChange
  } = useProjectEditorHandlers({
    setDraft,
    setPickedResidue,
    setProteinTemplates,
    filterConstraintsByBackend
  });
  const { predictionConstraintsWorkspaceProps, predictionComponentsSidebarProps } = usePredictionWorkspaceProps({
    draft,
    setDraft,
    filterConstraintsByBackend,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions: constraintTemplateOptions || [],
    selectedTemplatePreview,
    setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    canEdit,
    setWorkspaceTab,
    navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    applyPickToSelectedConstraint,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    clearConstraintSelection,
    activeConstraintId,
    selectedContactConstraintIds,
    selectConstraint,
    allowedConstraintTypes,
    isBondOnlyBackend,
    hasIncompleteComponents,
    componentCompletion,
    sidebarTypeOrder,
    componentTypeBuckets,
    sidebarTypeOpen,
    setSidebarTypeOpen,
    addComponentToDraft,
    activeComponentId,
    jumpToComponent,
    sidebarConstraintsOpen,
    setSidebarConstraintsOpen,
    addConstraintFromSidebar,
    hasActiveChains: activeChainInfos.length > 0,
    selectedContactConstraintIdSet,
    jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    canEnableAffinityFromWorkspace,
    setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason,
    showAffinityComputeToggle: !isPeptideDesignWorkflow
  });
  const {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  } = useProjectWorkflowSectionProps({
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    workflowTitle: workflow.title,
    workflowShortTitle: workflow.shortTitle,
    projectTaskState: displayTaskState || project.task_state || '',
    projectTaskId:
      String(statusContextTaskRow?.task_id || '').trim() ||
      String(activeResultTask?.task_id || '').trim() ||
      String(project.task_id || '').trim(),
    statusInfo: statusInfo || null,
    progressPercent,
    onPeptideRequestStructure: async () => {
      const contextTask = activeResultTask || statusContextTaskRow;
      const taskId = String(contextTask?.task_id || project.task_id || '').trim();
      if (!taskId) return;
      await pullResultForViewer(taskId, {
        taskRowId: contextTask?.id || undefined,
        persistProject: String(project.task_id || '').trim() === taskId,
        resultMode: 'view'
      });
    },
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onResultsResizerPointerDown: handleResultsResizerPointerDown,
    onResultsResizerKeyDown: handleResultsResizerKeyDown,
    snapshotCards,
    snapshotConfidence: snapshotConfidence || null,
    resultChainIds,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    displayStructureText,
    displayStructureFormat,
    displayStructureColorMode,
    displayStructureName,
    confidenceBackend,
    projectBackend,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    hasAffinityDisplayStructure,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    affinityResultLigandSmiles,
    affinityResultLigandAtomPlddts,
    affinityTargetChainIds,
    affinityLigandChainId,
    snapshotLigandAtomPlddts,
    snapshotPlddt,
    snapshotIptm,
    snapshotSelectedPairIptm,
    selectedResultLigandSequence,
    canEdit,
    submitting,
    affinityTargetFileName: affinityTargetFile?.name || '',
    affinityLigandFileName: affinityLigandFile?.name || '',
    affinityLigandSmiles,
    affinityPreviewLigandSmiles: String(affinityPreview?.ligandSmiles || ''),
    affinityUseMsa,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    affinityHasLigand,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    leadOptProteinSequence: leadOptPrimary.proteinSequence,
    leadOptLigandSmiles: leadOptPrimary.ligandSmiles,
    leadOptTargetChain: leadOptWorkspaceTargetChain,
    leadOptLigandChain: leadOptWorkspaceLigandChain,
    leadOptReferenceScopeKey: `${project.id}:leadopt`,
    leadOptPersistedReferenceUploads: leadOptPersistedUploads,
    onLeadOptReferenceUploadsChange: handleLeadOptReferenceUploadsChange,
    onLeadOptMmpTaskQueued: handleLeadOptMmpTaskQueued,
    onLeadOptMmpTaskCompleted: handleLeadOptMmpTaskCompleted,
    onLeadOptMmpTaskFailed: handleLeadOptMmpTaskFailed,
    onLeadOptUiStateChange: handleLeadOptUiStateChange,
    onLeadOptPredictionQueued: handleLeadOptPredictionQueued,
    onLeadOptPredictionStateChange: handleLeadOptPredictionStateChange,
    onLeadOptNavigateToResults: () => {},
    leadOptInitialMmpSnapshot,
    setDraft,
    setWorkspaceTab,
    onRegisterLeadOptHeaderRunAction: handleRegisterLeadOptHeaderRunAction,
    workspaceTab,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    onComponentsResizerPointerDown: handleComponentsResizerPointerDown,
    onComponentsResizerKeyDown: handleComponentsResizerKeyDown,
    components: draft.inputConfig.components,
    onComponentsChange: handlePredictionComponentsChange,
    proteinTemplates,
    onProteinTemplateChange: handlePredictionProteinTemplateChange,
    activeComponentId,
    setActiveComponentId,
    onProteinTemplateResiduePick: handlePredictionTemplateResiduePick,
    predictionConstraintsWorkspaceProps,
    predictionComponentsSidebarProps,
    backend: draft.backend,
    seed: draft.inputConfig.options.seed ?? null,
    peptideDesignMode: draft.inputConfig.options.peptideDesignMode ?? 'cyclic',
    peptideBinderLength: draft.inputConfig.options.peptideBinderLength ?? 20,
    peptideUseInitialSequence: draft.inputConfig.options.peptideUseInitialSequence ?? false,
    peptideInitialSequence: draft.inputConfig.options.peptideInitialSequence ?? '',
    peptideSequenceMask:
      draft.inputConfig.options.peptideSequenceMask ??
      'X'.repeat(Math.max(1, draft.inputConfig.options.peptideBinderLength ?? 20)),
    peptideIterations: draft.inputConfig.options.peptideIterations ?? 12,
    peptidePopulationSize: draft.inputConfig.options.peptidePopulationSize ?? 16,
    peptideEliteSize: draft.inputConfig.options.peptideEliteSize ?? 5,
    peptideMutationRate: draft.inputConfig.options.peptideMutationRate ?? 0.25,
    peptideBicyclicLinkerCcd: draft.inputConfig.options.peptideBicyclicLinkerCcd ?? 'SEZ',
    peptideBicyclicCysPositionMode: draft.inputConfig.options.peptideBicyclicCysPositionMode ?? 'auto',
    peptideBicyclicFixTerminalCys: draft.inputConfig.options.peptideBicyclicFixTerminalCys ?? true,
    peptideBicyclicIncludeExtraCys: draft.inputConfig.options.peptideBicyclicIncludeExtraCys ?? false,
    peptideBicyclicCys1Pos: draft.inputConfig.options.peptideBicyclicCys1Pos ?? 3,
    peptideBicyclicCys2Pos: draft.inputConfig.options.peptideBicyclicCys2Pos ?? 8,
    peptideBicyclicCys3Pos:
      draft.inputConfig.options.peptideBicyclicCys3Pos ??
      (draft.inputConfig.options.peptideBinderLength ?? 20),
    onBackendChange: handleRuntimeBackendChange,
    onSeedChange: handleRuntimeSeedChange,
    onPeptideDesignModeChange: handleRuntimePeptideDesignModeChange,
    onPeptideBinderLengthChange: handleRuntimePeptideBinderLengthChange,
    onPeptideUseInitialSequenceChange: handleRuntimePeptideUseInitialSequenceChange,
    onPeptideInitialSequenceChange: handleRuntimePeptideInitialSequenceChange,
    onPeptideSequenceMaskChange: handleRuntimePeptideSequenceMaskChange,
    onPeptideIterationsChange: handleRuntimePeptideIterationsChange,
    onPeptidePopulationSizeChange: handleRuntimePeptidePopulationSizeChange,
    onPeptideEliteSizeChange: handleRuntimePeptideEliteSizeChange,
    onPeptideMutationRateChange: handleRuntimePeptideMutationRateChange,
    onPeptideBicyclicLinkerCcdChange: handleRuntimePeptideBicyclicLinkerCcdChange,
    onPeptideBicyclicCysPositionModeChange: handleRuntimePeptideBicyclicCysPositionModeChange,
    onPeptideBicyclicFixTerminalCysChange: handleRuntimePeptideBicyclicFixTerminalCysChange,
    onPeptideBicyclicIncludeExtraCysChange: handleRuntimePeptideBicyclicIncludeExtraCysChange,
    onPeptideBicyclicCys1PosChange: handleRuntimePeptideBicyclicCys1PosChange,
    onPeptideBicyclicCys2PosChange: handleRuntimePeptideBicyclicCys2PosChange,
    onPeptideBicyclicCys3PosChange: handleRuntimePeptideBicyclicCys3PosChange
  });
  const taskHistoryPath = `/projects/${project.id}/tasks`;
  const {
    handleRunAction,
    handleRunCurrentDraft,
    handleRestoreSavedDraft,
    handleResetFromHeader,
    handleWorkspaceFormSubmit,
    handleOpenTaskHistory,
  } = useProjectRunHandlers({
    runDisabled,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saving,
    submitting,
    loading,
    hasUnsavedChanges,
    saveDraft,
    taskHistoryPath,
    setRunRedirectTaskId,
    navigate,
  });

  const leadOptHeaderActionMissing = isLeadOptimizationWorkflow && !leadOptHeaderRunAction;
  const effectiveRunDisabled = runDisabled || leadOptHeaderActionMissing;
  const effectiveRunBlockedReason = leadOptHeaderActionMissing
    ? workspaceTab === 'components'
      ? 'Select at least one fragment to run.'
      : 'Run action is only available in Lead Optimization Components view.'
    : runBlockedReason;
  const headerRuntimeTaskId =
    readText(statusContextTaskRow?.task_id).trim() ||
    readText(activeResultTask?.task_id).trim() ||
    readText(project.task_id).trim();
  const headerRuntimeStateToken = readText(displayTaskState || project.task_state).trim().toUpperCase();
  const showHeaderStopAction =
    isPeptideDesignWorkflow &&
    Boolean(headerRuntimeTaskId) &&
    (headerRuntimeStateToken === 'RUNNING' || headerRuntimeStateToken === 'QUEUED');
  const headerStopRunTitle = headerRuntimeStateToken === 'RUNNING' ? 'Stop run' : 'Cancel queued run';
  const headerStopRunDisabled = !showHeaderStopAction || headerStopRunPending || runSubmitting;
  const handleHeaderRunAction = () => {
    if (isLeadOptimizationWorkflow) {
      if (!leadOptHeaderRunAction || leadOptHeaderRunPending) return;
      setLeadOptHeaderRunPending(true);
      void Promise.resolve(leadOptHeaderRunAction())
        .catch(() => {
          // Lead opt workspace already surfaces query errors.
        })
        .finally(() => {
          setLeadOptHeaderRunPending(false);
        });
      return;
    }
    handleRunAction();
  };
  const handleHeaderStopAction = () => {
    if (!showHeaderStopAction || headerStopRunDisabled) return;
    if (!headerRuntimeTaskId) return;
    const actionToken = headerRuntimeStateToken === 'RUNNING' ? 'stop' : 'cancel';
    if (!window.confirm(`Confirm ${actionToken} for task "${headerRuntimeTaskId}"?`)) return;
    setHeaderStopRunPending(true);
    setError(null);
    void terminateBackendTask(headerRuntimeTaskId)
      .then(async (response) => {
        if (response.terminated !== true) {
          throw new Error(`Backend did not confirm ${actionToken} for task "${headerRuntimeTaskId}".`);
        }
        await loadProject();
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : `Failed to ${actionToken} running task.`);
      })
      .finally(() => {
        setHeaderStopRunPending(false);
      });
  };

  return (
    <>
    <ProjectDetailLayout
      projectName={project.name}
      canDownloadResult={Boolean(
        isLeadOptimizationWorkflow ? (leadOptDownloadRecords.length > 0 || leadOptDownloadTaskId) : defaultDownloadTaskId
      )}
      workflow={{
        shortTitle: workflow.shortTitle,
        runLabel: workflow.runLabel,
        description: workflow.description
      }}
      workspaceTab={workspaceTab}
      componentStepLabel={componentStepLabel}
      taskName={draft.taskName}
      taskSummary={draft.taskSummary}
      isPredictionWorkflow={isPredictionWorkflow}
      isAffinityWorkflow={isAffinityWorkflow}
      isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
      displayTaskState={displayTaskState}
      isActiveRuntime={isActiveRuntime}
      progressPercent={progressPercent}
      waitingSeconds={waitingSeconds}
      totalRuntimeSeconds={totalRuntimeSeconds}
      canEdit={canEdit}
      loading={loading}
      saving={saving}
      submitting={submitting}
      runSubmitting={runSubmitting}
      hasUnsavedChanges={hasUnsavedChanges}
      runMenuOpen={runMenuOpen}
      runDisabled={effectiveRunDisabled}
      runBlockedReason={effectiveRunBlockedReason}
      isRunRedirecting={isRunRedirecting}
      canOpenRunMenu={canOpenRunMenu}
      showHeaderRunAction
      showStopAction={showHeaderStopAction}
      stopSubmitting={headerStopRunPending}
      stopDisabled={headerStopRunDisabled}
      stopTitle={headerStopRunTitle}
      showQuickRunFab={showQuickRunFab}
      taskHistoryPath={taskHistoryPath}
      runSuccessNotice={runSuccessNotice}
      error={error}
      resultError={resultError}
      affinityPreviewError={affinityPreviewError}
      resultChainConsistencyWarning={resultChainConsistencyWarning}
      projectResultsSectionProps={projectResultsSectionProps}
      affinitySectionProps={affinityWorkflowSectionProps}
      leadOptimizationSectionProps={leadOptimizationWorkflowSectionProps}
      predictionSectionProps={predictionWorkflowSectionProps}
      runtimeSettingsProps={workflowRuntimeSettingsSectionProps}
      runActionRef={runActionRef as RefObject<HTMLDivElement>}
      topRunButtonRef={topRunButtonRef as RefObject<HTMLButtonElement>}
      onOpenTaskHistory={handleOpenTaskHistory}
      onDownloadResult={() => {
        setError(null);
        if (isLeadOptimizationWorkflow) {
          void downloadLeadOptCombinedArchive({
            predictionMap: aggregatedLeadOptSnapshotRecord.prediction_by_smiles,
            preferredBackend: aggregatedLeadOptSnapshotRecord.selected_backend,
            projectName: project.name,
            queryId:
              readText(aggregatedLeadOptSnapshotRecord.query_id).trim() ||
              readText(asRecord(aggregatedLeadOptSnapshotRecord.query_result).query_id).trim(),
            fallbackTaskId: leadOptDownloadTaskId,
          }).catch((err) => {
            setError(err instanceof Error ? err.message : 'Failed to download lead-opt result archive.');
          });
          return;
        }
        if (!defaultDownloadTaskId) return;
        void downloadResultFile(defaultDownloadTaskId).catch((err) => {
          setError(err instanceof Error ? err.message : 'Failed to download result archive.');
        });
      }}
      onSaveDraft={() => {
        void saveDraft();
      }}
      onReset={handleResetFromHeader}
      onRunAction={handleHeaderRunAction}
      onStopAction={handleHeaderStopAction}
      onRestoreSavedDraft={handleRestoreSavedDraft}
      onRunCurrentDraft={handleRunCurrentDraft}
      onWorkspaceTabChange={setWorkspaceTab}
      onTaskNameChange={handleTaskNameChange}
      onTaskSummaryChange={handleTaskSummaryChange}
      onWorkspaceFormSubmit={handleWorkspaceFormSubmit}
    />
    </>
  );
}
