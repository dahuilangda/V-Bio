import type { MutableRefObject } from 'react';
import {
  downloadResultBlob,
  getTaskStatus,
  parseResultBundle,
} from '../../api/backendApi';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import type { Project, ProjectTask, TaskState } from '../../types/models';
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

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
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

  const statusPeptide = asRecord(info.peptide_design);
  const statusPeptideProgress = asRecord(statusPeptide.progress);
  const statusTopProgress = asRecord(info.progress);
  const statusRequest = asRecord(info.request);
  const statusRequestOptions = asRecord(statusRequest.options);
  const statusTopOptions = asRecord(info.options);

  const setupPatch = pickRecordFields(statusPeptide, PEPTIDE_RUNTIME_SETUP_KEYS);
  const peptideProgressPatch = {
    ...pickRecordFields(statusPeptide, PEPTIDE_RUNTIME_PROGRESS_KEYS),
    ...pickRecordFields(statusPeptideProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS)
  };
  const topProgressPatch = pickRecordFields(statusTopProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS);
  const optionsPatch = Object.keys(statusRequestOptions).length > 0 ? statusRequestOptions : statusTopOptions;

  if (
    Object.keys(setupPatch).length === 0 &&
    Object.keys(peptideProgressPatch).length === 0 &&
    Object.keys(topProgressPatch).length === 0 &&
    Object.keys(optionsPatch).length === 0
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
  const mergedPeptideProgress = {
    ...asRecord(currentPeptide.progress),
    ...topProgressPatch,
    ...peptideProgressPatch
  };
  nextConfidence.peptide_design = {
    ...currentPeptide,
    ...setupPatch,
    ...peptideProgressPatch,
    progress: mergedPeptideProgress
  };

  const currentTopProgress = asRecord(nextConfidence.progress);
  nextConfidence.progress = {
    ...currentTopProgress,
    ...topProgressPatch,
    ...peptideProgressPatch
  };

  return JSON.stringify(nextConfidence) === JSON.stringify(currentConfidence) ? null : nextConfidence;
}

export async function pullResultForViewerTask(params: {
  taskId: string;
  options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode };
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  setStructureText: (value: string) => void;
  setStructureFormat: (value: 'cif' | 'pdb') => void;
  setStructureTaskId: (value: string | null) => void;
  setResultError: (value: string | null) => void;
}): Promise<void> {
  const {
    taskId,
    options,
    patch,
    patchTask,
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
    const parsed = await parseResultBundle(blob);
    if (!parsed) {
      throw new Error('No structure file was found in the result archive.');
    }

    setStructureText(parsed.structureText);
    setStructureFormat(parsed.structureFormat);
    setStructureTaskId(taskId);

    if (shouldPersistProject) {
      await patch({
        confidence: parsed.confidence,
        affinity: parsed.affinity,
        structure_name: parsed.structureName,
      });
    }
    if (options?.taskRowId) {
      await patchTask(options.taskRowId, {
        confidence: parsed.confidence,
        affinity: parsed.affinity,
        structure_name: parsed.structureName,
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
  setStatusInfo: (value: Record<string, unknown> | null) => void;
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
    setStatusInfo(status.info ?? null);
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
      if (taskConfidencePatch) {
        taskPatch.confidence = taskConfidencePatch;
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
        Boolean(taskConfidencePatch);
      if (shouldPatchTask) {
        await patchTask(runtimeTask.id, taskPatch);
      }
    }

    const statusLooksLikeMmp = String(statusText || '').toUpperCase().includes('MMP');
    if (taskState === 'SUCCESS' && next?.task_id && !statusLooksLikeMmp && !hasLeadOptMmpOnlySnapshot(runtimeTask)) {
      const resultMode: DownloadResultMode = 'view';
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
