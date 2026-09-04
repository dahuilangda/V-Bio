import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import {
  downloadResultBlob,
  getTaskStatus,
  parseResultBundle,
} from '../../api/backendApi';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import type { Project, ProjectTask, TaskState } from '../../types/models';
import { mergePeptidePreviewIntoProperties } from '../../utils/peptideTaskPreview';
import { hasStoredTaskInputOptions, readTaskInputOptions } from './projectTaskSnapshot';
import {
  derivePersistedResultConfidences,
  hasMeaningfulValue
} from '../../utils/resultConfidenceStorage';
import { normalizeWorkflowKey } from '../../utils/workflows';
import {
  inferTaskStateFromStatusPayload,
  isTransientRuntimeStatusText,
  readTaskRuntimeStatusText as readStatusText
} from '../../utils/taskRuntime';

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
  'generation_total_tasks',
  'generation_completed_tasks',
  'generation_running_tasks',
  'generation_queued_tasks',
  'elapsed_seconds',
  'estimated_remaining_seconds',
  'estimated_completion_time',
  'candidates_evaluated',
  'adaptive_mutation_rate',
  'stagnant_generations',
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

function stripPeptideCandidateRowKeysFromRecord(source: Record<string, unknown>): Record<string, unknown> {
  const next = { ...source };
  for (const key of PEPTIDE_CANDIDATE_ROW_KEYS) {
    delete next[key];
  }
  return next;
}

function injectPeptideCandidateRowsIntoStatusPayload(
  baseValue: unknown,
  rows: Array<Record<string, unknown>>
): Record<string, unknown> {
  const base = stripPeptideCandidateRowKeysFromRecord(asRecord(baseValue));
  const peptide = stripPeptideCandidateRowKeysFromRecord(asRecord(base.peptide_design));
  const progress = stripPeptideCandidateRowKeysFromRecord(asRecord(base.progress));
  const peptideProgress = stripPeptideCandidateRowKeysFromRecord(asRecord(peptide.progress));
  return {
    ...base,
    peptide_design: {
      ...peptide,
      best_sequences: rows,
      candidate_count: rows.length,
      progress: peptideProgress
    },
    progress
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

// Runtime candidate rows accumulate across GA generations. Without a bound the merged
// blob grows every poll and each poll's merge/stringify cost grows with it (this blob
// is compared, re-merged and re-rendered every cycle — the growth is what eventually
// froze the page). The terminal result always comes from the result bundle, not from
// this accumulator, so keeping the newest PEPTIDE_RUNTIME_CANDIDATE_ROW_CAP rows is a
// bound on scratch state, not data loss.
const PEPTIDE_RUNTIME_CANDIDATE_ROW_CAP = 200;

// Preview structure text per candidate can be hundreds of KB per row in the runtime
// status payload. No runtime consumer reads it from the accumulator (the result bundle
// downloaded on SUCCESS is the structure source of truth), and keeping it makes every
// subsequent merge and equality check pay for it.
const PEPTIDE_CANDIDATE_ROW_BULK_KEYS = [
  'structure_text',
  'structureText',
  'cif_text',
  'cifText',
  'pdb_text',
  'pdbText',
  'content'
] as const;

function stripBulkStructureFieldsFromCandidateRow(row: Record<string, unknown>): Record<string, unknown> {
  const next = { ...row };
  for (const key of PEPTIDE_CANDIDATE_ROW_BULK_KEYS) {
    delete next[key];
  }
  return next;
}

function mergePeptideCandidateRows(
  existingRows: Array<Record<string, unknown>>,
  incomingRows: Array<Record<string, unknown>>
): Array<Record<string, unknown>> {
  const normalizedIncoming = incomingRows.map(stripBulkStructureFieldsFromCandidateRow);
  if (existingRows.length === 0) {
    return normalizedIncoming.length > PEPTIDE_RUNTIME_CANDIDATE_ROW_CAP
      ? normalizedIncoming.slice(-PEPTIDE_RUNTIME_CANDIDATE_ROW_CAP)
      : normalizedIncoming;
  }
  if (normalizedIncoming.length === 0) return existingRows;
  const merged = new Map<string, Record<string, unknown>>();
  for (const row of existingRows) {
    merged.set(readPeptideCandidateIdentity(row), row);
  }
  for (const row of normalizedIncoming) {
    const key = readPeptideCandidateIdentity(row);
    const previous = merged.get(key);
    if (!previous) {
      merged.set(key, row);
      continue;
    }
    merged.set(key, mergeRowsPreferRicher(previous, row));
  }
  const values = [...merged.values()];
  return values.length > PEPTIDE_RUNTIME_CANDIDATE_ROW_CAP ? values.slice(-PEPTIDE_RUNTIME_CANDIDATE_ROW_CAP) : values;
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

  const currentPeptide = stripPeptideCandidateRowKeysFromRecord(asRecord(nextConfidence.peptide_design));
  const existingCandidateRows = readPeptideCandidateRowsFromPayload(currentConfidence);
  const mergedCandidateRows = mergePeptideCandidateRows(existingCandidateRows, incomingCandidateRows);
  const mergedPeptideProgress = stripPeptideCandidateRowKeysFromRecord({
    ...asRecord(currentPeptide.progress),
    ...topProgressPatch,
    ...peptideProgressPatch
  });
  const nextPeptide: Record<string, unknown> = {
    ...currentPeptide,
    ...setupPatch,
    ...peptideProgressPatch,
    progress: mergedPeptideProgress
  };
  if (mergedCandidateRows.length > 0) {
    nextPeptide.best_sequences = mergedCandidateRows;
    if (!hasMeaningfulValue(nextPeptide.candidate_count)) {
      nextPeptide.candidate_count = mergedCandidateRows.length;
    }
  }
  nextConfidence.peptide_design = nextPeptide;

  const currentTopProgress = stripPeptideCandidateRowKeysFromRecord(asRecord(nextConfidence.progress));
  const nextProgress: Record<string, unknown> = stripPeptideCandidateRowKeysFromRecord({
    ...currentTopProgress,
    ...topProgressPatch,
    ...peptideProgressPatch
  });
  for (const key of PEPTIDE_CANDIDATE_ROW_KEYS) {
    delete nextConfidence[key];
  }
  nextConfidence.progress = nextProgress;

  return JSON.stringify(nextConfidence) === JSON.stringify(currentConfidence) ? null : nextConfidence;
}

export async function pullResultForViewerTask(params: {
  taskId: string;
  options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode; preferredStructureName?: string };
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
    const blob = await downloadResultBlob(taskId, {
      mode: resultMode,
      preferredStructureName: options?.preferredStructureName
    });
    const parsed = await parseResultBundle(blob, {
      preservePeptideCandidateStructureText: false,
      preferredStructureName: options?.preferredStructureName
    });
    if (!parsed) {
      throw new Error('No structure file was found in the result archive.');
    }
    const parsedConfidence = asRecord(parsed.confidence);
    const baseTaskInputOptions = baseTaskProperties && hasStoredTaskInputOptions({ properties: baseTaskProperties })
      ? readTaskInputOptions({ properties: baseTaskProperties } as unknown as ProjectTask)
      : {};
    const persistedConfidence = derivePersistedResultConfidences({
      parsedConfidenceValue: parsedConfidence,
      baseProjectConfidenceValue: baseProjectConfidence,
      baseTaskConfidenceValue: baseTaskConfidence,
      baseTaskInputOptions
    });
    const persistedProjectConfidence = persistedConfidence.projectConfidence;
    const persistedTaskConfidence = persistedConfidence.taskConfidence;

    setStructureText(parsed.structureText);
    setStructureFormat(parsed.structureFormat);
    setStructureTaskId(taskId);

    if (typeof setStatusInfo === 'function') {
      const effectiveRows = readPeptideCandidateRowsFromPayload(parsedConfidence);
      if (effectiveRows.length > 0) {
        setStatusInfo((previous) => {
          const prev = asRecord(previous);
          const previousTaskScope = readStatusScopeTaskId(prev);
          const scopedPrev = previousTaskScope === normalizeTaskId(taskId) ? prev : {};
          const prevPeptide = stripPeptideCandidateRowKeysFromRecord(asRecord(scopedPrev.peptide_design));
          const prevProgress = stripPeptideCandidateRowKeysFromRecord(asRecord(scopedPrev.progress));
          const scopedTaskId = normalizeTaskId(taskId);
          const nextPeptideProgress = stripPeptideCandidateRowKeysFromRecord(asRecord(prevPeptide.progress));
          const nextStatus = stripPeptideCandidateRowKeysFromRecord(scopedPrev);
          return {
            ...nextStatus,
            __task_id: scopedTaskId,
            peptide_design: {
              ...prevPeptide,
              best_sequences: effectiveRows,
              candidate_count: effectiveRows.length,
              progress: nextPeptideProgress
            },
            progress: prevProgress
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
      const propertiesPatch = baseTaskProperties && hasStoredTaskInputOptions({ properties: baseTaskProperties })
        ? mergePeptidePreviewIntoProperties(baseTaskProperties, persistedTaskConfidence)
        : null;
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
  setProject: Dispatch<SetStateAction<Project | null>>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  pullResultForViewer: (
    taskId: string,
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode }
  ) => Promise<void>;
  options?: { silent?: boolean; taskId?: string };
}): Promise<boolean> {
  const {
    project,
    projectTasks,
    statusRefreshInFlightRef,
    setError,
    setStatusInfo,
    setProject,
    setProjectTasks,
    sortProjectTasks,
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
    return false;
  }

  const requestedTaskId = String(options?.taskId || '').trim();
  const activeTaskId = requestedTaskId || String(project?.task_id || '').trim();
  if (!activeTaskId) {
    if (!silent) {
      setError('No task ID yet. Submit a task first.');
    }
    return false;
  }
  if (statusRefreshInFlightRef.current.has(activeTaskId)) return false;
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

    // Runtime progress (status text, elapsed-derived fields, peptide candidate rows)
    // changes on every poll. It is applied to IN-MEMORY state only: polling must never
    // PATCH the DB per tick, because each PATCH bumps updated_at, which in turn
    // invalidates the polling signatures, the task-detail cache and the snapshot
    // markers — the feedback loop that multiplied requests and froze the page. The DB
    // is written only when the task STATE itself transitions (plus terminal backfill);
    // final result payloads are persisted separately by pullResultForViewer on SUCCESS.
    // Merged ONCE: the project patch intentionally derives from the task row's confidence
    // when it exists (same base as the task patch), so one merge feeds both state writes.
    const peptideConfidencePatch = isPeptideDesignWorkflow
      ? mergePeptideRuntimeStatusIntoConfidence(
          runtimeTask?.confidence && typeof runtimeTask.confidence === 'object'
            ? (runtimeTask.confidence as Record<string, unknown>)
            : project.confidence,
          runtimeInfo
        )
      : null;
    const taskPropertiesPatch =
      isPeptideDesignWorkflow && runtimeTask && hasStoredTaskInputOptions(runtimeTask)
        ? mergePeptidePreviewIntoProperties(runtimeTask.properties, peptideConfidencePatch || runtimeTask.confidence)
        : null;

    // Change signal for the adaptive poll cadence: true when this tick observed anything
    // new (status text, state, terminal fields, peptide progress). Computed from the same
    // snapshot the state updaters below compare against, so the poller learns the answer
    // synchronously without depending on when React flushes the updaters.
    const nextProjectCompletedAt = taskState === 'SUCCESS' ? completedAt : project.completed_at;
    const nextProjectDurationSeconds = taskState === 'SUCCESS' ? durationSeconds : project.duration_seconds;
    const projectChanged =
      isProjectActiveTask &&
      (
        project.task_state !== taskState ||
        (project.status_text || '') !== statusText ||
        (project.error_text || '') !== nextErrorText ||
        (project.completed_at || null) !== (nextProjectCompletedAt || null) ||
        (project.duration_seconds ?? null) !== (nextProjectDurationSeconds ?? null) ||
        peptideConfidencePatch !== null
      );
    const nextTaskCompletedAt = taskState === 'SUCCESS' ? completedAt : runtimeTask?.completed_at;
    const nextTaskDurationSeconds = taskState === 'SUCCESS' ? durationSeconds : runtimeTask?.duration_seconds;
    const taskChanged = Boolean(runtimeTask) && (
      runtimeTask!.task_state !== taskState ||
      (runtimeTask!.status_text || '') !== statusText ||
      (runtimeTask!.error_text || '') !== nextErrorText ||
      (runtimeTask!.completed_at || null) !== (nextTaskCompletedAt || null) ||
      (runtimeTask!.duration_seconds ?? null) !== (nextTaskDurationSeconds ?? null) ||
      peptideConfidencePatch !== null ||
      taskPropertiesPatch !== null
    );
    const hasObservedChanges = projectChanged || taskChanged;

    // Identity stability comes from the gating itself: when nothing changed we never
    // touch state, so downstream `project`/`projectTasks` identities stay stable.
    if (isProjectActiveTask && projectChanged) {
      setProject((prev) => {
        // Stale-write guard: the active task may have switched while this poll ran.
        if (!prev || String(prev.task_id || '').trim() !== activeTaskId) return prev;
        return {
          ...prev,
          task_state: taskState,
          status_text: statusText,
          error_text: nextErrorText,
          ...(taskState === 'SUCCESS' ? { completed_at: completedAt, duration_seconds: durationSeconds } : {}),
          ...(peptideConfidencePatch ? { confidence: peptideConfidencePatch } : {})
        };
      });
    }

    if (runtimeTask && taskChanged) {
      setProjectTasks((prev) =>
        sortProjectTasks(
          prev.map((row) => {
            if (row.id !== runtimeTask.id) return row;
            return {
              ...row,
              task_state: taskState,
              status_text: statusText,
              error_text: nextErrorText,
              ...(taskState === 'SUCCESS' ? { completed_at: completedAt, duration_seconds: durationSeconds } : {}),
              ...(peptideConfidencePatch ? { confidence: peptideConfidencePatch } : {}),
              ...(taskPropertiesPatch ? { properties: taskPropertiesPatch as unknown as ProjectTask['properties'] } : {})
            };
          })
        )
      );
    }

    const persistedProjectState = String(project.task_state || '').trim().toUpperCase();
    const persistedTaskState = String(runtimeTask?.task_state || '').trim().toUpperCase();
    const projectNeedsTerminalBackfill =
      taskState === 'SUCCESS' && (!project.completed_at || project.duration_seconds === null);
    const taskNeedsTerminalBackfill =
      taskState === 'SUCCESS' && (!runtimeTask?.completed_at || runtimeTask.duration_seconds === null);
    const shouldPersistProject =
      isProjectActiveTask && (persistedProjectState !== taskState || projectNeedsTerminalBackfill);
    const shouldPersistTask = Boolean(runtimeTask) && (persistedTaskState !== taskState || taskNeedsTerminalBackfill);

    if (shouldPersistProject) {
      const patchData: Partial<Project> = {
        task_state: taskState,
        status_text: statusText,
        error_text: nextErrorText,
      };
      if (taskState === 'SUCCESS') {
        patchData.completed_at = completedAt;
        patchData.duration_seconds = durationSeconds;
      }
      await patch(patchData);
    }

    if (runtimeTask && shouldPersistTask) {
      const taskPatch: Partial<ProjectTask> = {
        task_state: taskState,
        status_text: statusText,
        error_text: nextErrorText,
      };
      if (taskState === 'SUCCESS') {
        taskPatch.completed_at = completedAt;
        taskPatch.duration_seconds = durationSeconds;
      }
      await patchTask(runtimeTask.id, taskPatch);
    }

    if (
      taskState === 'SUCCESS' &&
      activeTaskId &&
      !isLeadOptimizationWorkflow
    ) {
      const resultMode: DownloadResultMode = 'view';
      await pullResultForViewer(activeTaskId, {
        taskRowId: runtimeTask?.id,
        persistProject: isProjectActiveTask,
        resultMode
      });
    }
    return hasObservedChanges;
  } catch (err) {
    if (!silent) {
      setError(err instanceof Error ? err.message : 'Failed to refresh task status.');
    }
    return false;
  } finally {
    statusRefreshInFlightRef.current.delete(activeTaskId);
  }
}
