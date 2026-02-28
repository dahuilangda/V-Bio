import { useCallback, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { NavigateFunction } from 'react-router-dom';
import type { Project, ProjectTask } from '../../types/models';
import { getTaskStatus } from '../../api/backendApi';
import { deleteProjectTask, getProjectTaskById, updateProject, updateProjectTask } from '../../api/supabaseLite';
import { getWorkflowDefinition } from '../../utils/workflows';
import {
  isProjectTaskRow,
  readLeadOptTaskSummary,
  sanitizeTaskRows,
  waitForRuntimeTaskToStop,
} from './taskDataUtils';
import { inferTaskStateFromStatusPayload } from './taskRuntimeUiUtils';

interface UseProjectTaskRowActionsOptions {
  project: Project | null;
  navigate: NavigateFunction;
  setError: Dispatch<SetStateAction<string | null>>;
  setTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  terminateBackendTask: (taskId: string) => Promise<{ terminated?: boolean }>;
}

interface UseProjectTaskRowActionsResult {
  openingTaskId: string | null;
  deletingTaskId: string | null;
  terminatingTaskId: string | null;
  editingTaskNameId: string | null;
  editingTaskNameValue: string;
  savingTaskNameId: string | null;
  setEditingTaskNameValue: Dispatch<SetStateAction<string>>;
  openTask: (task: ProjectTask) => Promise<void>;
  beginTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  cancelTaskNameEdit: () => void;
  saveTaskNameEdit: (task: ProjectTask, displayName: string) => Promise<void>;
  terminateTask: (task: ProjectTask) => Promise<void>;
  removeTask: (task: ProjectTask) => Promise<void>;
}

function hasObjectContent(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}

export function useProjectTaskRowActions({
  project,
  navigate,
  setError,
  setTasks,
  terminateBackendTask,
}: UseProjectTaskRowActionsOptions): UseProjectTaskRowActionsResult {
  const [openingTaskId, setOpeningTaskId] = useState<string | null>(null);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [terminatingTaskId, setTerminatingTaskId] = useState<string | null>(null);
  const [editingTaskNameId, setEditingTaskNameId] = useState<string | null>(null);
  const [editingTaskNameValue, setEditingTaskNameValue] = useState('');
  const [savingTaskNameId, setSavingTaskNameId] = useState<string | null>(null);

  const resolveEffectiveRuntimeState = useCallback(async (task: ProjectTask): Promise<ProjectTask['task_state']> => {
    const runtimeTaskId = String(task.task_id || '').trim();
    const inferredFromRow = inferTaskStateFromStatusPayload(
      { state: String(task.task_state || ''), info: { status: String(task.status_text || '') } },
      task.task_state
    );
    if (!runtimeTaskId) return inferredFromRow;
    try {
      const backendStatus = await getTaskStatus(runtimeTaskId);
      const inferredFromBackend = inferTaskStateFromStatusPayload(
        backendStatus as { info?: Record<string, unknown>; state: string },
        inferredFromRow
      );
      return inferredFromBackend || inferredFromRow;
    } catch {
      return inferredFromRow;
    }
  }, []);

  const openTask = useCallback(async (task: ProjectTask) => {
    if (!project) return;
    setOpeningTaskId(task.id);
    setError(null);
    try {
      const runtimeTaskId = String(task.task_id || '').trim();
      const workflowKey = getWorkflowDefinition(project.task_type).key;
      if (!runtimeTaskId) {
        const query = new URLSearchParams({
          tab: 'components',
          task_row_id: task.id
        }).toString();
        navigate(`/projects/${project.id}?${query}`);
        return;
      }
      let taskForOpen = task;
      const shouldHydrateTaskRowForOpen = workflowKey !== 'lead_optimization' && !hasObjectContent(task.affinity);
      if (shouldHydrateTaskRowForOpen) {
        const hydrated = await getProjectTaskById(task.id);
        if (isProjectTaskRow(hydrated)) {
          taskForOpen = { ...task, ...hydrated };
        }
      }
      const nextAffinity =
        hasObjectContent(taskForOpen.affinity)
          ? taskForOpen.affinity
          : project.affinity || {};
      await updateProject(project.id, {
        task_id: taskForOpen.task_id,
        task_state: taskForOpen.task_state,
        status_text: taskForOpen.status_text || '',
        error_text: taskForOpen.error_text || '',
        submitted_at: taskForOpen.submitted_at,
        completed_at: taskForOpen.completed_at,
        duration_seconds: taskForOpen.duration_seconds,
        confidence: taskForOpen.confidence || {},
        affinity: nextAffinity,
        structure_name: taskForOpen.structure_name || '',
        backend: taskForOpen.backend || project.backend
      });
      const hasLeadOptResultPayload =
        workflowKey === 'lead_optimization' && Boolean(readLeadOptTaskSummary(taskForOpen));
      const hasTaskResult = Boolean(
        taskForOpen.task_state === 'SUCCESS' &&
          (String(taskForOpen.structure_name || '').trim() ||
            hasObjectContent(taskForOpen.confidence) ||
            hasObjectContent(taskForOpen.affinity))
      );
      if (hasLeadOptResultPayload || hasTaskResult) {
        navigate(`/projects/${project.id}?tab=results`);
      } else {
        const query = new URLSearchParams({
          tab: 'components',
          task_row_id: task.id
        }).toString();
        navigate(`/projects/${project.id}?${query}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to open selected task.');
    } finally {
      setOpeningTaskId(null);
    }
  }, [project, setError, navigate]);

  const beginTaskNameEdit = useCallback((task: ProjectTask, displayName: string) => {
    if (savingTaskNameId) return;
    setEditingTaskNameId(task.id);
    setEditingTaskNameValue(displayName);
    setError(null);
  }, [savingTaskNameId, setError]);

  const cancelTaskNameEdit = useCallback(() => {
    if (savingTaskNameId) return;
    setEditingTaskNameId(null);
    setEditingTaskNameValue('');
  }, [savingTaskNameId]);

  const saveTaskNameEdit = useCallback(async (task: ProjectTask, displayName: string) => {
    if (editingTaskNameId !== task.id) return;
    if (savingTaskNameId && savingTaskNameId !== task.id) return;
    const nextName = editingTaskNameValue.trim();
    const currentName = String(task.name || '').trim();
    if (nextName === currentName || (!nextName && !currentName)) {
      setEditingTaskNameId(null);
      setEditingTaskNameValue('');
      return;
    }
    setSavingTaskNameId(task.id);
    setError(null);
    try {
      const patch: Partial<ProjectTask> = {
        name: nextName
      };
      const updatedTask = await updateProjectTask(task.id, patch);
      if (!isProjectTaskRow(updatedTask)) {
        throw new Error('Backend returned invalid task row while updating task name.');
      }
      setTasks((prev) =>
        sanitizeTaskRows(prev).map((row) => (row.id === task.id ? { ...row, ...updatedTask } : row))
      );
      setEditingTaskNameId(null);
      setEditingTaskNameValue('');
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to update task name "${displayName}".`);
    } finally {
      setSavingTaskNameId(null);
    }
  }, [editingTaskNameId, savingTaskNameId, editingTaskNameValue, setError, setTasks]);

  const terminateTask = useCallback(async (task: ProjectTask) => {
    const runtimeTaskId = String(task.task_id || '').trim();
    const runtimeState = await resolveEffectiveRuntimeState(task);
    const isActiveRuntime = runtimeState === 'QUEUED' || runtimeState === 'RUNNING';
    if (!isActiveRuntime) return;

    if (!runtimeTaskId) {
      setError('Task is active but task_id is missing; cannot cancel backend task.');
      return;
    }

    const actionLabel = runtimeState === 'RUNNING' ? 'stop' : 'cancel';
    if (!window.confirm(`Confirm ${actionLabel} for task "${runtimeTaskId}"?`)) {
      return;
    }

    setTerminatingTaskId(task.id);
    setError(null);
    try {
      const terminateResult = await terminateBackendTask(runtimeTaskId);
      if (terminateResult.terminated !== true) {
        throw new Error(
          runtimeState === 'RUNNING'
            ? `Backend did not confirm stop for task "${runtimeTaskId}".`
            : `Backend did not confirm cancellation for task "${runtimeTaskId}".`
        );
      }

      const terminalState = await waitForRuntimeTaskToStop(runtimeTaskId, runtimeState === 'RUNNING' ? 18000 : 12000);
      if (terminalState === null || terminalState === 'QUEUED' || terminalState === 'RUNNING') {
        throw new Error(
          runtimeState === 'RUNNING'
            ? `Failed to stop backend task "${runtimeTaskId}" within timeout.`
            : `Failed to cancel backend task "${runtimeTaskId}" within timeout.`
        );
      }

      const nowIso = new Date().toISOString();
      const submittedAtTs = Date.parse(String(task.submitted_at || ''));
      const nextDurationSeconds = Number.isFinite(submittedAtTs)
        ? Math.max(0, Math.floor((Date.now() - submittedAtTs) / 1000))
        : (task.duration_seconds ?? null);
      const nextStatusText =
        terminalState === 'REVOKED'
          ? 'Task cancelled by user.'
          : terminalState === 'FAILURE'
            ? 'Task terminated.'
            : String(task.status_text || '').trim() || 'Task finished.';
      const patch: Partial<ProjectTask> = {
        task_state: terminalState,
        status_text: nextStatusText,
        completed_at: nowIso,
        duration_seconds: nextDurationSeconds,
        error_text: terminalState === 'REVOKED' ? '' : String(task.error_text || '')
      };
      const updatedTask = await updateProjectTask(task.id, patch);
      const nextRow = isProjectTaskRow(updatedTask) ? { ...task, ...updatedTask, ...patch } : { ...task, ...patch };
      setTasks((prev) => sanitizeTaskRows(prev).map((row) => (row.id === task.id ? nextRow : row)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to cancel running task.');
    } finally {
      setTerminatingTaskId(null);
    }
  }, [resolveEffectiveRuntimeState, setError, setTasks, terminateBackendTask, updateProjectTask]);

  const removeTask = useCallback(async (task: ProjectTask) => {
    const runtimeTaskId = String(task.task_id || '').trim();
    const runtimeState = await resolveEffectiveRuntimeState(task);
    const isRuntimeActiveState = runtimeState === 'QUEUED' || runtimeState === 'RUNNING';
    const isActiveRuntime = Boolean(runtimeTaskId) && isRuntimeActiveState;
    const confirmText = isActiveRuntime
      ? runtimeState === 'QUEUED'
        ? `Task "${runtimeTaskId}" is queued. Delete will first cancel it on backend. Continue?`
        : `Task "${runtimeTaskId}" is running. Delete will first stop it on backend. Continue?`
      : `Delete task "${task.task_id || task.id}" from this project?`;
    if (!window.confirm(confirmText)) return;
    setDeletingTaskId(task.id);
    setError(null);
    try {
      if (isRuntimeActiveState && !runtimeTaskId) {
        throw new Error(
          runtimeState === 'RUNNING'
            ? 'Task appears to be running but task_id is missing; deletion is blocked to avoid orphan runtime.'
            : 'Task appears to be queued but task_id is missing; deletion is blocked to avoid orphan runtime.'
        );
      }
      if (runtimeState === 'QUEUED' || runtimeState === 'RUNNING') {
        if (!runtimeTaskId) {
          throw new Error(
            runtimeState === 'RUNNING'
              ? 'Task is running but task_id is missing; cannot stop backend task, deletion is blocked.'
              : 'Task is queued but task_id is missing; cannot cancel backend task, deletion is blocked.'
          );
        }
        const terminateResult = await terminateBackendTask(runtimeTaskId);
        if (terminateResult.terminated !== true) {
          throw new Error(
            runtimeState === 'RUNNING'
              ? `Backend did not confirm stop for task "${runtimeTaskId}", deletion is blocked.`
              : `Backend did not confirm cancellation for task "${runtimeTaskId}", deletion is blocked.`
          );
        }
        const terminalState = await waitForRuntimeTaskToStop(runtimeTaskId, runtimeState === 'RUNNING' ? 14000 : 9000);
        if (terminalState === null || terminalState === 'QUEUED' || terminalState === 'RUNNING') {
          throw new Error(
            runtimeState === 'RUNNING'
              ? `Failed to stop backend task "${runtimeTaskId}". Task is still active, so deletion is blocked.`
              : `Failed to cancel backend task "${runtimeTaskId}". Task is still active, so deletion is blocked.`
          );
        }
      }
      await deleteProjectTask(task.id);
      setTasks((prev) => sanitizeTaskRows(prev).filter((row) => row.id !== task.id));
      if (editingTaskNameId === task.id) {
        setEditingTaskNameId(null);
        setEditingTaskNameValue('');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete task.');
    } finally {
      setDeletingTaskId(null);
    }
  }, [editingTaskNameId, resolveEffectiveRuntimeState, setError, setTasks, terminateBackendTask]);

  return {
    openingTaskId,
    deletingTaskId,
    terminatingTaskId,
    editingTaskNameId,
    editingTaskNameValue,
    savingTaskNameId,
    setEditingTaskNameValue,
    openTask,
    beginTaskNameEdit,
    cancelTaskNameEdit,
    saveTaskNameEdit,
    terminateTask,
    removeTask,
  };
}
