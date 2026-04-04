import type { MutableRefObject } from 'react';
import {
  compactResultConfidenceForStorage,
  downloadResultBlob,
  enumerateLeadOptimizationMmp,
  fetchLeadOptimizationMmpQueryStatus,
  getTaskStatus,
  parseResultBundle,
} from '../../api/backendApi';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import type { LeadOptMmpQueryResponse } from '../../api/backendLeadOptimizationApi';
import type { Project, ProjectTask, TaskState } from '../../types/models';
import { mergePeptidePreviewIntoProperties } from '../../utils/peptideTaskPreview';
import {
  hasMeaningfulValue,
  hasPeptideSummaryFields,
  mergePeptideSummaryIntoParsedConfidence
} from '../../utils/resultConfidenceStorage';
import { normalizeWorkflowKey } from '../../utils/workflows';
import { inferTaskStateFromStatusPayload, readStatusText } from './projectMetrics';

function hasLeadOptMmpOnlySnapshot(task: ProjectTask | null): boolean {
  if (!task) return false;
  if (task.confidence && typeof task.confidence === 'object') {
    const leadOptMmp = (task.confidence as Record<string, unknown>).lead_opt_mmp;
    if (leadOptMmp && typeof leadOptMmp === 'object') return true;
  }
  return String(task.status_text || '').toUpperCase().includes('MMP');
}

function isTransientRuntimeStatusText(value: unknown): boolean {
  const normalized = String(value || '').trim().toLowerCase();
  if (!normalized) return false;
  return (
    normalized === 'running' ||
    normalized === 'queued' ||
    normalized === 'pending' ||
    normalized === 'started' ||
    normalized === 'starting' ||
    normalized.includes(' running') ||
    normalized.includes(' queued') ||
    normalized.includes('pending') ||
    normalized.includes('started') ||
    normalized.includes('preparing') ||
    normalized.includes('processing') ||
    normalized.includes('uploading')
  );
}

const PEPTIDE_RUNTIME_SETUP_KEYS = [
  'design_mode',
  'mode',
  'binder_length',
  'length',
  'iterations',
  'population_size',
  'elite_size',
  'mutation_rate'
] as const;

const PEPTIDE_RUNTIME_PROGRESS_KEYS = [
  'current_generation',
  'generation',
  'total_generations',
  'completed_tasks',
  'pending_tasks',
  'total_tasks',
  'candidate_count',
  'best_score',
  'current_best_score',
  'progress_percent',
  'current_status',
  'status_stage',
  'stage',
  'status_message',
  'current_best_sequences',
  'best_sequences',
  'candidates'
] as const;

const PEPTIDE_CANDIDATE_ROW_KEYS = ['current_best_sequences', 'best_sequences', 'candidates'] as const;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readFiniteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeTaskId(value: unknown): string {
  return String(value || '').trim();
}

function readStatusScopeTaskId(value: unknown): string {
  const payload = asRecord(value);
  const direct = normalizeTaskId(payload.__task_id ?? payload.task_id ?? payload.taskId);
  if (direct) return direct;
  const progress = asRecord(payload.progress);
  const fromProgress = normalizeTaskId(progress.task_id ?? progress.taskId);
  if (fromProgress) return fromProgress;
  const peptide = asRecord(payload.peptide_design);
  return normalizeTaskId(peptide.task_id ?? peptide.taskId);
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
}

function hasLeadOptMaterializedSnapshot(task: ProjectTask | null): boolean {
  if (!task) return false;
  const properties = asRecord(task.properties);
  const listMeta = asRecord(properties.lead_opt_list);
  const queryResult = asRecord(listMeta.query_result);
  const confidence = asRecord(task.confidence);
  const leadOptMmp = asRecord(confidence.lead_opt_mmp);
  const confidenceQueryResult = asRecord(leadOptMmp.query_result);
  const queryId = normalizeTaskId(
    listMeta.query_id ||
      queryResult.query_id ||
      leadOptMmp.query_id ||
      confidenceQueryResult.query_id
  );
  if (!queryId) return false;
  if (Object.keys(queryResult).length > 0 || Object.keys(confidenceQueryResult).length > 0) return true;
  if (Array.isArray(listMeta.enumerated_candidates) || Array.isArray(leadOptMmp.enumerated_candidates)) return true;
  if (readFiniteNumber(listMeta.candidate_count) !== null || readFiniteNumber(listMeta.transform_count) !== null) return true;
  if (readFiniteNumber(leadOptMmp.candidate_count) !== null || readFiniteNumber(leadOptMmp.transform_count) !== null) return true;
  return false;
}

function compactLeadOptQueryResultForStorage(resultInput: unknown, taskId: string): Record<string, unknown> {
  const result = asRecord(resultInput);
  const transforms = asRecordArray(result.transforms);
  const globalTransforms = asRecordArray(result.global_transforms);
  const clusters = asRecordArray(result.clusters);
  const count = readFiniteNumber(result.count) ?? transforms.length;
  const globalCount = readFiniteNumber(result.global_count) ?? Math.max(count, globalTransforms.length);
  const groupedByEnvironment = result.grouped_by_environment;
  return {
    query_id: normalizeTaskId(result.query_id),
    task_id: normalizeTaskId(result.task_id) || taskId,
    query_mode: String(result.query_mode || 'one-to-many').trim() || 'one-to-many',
    aggregation_type: String(result.aggregation_type || '').trim(),
    property_targets: asRecord(result.property_targets),
    rule_env_radius: readFiniteNumber(result.rule_env_radius) ?? 1,
    ...(typeof groupedByEnvironment === 'boolean' ? { grouped_by_environment: groupedByEnvironment } : {}),
    mmp_database_id: normalizeTaskId(result.mmp_database_id),
    mmp_database_label: String(result.mmp_database_label || '').trim(),
    mmp_database_schema: String(result.mmp_database_schema || '').trim(),
    cluster_group_by: String(result.cluster_group_by || '').trim(),
    min_pairs: readFiniteNumber(result.min_pairs) ?? 1,
    transforms,
    global_transforms: globalTransforms,
    clusters,
    count,
    global_count: globalCount,
    stats: asRecord(result.stats)
  };
}

function readLeadOptPredictionSummary(task: ProjectTask | null): Record<string, unknown> {
  const properties = asRecord(task?.properties);
  const listSummary = asRecord(asRecord(properties.lead_opt_list).prediction_summary);
  const stateSummary = asRecord(asRecord(properties.lead_opt_state).prediction_summary);
  const confidenceSummary = asRecord(asRecord(asRecord(task?.confidence).lead_opt_mmp).prediction_summary);
  const source = Object.keys(stateSummary).length > 0
    ? stateSummary
    : Object.keys(listSummary).length > 0
      ? listSummary
      : confidenceSummary;
  return {
    total: readFiniteNumber(source.total) ?? 0,
    queued: readFiniteNumber(source.queued) ?? 0,
    running: readFiniteNumber(source.running) ?? 0,
    success: readFiniteNumber(source.success) ?? 0,
    failure: readFiniteNumber(source.failure) ?? 0,
    ...(normalizeTaskId(source.latest_task_id) ? { latest_task_id: normalizeTaskId(source.latest_task_id) } : {})
  };
}

function buildLeadOptCompletedTaskPatch(
  task: ProjectTask,
  taskId: string,
  result: LeadOptMmpQueryResponse,
  enumeratedCandidates: Array<Record<string, unknown>>
): Partial<ProjectTask> {
  const properties = asRecord(task.properties);
  const listMeta = asRecord(properties.lead_opt_list);
  const stateMeta = asRecord(properties.lead_opt_state);
  const confidence = asRecord(task.confidence);
  const leadOptMmp = asRecord(confidence.lead_opt_mmp);
  const compactQueryResult = compactLeadOptQueryResultForStorage(result, taskId);
  const queryId = normalizeTaskId(compactQueryResult.query_id);
  const transformCount = Math.max(
    asRecordArray(compactQueryResult.transforms).length,
    readFiniteNumber(compactQueryResult.count) ?? 0
  );
  const candidateCount = enumeratedCandidates.length;
  const predictionSummary = readLeadOptPredictionSummary(task);
  const predictionStage = String(
    stateMeta.prediction_stage || listMeta.prediction_stage || leadOptMmp.prediction_stage || 'idle'
  ).trim() || 'idle';
  const stage = String(stateMeta.stage || listMeta.stage || leadOptMmp.stage || 'completed').trim() || 'completed';
  const nextProperties = {
    ...properties,
    lead_opt_list: {
      ...listMeta,
      stage,
      prediction_stage: predictionStage,
      query_id: queryId,
      task_id: taskId,
      transform_count: transformCount,
      candidate_count: candidateCount,
      mmp_database_id: normalizeTaskId(compactQueryResult.mmp_database_id),
      mmp_database_label: String(compactQueryResult.mmp_database_label || '').trim(),
      mmp_database_schema: String(compactQueryResult.mmp_database_schema || '').trim(),
      prediction_summary: predictionSummary,
      query_result: compactQueryResult,
      enumerated_candidates: enumeratedCandidates,
      ui_state: asRecord(listMeta.ui_state)
    },
    lead_opt_state: {
      ...stateMeta,
      stage,
      prediction_stage: predictionStage,
      query_id: queryId,
      prediction_summary: predictionSummary,
      prediction_by_smiles: asRecord(stateMeta.prediction_by_smiles),
      reference_prediction_by_backend: asRecord(stateMeta.reference_prediction_by_backend)
    }
  };
  const nextConfidence = {
    ...confidence,
    lead_opt_mmp: {
      ...leadOptMmp,
      stage,
      prediction_stage: predictionStage,
      query_id: queryId,
      task_id: taskId,
      transform_count: transformCount,
      candidate_count: candidateCount,
      mmp_database_id: normalizeTaskId(compactQueryResult.mmp_database_id),
      mmp_database_label: String(compactQueryResult.mmp_database_label || '').trim(),
      mmp_database_schema: String(compactQueryResult.mmp_database_schema || '').trim(),
      prediction_summary: predictionSummary,
      prediction_by_smiles: asRecord(leadOptMmp.prediction_by_smiles),
      reference_prediction_by_backend: asRecord(leadOptMmp.reference_prediction_by_backend),
      query_result: compactQueryResult,
      enumerated_candidates: enumeratedCandidates
    }
  };
  return {
    task_state: 'SUCCESS',
    status_text: `MMP complete (${transformCount} transforms, ${candidateCount} rows). Scoring not started.`,
    error_text: '',
    completed_at: task.completed_at || new Date().toISOString(),
    properties: nextProperties as unknown as ProjectTask['properties'],
    confidence: nextConfidence as unknown as ProjectTask['confidence']
  };
}

function summarizeLeadOptTerminalPredictions(task: ProjectTask | null): { total: number; success: number; failure: number } {
  const properties = asRecord(task?.properties);
  const stateMeta = asRecord(properties.lead_opt_state);
  const confidence = asRecord(task?.confidence);
  const leadOptMmp = asRecord(confidence.lead_opt_mmp);
  const merged = {
    ...asRecord(leadOptMmp.reference_prediction_by_backend),
    ...asRecord(stateMeta.reference_prediction_by_backend),
    ...asRecord(leadOptMmp.prediction_by_smiles),
    ...asRecord(stateMeta.prediction_by_smiles)
  };
  let total = 0;
  let success = 0;
  let failure = 0;
  for (const value of Object.values(merged)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
    total += 1;
    const state = String((value as Record<string, unknown>).state || '').trim().toUpperCase();
    if (state === 'SUCCESS') success += 1;
    else if (state === 'FAILURE') failure += 1;
  }
  return { total, success, failure };
}

function readLeadOptTerminalStatusText(task: ProjectTask | null, taskState: TaskState, fallback: string): string {
  const properties = asRecord(task?.properties);
  const listMeta = asRecord(properties.lead_opt_list);
  const queryResult = asRecord(listMeta.query_result);
  const confidence = asRecord(task?.confidence);
  const leadOptMmp = asRecord(confidence.lead_opt_mmp);
  const transformCount = Math.max(
    readFiniteNumber(listMeta.transform_count) ?? 0,
    readFiniteNumber(leadOptMmp.transform_count) ?? 0,
    readFiniteNumber(queryResult.count) ?? 0
  );
  const candidateCount = Math.max(
    readFiniteNumber(listMeta.candidate_count) ?? 0,
    readFiniteNumber(leadOptMmp.candidate_count) ?? 0,
    Array.isArray(listMeta.enumerated_candidates) ? listMeta.enumerated_candidates.length : 0,
    Array.isArray(leadOptMmp.enumerated_candidates) ? leadOptMmp.enumerated_candidates.length : 0
  );
  const queryId = normalizeTaskId(listMeta.query_id || queryResult.query_id || leadOptMmp.query_id);
  if (queryId || transformCount > 0 || candidateCount > 0) {
    if (taskState === 'SUCCESS') {
      return `MMP complete (${transformCount} transforms, ${candidateCount} rows). Scoring not started.`;
    }
    return fallback;
  }

  const summary = summarizeLeadOptTerminalPredictions(task);
  if (summary.total > 0) {
    if (taskState === 'SUCCESS') {
      return `Scoring complete (${summary.success}/${Math.max(1, summary.total)})`;
    }
    if (taskState === 'FAILURE') {
      return summary.success === 0
        ? `Scoring complete (0/${Math.max(1, summary.total)})`
        : `Scoring complete (${summary.success}/${Math.max(1, summary.total)})`;
    }
  }

  return taskState === 'SUCCESS' ? 'Task completed.' : fallback;
}

export async function materializeLeadOptCompletedTask(params: {
  task: ProjectTask | null;
  taskId: string;
  persistTask: (taskRowId: string, patch: Partial<ProjectTask>) => Promise<ProjectTask | null>;
}): Promise<ProjectTask | null> {
  const { task, taskId, persistTask } = params;
  if (!task) return null;
  const normalizedTaskId = normalizeTaskId(taskId);
  if (!normalizedTaskId) return null;
  if (hasLeadOptMaterializedSnapshot(task)) return task;

  const status = await fetchLeadOptimizationMmpQueryStatus(normalizedTaskId);
  const state = String(status.state || '').trim().toUpperCase();
  const result = status.result || null;
  if (state !== 'SUCCESS' || !result) return null;

  const compactQueryResult = compactLeadOptQueryResultForStorage(result, normalizedTaskId);
  const queryId = normalizeTaskId(compactQueryResult.query_id);
  const transformCount = Math.max(
    asRecordArray(compactQueryResult.transforms).length,
    readFiniteNumber(compactQueryResult.count) ?? 0
  );
  if (!queryId && transformCount > 0) {
    throw new Error(`Lead opt task ${normalizedTaskId} finished without query_id.`);
  }

  let enumeratedCandidates: Array<Record<string, unknown>> = [];
  if (queryId && transformCount > 0) {
    const enumerate = await enumerateLeadOptimizationMmp({
      query_id: queryId,
      task_id: normalizedTaskId,
      property_constraints: {},
      max_candidates: Math.min(1000, Math.max(200, transformCount)),
      compact: true
    });
    enumeratedCandidates = asRecordArray(enumerate.candidates);
  }

  const patch = buildLeadOptCompletedTaskPatch(task, normalizedTaskId, result, enumeratedCandidates);
  return await persistTask(task.id, patch);
}

function readPeptideCandidateRowsFromPayload(value: unknown): Array<Record<string, unknown>> {
  const payload = asRecord(value);
  const direct = asRecordArray(payload.best_sequences);
  if (direct.length > 0) return direct;
  const directCurrent = asRecordArray(payload.current_best_sequences);
  if (directCurrent.length > 0) return directCurrent;
  const peptide = asRecord(payload.peptide_design);
  const peptideBest = asRecordArray(peptide.best_sequences);
  if (peptideBest.length > 0) return peptideBest;
  const peptideCurrent = asRecordArray(peptide.current_best_sequences);
  if (peptideCurrent.length > 0) return peptideCurrent;
  const progress = asRecord(payload.progress);
  const progressBest = asRecordArray(progress.best_sequences);
  if (progressBest.length > 0) return progressBest;
  return asRecordArray(progress.current_best_sequences);
}

function injectPeptideCandidateRowsIntoStatusPayload(
  baseValue: unknown,
  rows: Array<Record<string, unknown>>
): Record<string, unknown> {
  const base = asRecord(baseValue);
  const peptide = asRecord(base.peptide_design);
  const progress = asRecord(base.progress);
  const peptideProgress = asRecord(peptide.progress);
  return {
    ...base,
    peptide_design: {
      ...peptide,
      best_sequences: rows,
      current_best_sequences: rows,
      progress: {
        ...peptideProgress,
        best_sequences: rows,
        current_best_sequences: rows
      }
    },
    best_sequences: rows,
    current_best_sequences: rows,
    progress: {
      ...progress,
      best_sequences: rows,
      current_best_sequences: rows
    }
  };
}

function readPeptideCandidateIdentity(row: Record<string, unknown>): string {
  const sequence = String(
    row.peptide_sequence ?? row.binder_sequence ?? row.candidate_sequence ?? row.designed_sequence ?? row.sequence ?? ''
  )
    .trim()
    .toUpperCase();
  const generation = String(row.generation ?? row.iteration ?? row.iter ?? '').trim();
  const rank = String(row.rank ?? row.ranking ?? row.order ?? '').trim();
  const rowId = String(row.id ?? row.structure_name ?? '').trim();
  if (sequence) return `seq:${sequence}|gen:${generation}|rank:${rank}`;
  if (rowId) return `id:${rowId}|gen:${generation}|rank:${rank}`;
  return JSON.stringify(row);
}

function countFiniteNumbers(value: unknown): number {
  if (Array.isArray(value)) {
    return value.filter((item) => typeof item === 'number' && Number.isFinite(item)).length;
  }
  if (value && typeof value === 'object') {
    return Object.values(value as Record<string, unknown>).reduce<number>(
      (sum, item) => sum + countFiniteNumbers(item),
      0
    );
  }
  return 0;
}

function peptideCandidateRowRichness(row: Record<string, unknown>): number {
  let score = 0;
  const structureText = String(
    row.structure_text ?? row.structureText ?? row.cif_text ?? row.pdb_text ?? row.content ?? ''
  ).trim();
  if (structureText) score += 8;

  const residueCount = Math.max(
    countFiniteNumbers(row.residue_plddt),
    countFiniteNumbers(row.residue_plddts),
    countFiniteNumbers(row.per_residue_plddt),
    countFiniteNumbers(row.aa_plddt)
  );
  if (residueCount >= 4) score += 6;

  const residueByChainCount = Math.max(
    countFiniteNumbers(row.residue_plddt_by_chain),
    countFiniteNumbers(row.residuePlddtByChain),
    countFiniteNumbers(row.chain_residue_plddt)
  );
  if (residueByChainCount >= 4) score += 4;

  if (hasMeaningfulValue(row.pair_iptm) || hasMeaningfulValue(row.iptm)) score += 2;
  if (hasMeaningfulValue(row.binder_avg_plddt) || hasMeaningfulValue(row.plddt)) score += 1;
  return score;
}

function mergeRowsPreferRicher(
  current: Record<string, unknown>,
  incoming: Record<string, unknown>
): Record<string, unknown> {
  const currentRichness = peptideCandidateRowRichness(current);
  const incomingRichness = peptideCandidateRowRichness(incoming);
  const primary = currentRichness >= incomingRichness ? current : incoming;
  const secondary = primary === current ? incoming : current;
  const merged: Record<string, unknown> = { ...secondary };
  for (const [key, value] of Object.entries(primary)) {
    if (!hasMeaningfulValue(value)) continue;
    merged[key] = value;
  }
  return merged;
}

function mergePeptideCandidateRows(
  existingRows: Array<Record<string, unknown>>,
  incomingRows: Array<Record<string, unknown>>
): Array<Record<string, unknown>> {
  if (existingRows.length === 0) return incomingRows;
  if (incomingRows.length === 0) return existingRows;
  const merged = new Map<string, Record<string, unknown>>();
  for (const row of existingRows) {
    merged.set(readPeptideCandidateIdentity(row), row);
  }
  for (const row of incomingRows) {
    const key = readPeptideCandidateIdentity(row);
    const previous = merged.get(key);
    if (!previous) {
      merged.set(key, row);
      continue;
    }
    merged.set(key, mergeRowsPreferRicher(previous, row));
  }
  return [...merged.values()];
}

function pickRecordFields(source: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const next: Record<string, unknown> = {};
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) continue;
    const value = source[key];
    if (value === undefined || value === null || value === '') continue;
    next[key] = value;
  }
  return next;
}

function mergePeptideRuntimeStatusIntoConfidence(
  currentConfidenceValue: unknown,
  statusInfo: Record<string, unknown>
): Record<string, unknown> | null {
  const info = asRecord(statusInfo);
  if (Object.keys(info).length === 0) return null;
  const incomingCandidateRows = readPeptideCandidateRowsFromPayload(info);

  const statusPeptide = asRecord(info.peptide_design);
  const statusPeptideProgress = asRecord(statusPeptide.progress);
  const statusTopProgress = asRecord(info.progress);
  const statusRequest = asRecord(info.request);
  const statusRequestOptions = asRecord(statusRequest.options);
  const statusTopOptions = asRecord(info.options);

  const setupPatch = pickRecordFields(statusPeptide, PEPTIDE_RUNTIME_SETUP_KEYS);
  const pickProgressWithoutCandidateRows = (source: Record<string, unknown>) => {
    const patch = pickRecordFields(source, PEPTIDE_RUNTIME_PROGRESS_KEYS);
    for (const key of PEPTIDE_CANDIDATE_ROW_KEYS) {
      delete patch[key];
    }
    return patch;
  };
  const peptideProgressPatch = {
    ...pickProgressWithoutCandidateRows(statusPeptide),
    ...pickProgressWithoutCandidateRows(statusPeptideProgress)
  };
  const topProgressPatch = pickProgressWithoutCandidateRows(statusTopProgress);
  const optionsPatch = Object.keys(statusRequestOptions).length > 0 ? statusRequestOptions : statusTopOptions;

  if (
    Object.keys(setupPatch).length === 0 &&
    Object.keys(peptideProgressPatch).length === 0 &&
    Object.keys(topProgressPatch).length === 0 &&
    Object.keys(optionsPatch).length === 0 &&
    incomingCandidateRows.length === 0
  ) {
    return null;
  }

  const currentConfidence = asRecord(currentConfidenceValue);
  const nextConfidence: Record<string, unknown> = { ...currentConfidence };

  if (Object.keys(optionsPatch).length > 0) {
    const currentRequest = asRecord(nextConfidence.request);
    nextConfidence.request = {
      ...currentRequest,
      options: {
        ...asRecord(currentRequest.options),
        ...optionsPatch
      }
    };
  }

  const currentPeptide = asRecord(nextConfidence.peptide_design);
  const existingCandidateRows = readPeptideCandidateRowsFromPayload(currentConfidence);
  const mergedCandidateRows = mergePeptideCandidateRows(existingCandidateRows, incomingCandidateRows);
  const mergedPeptideProgress = {
    ...asRecord(currentPeptide.progress),
    ...topProgressPatch,
    ...peptideProgressPatch
  };
  const nextPeptide: Record<string, unknown> = {
    ...currentPeptide,
    ...setupPatch,
    ...peptideProgressPatch,
    progress: mergedPeptideProgress
  };
  if (mergedCandidateRows.length > 0) {
    nextPeptide.best_sequences = mergedCandidateRows;
    nextPeptide.current_best_sequences = mergedCandidateRows;
    if (!hasMeaningfulValue(nextPeptide.candidate_count)) {
      nextPeptide.candidate_count = mergedCandidateRows.length;
    }
    mergedPeptideProgress.best_sequences = mergedCandidateRows;
    mergedPeptideProgress.current_best_sequences = mergedCandidateRows;
  }
  nextConfidence.peptide_design = nextPeptide;

  const currentTopProgress = asRecord(nextConfidence.progress);
  const nextProgress: Record<string, unknown> = {
    ...currentTopProgress,
    ...topProgressPatch,
    ...peptideProgressPatch
  };
  if (mergedCandidateRows.length > 0) {
    nextConfidence.best_sequences = mergedCandidateRows;
    nextConfidence.current_best_sequences = mergedCandidateRows;
    nextProgress.best_sequences = mergedCandidateRows;
    nextProgress.current_best_sequences = mergedCandidateRows;
  }
  nextConfidence.progress = nextProgress;

  return JSON.stringify(nextConfidence) === JSON.stringify(currentConfidence) ? null : nextConfidence;
}

export async function pullResultForViewerTask(params: {
  taskId: string;
  options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode };
  baseProjectConfidence?: Record<string, unknown> | null;
  baseTaskConfidence?: Record<string, unknown> | null;
  baseTaskProperties?: Record<string, unknown> | null;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  setStatusInfo?: (
    value:
      | Record<string, unknown>
      | null
      | ((prev: Record<string, unknown> | null) => Record<string, unknown> | null)
  ) => void;
  setStructureText: (value: string) => void;
  setStructureFormat: (value: 'cif' | 'pdb') => void;
  setStructureTaskId: (value: string | null) => void;
  setResultError: (value: string | null) => void;
}): Promise<void> {
  const {
    taskId,
    options,
    baseProjectConfidence,
    baseTaskConfidence,
    baseTaskProperties,
    patch,
    patchTask,
    setStatusInfo,
    setStructureText,
    setStructureFormat,
    setStructureTaskId,
    setResultError,
  } = params;

  const shouldPersistProject = options?.persistProject !== false;
  const resultMode = options?.resultMode || 'view';
  setResultError(null);
  try {
    const blob = await downloadResultBlob(taskId, { mode: resultMode });
    const parsed = await parseResultBundle(blob, { preservePeptideCandidateStructureText: true });
    if (!parsed) {
      throw new Error('No structure file was found in the result archive.');
    }
    const parsedConfidence = asRecord(parsed.confidence);
    const persistedConfidenceFull = compactResultConfidenceForStorage(parsedConfidence, {
      preservePeptideCandidateStructureText: true
    });
    const persistedConfidenceCompact = compactResultConfidenceForStorage(parsedConfidence, {
      preservePeptideCandidateStructureText: false
    });
    const hasPeptidePayload = hasPeptideSummaryFields(persistedConfidenceFull);
    const persistedProjectConfidenceSource = hasPeptidePayload ? persistedConfidenceCompact : persistedConfidenceFull;
    const persistedProjectConfidence = hasPeptidePayload
      ? persistedProjectConfidenceSource
      : mergePeptideSummaryIntoParsedConfidence(
          persistedProjectConfidenceSource,
          baseProjectConfidence
        );
    const taskConfidenceBase = baseTaskConfidence ?? (hasPeptidePayload ? null : baseProjectConfidence);
    const persistedTaskConfidence = mergePeptideSummaryIntoParsedConfidence(
      hasPeptidePayload ? persistedConfidenceCompact : persistedConfidenceFull,
      taskConfidenceBase
    );

    setStructureText(parsed.structureText);
    setStructureFormat(parsed.structureFormat);
    setStructureTaskId(taskId);

    if (typeof setStatusInfo === 'function') {
      const candidateRows = asRecordArray(parsedConfidence.best_sequences);
      const peptideDesign = asRecord(parsedConfidence.peptide_design);
      const peptideBestRows = asRecordArray(peptideDesign.best_sequences);
      const effectiveRows = peptideBestRows.length > 0 ? peptideBestRows : candidateRows;
      if (effectiveRows.length > 0) {
        setStatusInfo((previous) => {
          const prev = asRecord(previous);
          const previousTaskScope = readStatusScopeTaskId(prev);
          const scopedPrev = previousTaskScope === normalizeTaskId(taskId) ? prev : {};
          const prevPeptide = asRecord(scopedPrev.peptide_design);
          const prevProgress = asRecord(scopedPrev.progress);
          const scopedTaskId = normalizeTaskId(taskId);
          const nextPeptideProgress = {
            ...asRecord(prevPeptide.progress),
            best_sequences: effectiveRows,
            current_best_sequences: effectiveRows
          };
          return {
            ...scopedPrev,
            __task_id: scopedTaskId,
            peptide_design: {
              ...prevPeptide,
              best_sequences: effectiveRows,
              current_best_sequences: effectiveRows,
              progress: nextPeptideProgress
            },
            best_sequences: effectiveRows,
            current_best_sequences: effectiveRows,
            progress: {
              ...prevProgress,
              best_sequences: effectiveRows,
              current_best_sequences: effectiveRows
            }
          };
        });
      }
    }

    if (shouldPersistProject) {
      await patch({
        confidence: persistedProjectConfidence,
        affinity: parsed.affinity,
        structure_name: parsed.structureName,
      });
    }
    if (options?.taskRowId) {
      const propertiesPatch = mergePeptidePreviewIntoProperties(baseTaskProperties || {}, persistedTaskConfidence);
      await patchTask(options.taskRowId, {
        confidence: persistedTaskConfidence,
        affinity: parsed.affinity,
        structure_name: parsed.structureName,
        ...(propertiesPatch ? { properties: propertiesPatch as unknown as ProjectTask['properties'] } : {})
      });
    }
  } catch (err) {
    setStructureTaskId(null);
    setResultError(err instanceof Error ? err.message : 'Failed to parse downloaded result.');
  }
}

export async function refreshTaskStatus(params: {
  project: Project | null;
  projectTasks: ProjectTask[];
  statusRefreshInFlightRef: MutableRefObject<Set<string>>;
  setError: (value: string | null) => void;
  setStatusInfo: (
    value:
      | Record<string, unknown>
      | null
      | ((prev: Record<string, unknown> | null) => Record<string, unknown> | null)
  ) => void;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  pullResultForViewer: (
    taskId: string,
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode }
  ) => Promise<void>;
  options?: { silent?: boolean; taskId?: string };
}): Promise<void> {
  const {
    project,
    projectTasks,
    statusRefreshInFlightRef,
    setError,
    setStatusInfo,
    patch,
    patchTask,
    pullResultForViewer,
    options,
  } = params;

  const silent = Boolean(options?.silent);
  if (!project) {
    if (!silent) {
      setError('Project is not loaded yet.');
    }
    return;
  }

  const requestedTaskId = String(options?.taskId || '').trim();
  const activeTaskId = requestedTaskId || String(project?.task_id || '').trim();
  if (!activeTaskId) {
    if (!silent) {
      setError('No task ID yet. Submit a task first.');
    }
    return;
  }
  if (statusRefreshInFlightRef.current.has(activeTaskId)) return;
  statusRefreshInFlightRef.current.add(activeTaskId);

  if (!silent) {
    setError(null);
  }

  try {
    const status = await getTaskStatus(activeTaskId);
    const runtimeTask = projectTasks.find((item) => item.task_id === activeTaskId) || null;
    const taskState: TaskState = inferTaskStateFromStatusPayload(
      status,
      runtimeTask?.task_state || project.task_state
    );
    const isPeptideDesignWorkflow = normalizeWorkflowKey(project.task_type) === 'peptide_design';
    const isLeadOptimizationWorkflow = normalizeWorkflowKey(project.task_type) === 'lead_optimization';
    const rawStatusText = readStatusText(status);
    const statusText =
      (taskState === 'SUCCESS' || taskState === 'FAILURE' || taskState === 'REVOKED') &&
      isLeadOptimizationWorkflow &&
      isTransientRuntimeStatusText(rawStatusText)
        ? readLeadOptTerminalStatusText(runtimeTask, taskState, rawStatusText)
        : rawStatusText;
    setStatusInfo((previous) => {
      const incoming = asRecord(status.info);
      const scopedIncoming = {
        ...incoming,
        __task_id: activeTaskId
      };
      const incomingRows = readPeptideCandidateRowsFromPayload(incoming);
      if (incomingRows.length > 0) {
        return scopedIncoming;
      }
      const previousRows = readPeptideCandidateRowsFromPayload(previous);
      const previousScopeTaskId = readStatusScopeTaskId(previous);
      if (previousRows.length > 0 && previousScopeTaskId === activeTaskId) {
        return injectPeptideCandidateRowsIntoStatusPayload(scopedIncoming, previousRows);
      }
      return Object.keys(incoming).length > 0 ? scopedIncoming : null;
    });
    const runtimeInfo = asRecord(status.info);
    const isProjectActiveTask = String(project?.task_id || '').trim() === activeTaskId;
    const nextErrorText = taskState === 'FAILURE' ? statusText : '';
    const completedAt = taskState === 'SUCCESS' ? new Date().toISOString() : null;
    const submittedAt = runtimeTask?.submitted_at || project.submitted_at;
    const durationSeconds =
      taskState === 'SUCCESS' && submittedAt
        ? (() => {
            const duration = (Date.now() - new Date(submittedAt).getTime()) / 1000;
            return Number.isFinite(duration) ? duration : null;
          })()
        : null;
    if (
      taskState === 'SUCCESS' &&
      isLeadOptimizationWorkflow &&
      runtimeTask &&
      !hasLeadOptMaterializedSnapshot(runtimeTask)
    ) {
      const materializedLeadOptTask = await materializeLeadOptCompletedTask({
        task: runtimeTask,
        taskId: activeTaskId,
        persistTask: patchTask
      });
      if (materializedLeadOptTask) {
        if (isProjectActiveTask) {
          const nextStatusText = String(materializedLeadOptTask.status_text || statusText).trim();
          const nextCompletedAt = materializedLeadOptTask.completed_at || new Date().toISOString();
          const nextDurationSeconds =
            materializedLeadOptTask.duration_seconds ??
            (submittedAt
              ? (() => {
                  const duration = (Date.now() - new Date(submittedAt).getTime()) / 1000;
                  return Number.isFinite(duration) ? duration : null;
                })()
              : null);
          const shouldPatchProject =
            project.task_state !== 'SUCCESS' ||
            (project.status_text || '') !== nextStatusText ||
            (project.error_text || '') !== '' ||
            (project.completed_at || null) !== (nextCompletedAt || null) ||
            (project.duration_seconds ?? null) !== (nextDurationSeconds ?? null);
          if (shouldPatchProject) {
            await patch({
              task_state: 'SUCCESS',
              status_text: nextStatusText,
              error_text: '',
              completed_at: nextCompletedAt,
              duration_seconds: nextDurationSeconds
            });
          }
        }
        return;
      }
    }

    const patchData: Partial<Project> = {
      task_state: taskState,
      status_text: statusText,
      error_text: nextErrorText,
    };
    const projectConfidenceBase =
      runtimeTask?.confidence && typeof runtimeTask.confidence === 'object'
        ? (runtimeTask.confidence as Record<string, unknown>)
        : project.confidence;
    const projectConfidencePatch = isPeptideDesignWorkflow
      ? mergePeptideRuntimeStatusIntoConfidence(projectConfidenceBase, runtimeInfo)
      : null;
    if (projectConfidencePatch) {
      patchData.confidence = projectConfidencePatch;
    }

    if (taskState === 'SUCCESS') {
      patchData.completed_at = completedAt;
      patchData.duration_seconds = durationSeconds;
    }

    const shouldPatchProject =
      Boolean(project) &&
      isProjectActiveTask &&
      (
        project.task_state !== taskState ||
        (project.status_text || '') !== statusText ||
        (project.error_text || '') !== nextErrorText ||
        (taskState === 'SUCCESS' && (!project.completed_at || project.duration_seconds === null)) ||
        Boolean(projectConfidencePatch)
      );

    if (shouldPatchProject) {
      await patch(patchData);
    }

    if (runtimeTask) {
      const taskPatch: Partial<ProjectTask> = {
        task_state: taskState,
        status_text: statusText,
        error_text: nextErrorText,
      };
      const taskConfidencePatch = isPeptideDesignWorkflow
        ? mergePeptideRuntimeStatusIntoConfidence(runtimeTask.confidence, runtimeInfo)
        : null;
      const taskPropertiesPatch = isPeptideDesignWorkflow
        ? mergePeptidePreviewIntoProperties(runtimeTask.properties, taskConfidencePatch || runtimeTask.confidence)
        : null;
      if (taskConfidencePatch) {
        taskPatch.confidence = taskConfidencePatch;
      }
      if (taskPropertiesPatch) {
        taskPatch.properties = taskPropertiesPatch as unknown as ProjectTask['properties'];
      }
      if (taskState === 'SUCCESS') {
        taskPatch.completed_at = completedAt;
        taskPatch.duration_seconds = durationSeconds;
      }
      const shouldPatchTask =
        runtimeTask.task_state !== taskState ||
        (runtimeTask.status_text || '') !== statusText ||
        (runtimeTask.error_text || '') !== nextErrorText ||
        (taskState === 'SUCCESS' && (!runtimeTask.completed_at || runtimeTask.duration_seconds === null)) ||
        Boolean(taskConfidencePatch) ||
        Boolean(taskPropertiesPatch);
      if (shouldPatchTask) {
        await patchTask(runtimeTask.id, taskPatch);
      }
    }

    const statusLooksLikeMmp = String(statusText || '').toUpperCase().includes('MMP');
    if (
      taskState === 'SUCCESS' &&
      activeTaskId &&
      !isLeadOptimizationWorkflow &&
      !statusLooksLikeMmp &&
      !hasLeadOptMmpOnlySnapshot(runtimeTask)
    ) {
      const resultMode: DownloadResultMode = 'view';
      await pullResultForViewer(activeTaskId, {
        taskRowId: runtimeTask?.id,
        persistProject: isProjectActiveTask,
        resultMode
      });
    }
  } catch (err) {
    if (!silent) {
      setError(err instanceof Error ? err.message : 'Failed to refresh task status.');
    }
  } finally {
    statusRefreshInFlightRef.current.delete(activeTaskId);
  }
}
