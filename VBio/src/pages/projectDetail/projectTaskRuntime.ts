import type { MutableRefObject } from 'react';
import {
  downloadResultBlob,
  getTaskStatus,
  parseResultBundle,
} from '../../api/backendApi';
import type { Project, ProjectTask, TaskState } from '../../types/models';
import { mapTaskState, readStatusText } from './projectMetrics';

function hasLeadOptMmpOnlySnapshot(task: ProjectTask | null): boolean {
  if (!task) return false;
  if (String(task.structure_name || '').trim().length > 0) return false;
  if (task.confidence && typeof task.confidence === 'object') {
    const leadOptMmp = (task.confidence as Record<string, unknown>).lead_opt_mmp;
    if (leadOptMmp && typeof leadOptMmp === 'object') return true;
  }
  return String(task.status_text || '').toUpperCase().includes('MMP');
}

export async function pullResultForViewerTask(params: {
  taskId: string;
  options?: { taskRowId?: string; persistProject?: boolean };
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
  setResultError(null);
  try {
    const blob = await downloadResultBlob(taskId, { mode: 'view' });
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
  pullResultForViewer: (taskId: string, options?: { taskRowId?: string; persistProject?: boolean }) => Promise<void>;
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

    if (taskState === 'SUCCESS') {
      patchData.completed_at = completedAt;
      patchData.duration_seconds = durationSeconds;
    }

    const shouldPatch =
      project.task_state !== taskState ||
      (project.status_text || '') !== statusText ||
      (project.error_text || '') !== nextErrorText ||
      (taskState === 'SUCCESS' && (!project.completed_at || project.duration_seconds === null));

    const next = shouldPatch ? await patch(patchData) : project;

    if (runtimeTask) {
      const taskPatch: Partial<ProjectTask> = {
        task_state: taskState,
        status_text: statusText,
        error_text: nextErrorText,
      };
      if (taskState === 'SUCCESS') {
        taskPatch.completed_at = completedAt;
        taskPatch.duration_seconds = durationSeconds;
      }
      const shouldPatchTask =
        runtimeTask.task_state !== taskState ||
        (runtimeTask.status_text || '') !== statusText ||
        (runtimeTask.error_text || '') !== nextErrorText ||
        (taskState === 'SUCCESS' && (!runtimeTask.completed_at || runtimeTask.duration_seconds === null));
      if (shouldPatchTask) {
        await patchTask(runtimeTask.id, taskPatch);
      }
    }

    const statusLooksLikeMmp = String(statusText || '').toUpperCase().includes('MMP');
    if (taskState === 'SUCCESS' && next?.task_id && !statusLooksLikeMmp && !hasLeadOptMmpOnlySnapshot(runtimeTask)) {
      await pullResultForViewer(next.task_id, {
        taskRowId: runtimeTask?.id,
        persistProject: true,
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
