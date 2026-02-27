import type { MutableRefObject } from 'react';
import {
  compactResultConfidenceForStorage,
  downloadResultBlob,
  getTaskStatus,
  parseResultBundle,
} from '../../api/backendApi';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import type { Project, ProjectTask, TaskState } from '../../types/models';
import { mergePeptidePreviewIntoProperties } from '../../utils/peptideTaskPreview';
import { normalizeWorkflowKey } from '../../utils/workflows';
import { mapTaskState, readStatusText } from './projectMetrics';

function hasLeadOptMmpOnlySnapshot(task: ProjectTask | null): boolean {
  if (!task) return false;
  if (task.confidence && typeof task.confidence === 'object') {
    const leadOptMmp = (task.confidence as Record<string, unknown>).lead_opt_mmp;
    if (leadOptMmp && typeof leadOptMmp === 'object') return true;
  }
  return String(task.status_text || '').toUpperCase().includes('MMP');
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

const PEPTIDE_REQUEST_OPTION_KEYS = [
  'peptideDesignMode',
  'peptide_design_mode',
  'peptideBinderLength',
  'peptide_binder_length',
  'peptideIterations',
  'peptide_iterations',
  'peptidePopulationSize',
  'peptide_population_size',
  'peptideEliteSize',
  'peptide_elite_size',
  'peptideMutationRate',
  'peptide_mutation_rate'
] as const;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
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

function hasMeaningfulValue(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (typeof value === 'string') return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(asRecord(value)).length > 0;
  return true;
}

function copyMissingFields(
  target: Record<string, unknown>,
  source: Record<string, unknown>,
  keys: readonly string[]
): boolean {
  let changed = false;
  for (const key of keys) {
    const sourceValue = source[key];
    if (!hasMeaningfulValue(sourceValue)) continue;
    if (hasMeaningfulValue(target[key])) continue;
    target[key] = sourceValue;
    changed = true;
  }
  return changed;
}

function hasPeptideSummaryFields(value: Record<string, unknown>): boolean {
  if (Object.keys(asRecord(value.peptide_design)).length > 0) return true;
  if (Object.keys(asRecord(value.progress)).length > 0) return true;
  const requestOptions = asRecord(asRecord(value.request).options);
  return PEPTIDE_REQUEST_OPTION_KEYS.some((key) => hasMeaningfulValue(requestOptions[key]));
}

function mergePeptideSummaryIntoParsedConfidence(
  parsedConfidenceValue: Record<string, unknown>,
  baseConfidenceValue: Record<string, unknown> | null | undefined
): Record<string, unknown> {
  const baseConfidence = asRecord(baseConfidenceValue);
  if (Object.keys(baseConfidence).length === 0 || !hasPeptideSummaryFields(baseConfidence)) {
    return parsedConfidenceValue;
  }

  const merged: Record<string, unknown> = { ...parsedConfidenceValue };

  const mergedRequest = asRecord(merged.request);
  const mergedRequestOptions = { ...asRecord(mergedRequest.options) };
  const baseRequestOptions = asRecord(asRecord(baseConfidence.request).options);
  const requestChanged = copyMissingFields(mergedRequestOptions, baseRequestOptions, PEPTIDE_REQUEST_OPTION_KEYS);
  if (requestChanged) {
    merged.request = {
      ...mergedRequest,
      options: mergedRequestOptions
    };
  }

  const mergedPeptide = { ...asRecord(merged.peptide_design) };
  const basePeptide = asRecord(baseConfidence.peptide_design);
  const peptideChangedBySetup = copyMissingFields(mergedPeptide, basePeptide, PEPTIDE_RUNTIME_SETUP_KEYS);
  const peptideChangedByProgress = copyMissingFields(mergedPeptide, basePeptide, PEPTIDE_RUNTIME_PROGRESS_KEYS);

  const mergedPeptideProgress = { ...asRecord(mergedPeptide.progress) };
  const basePeptideProgress = asRecord(basePeptide.progress);
  const baseTopProgress = asRecord(baseConfidence.progress);
  const peptideProgressChanged =
    copyMissingFields(mergedPeptideProgress, basePeptideProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS) ||
    copyMissingFields(mergedPeptideProgress, baseTopProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS);
  if (peptideProgressChanged) {
    mergedPeptide.progress = mergedPeptideProgress;
  }

  if (
    peptideChangedBySetup ||
    peptideChangedByProgress ||
    peptideProgressChanged ||
    (Object.keys(mergedPeptide).length === 0 && Object.keys(basePeptide).length > 0)
  ) {
    merged.peptide_design = mergedPeptide;
  }

  const mergedProgress = { ...asRecord(merged.progress) };
  const topProgressChanged =
    copyMissingFields(mergedProgress, baseTopProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS) ||
    copyMissingFields(mergedProgress, mergedPeptideProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS);
  if (topProgressChanged) {
    merged.progress = mergedProgress;
  }

  if (!hasMeaningfulValue(merged.best_sequences) && hasMeaningfulValue(baseConfidence.best_sequences)) {
    merged.best_sequences = baseConfidence.best_sequences;
  }
  if (!hasMeaningfulValue(merged.current_best_sequences) && hasMeaningfulValue(baseConfidence.current_best_sequences)) {
    merged.current_best_sequences = baseConfidence.current_best_sequences;
  }

  return merged;
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
    const persistedProjectConfidence = mergePeptideSummaryIntoParsedConfidence(
      persistedProjectConfidenceSource,
      baseProjectConfidence
    );
    const persistedTaskConfidence = mergePeptideSummaryIntoParsedConfidence(
      hasPeptidePayload ? persistedConfidenceCompact : persistedConfidenceFull,
      baseTaskConfidence ?? baseProjectConfidence
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
          const prevPeptide = asRecord(prev.peptide_design);
          const prevProgress = asRecord(prev.progress);
          const nextPeptideProgress = {
            ...asRecord(prevPeptide.progress),
            best_sequences: effectiveRows,
            current_best_sequences: effectiveRows
          };
          return {
            ...prev,
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
      const propertiesPatch = mergePeptidePreviewIntoProperties(baseTaskProperties || {}, persistedProjectConfidence);
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
  options?: { silent?: boolean };
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

  if (!project?.task_id) {
    if (!silent) {
      setError('No task ID yet. Submit a task first.');
    }
    return;
  }

  const activeTaskId = project.task_id.trim();
  if (!activeTaskId) return;
  if (statusRefreshInFlightRef.current.has(activeTaskId)) return;
  statusRefreshInFlightRef.current.add(activeTaskId);

  if (!silent) {
    setError(null);
  }

  try {
    const status = await getTaskStatus(activeTaskId);
    const taskState: TaskState = mapTaskState(status.state);
    const statusText = readStatusText(status);
    setStatusInfo((previous) => {
      const incoming = asRecord(status.info);
      const incomingRows = readPeptideCandidateRowsFromPayload(incoming);
      if (incomingRows.length > 0) {
        return incoming;
      }
      const previousRows = readPeptideCandidateRowsFromPayload(previous);
      if (previousRows.length > 0) {
        return injectPeptideCandidateRowsIntoStatusPayload(incoming, previousRows);
      }
      return Object.keys(incoming).length > 0 ? incoming : null;
    });
    const runtimeInfo = asRecord(status.info);
    const isPeptideDesignWorkflow = normalizeWorkflowKey(project.task_type) === 'peptide_design';
    const nextErrorText = taskState === 'FAILURE' ? statusText : '';
    const runtimeTask = projectTasks.find((item) => item.task_id === activeTaskId) || null;
    const completedAt = taskState === 'SUCCESS' ? new Date().toISOString() : null;
    const submittedAt = runtimeTask?.submitted_at || project.submitted_at;
    const durationSeconds =
      taskState === 'SUCCESS' && submittedAt
        ? (() => {
            const duration = (Date.now() - new Date(submittedAt).getTime()) / 1000;
            return Number.isFinite(duration) ? duration : null;
          })()
        : null;

    const patchData: Partial<Project> = {
      task_state: taskState,
      status_text: statusText,
      error_text: nextErrorText,
    };
    const projectConfidencePatch = isPeptideDesignWorkflow
      ? mergePeptideRuntimeStatusIntoConfidence(project.confidence, runtimeInfo)
      : null;
    if (projectConfidencePatch) {
      patchData.confidence = projectConfidencePatch;
    }

    if (taskState === 'SUCCESS') {
      patchData.completed_at = completedAt;
      patchData.duration_seconds = durationSeconds;
    }

    const shouldPatch =
      project.task_state !== taskState ||
      (project.status_text || '') !== statusText ||
      (project.error_text || '') !== nextErrorText ||
      (taskState === 'SUCCESS' && (!project.completed_at || project.duration_seconds === null)) ||
      Boolean(projectConfidencePatch);

    const next = shouldPatch ? await patch(patchData) : project;

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
    if (taskState === 'SUCCESS' && next?.task_id && !statusLooksLikeMmp && !hasLeadOptMmpOnlySnapshot(runtimeTask)) {
      const resultMode: DownloadResultMode = isPeptideDesignWorkflow ? 'full' : 'view';
      await pullResultForViewer(next.task_id, {
        taskRowId: runtimeTask?.id,
        persistProject: true,
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
