import { useMemo } from 'react';
import type { Project, ProjectTask, TaskState } from '../../types/models';
import { findProgressPercent } from './projectMetrics';

interface UseProjectTaskStatusContextInput {
  project: Project | null;
  projectTasks: ProjectTask[];
  locationSearch: string;
  statusInfo: Record<string, unknown> | null;
  nowTs: number;
}

interface ProjectTaskStatusContext {
  requestedStatusTaskRow: ProjectTask | null;
  activeStatusTaskRow: ProjectTask | null;
  statusContextTaskRow: ProjectTask | null;
  displayTaskState: TaskState;
  displaySubmittedAt: string | null;
  displayCompletedAt: string | null;
  displayDurationSeconds: number | null;
  progressPercent: number;
  waitingSeconds: number | null;
  isActiveRuntime: boolean;
  totalRuntimeSeconds: number | null;
}

export function useProjectTaskStatusContext({
  project,
  projectTasks,
  locationSearch,
  statusInfo,
  nowTs
}: UseProjectTaskStatusContextInput): ProjectTaskStatusContext {
  const requestedStatusTaskRow = useMemo(() => {
    const requestedTaskRowId = new URLSearchParams(locationSearch).get('task_row_id');
    if (!requestedTaskRowId || !requestedTaskRowId.trim()) return null;
    return projectTasks.find((item) => String(item.id || '').trim() === requestedTaskRowId.trim()) || null;
  }, [locationSearch, projectTasks]);

  const activeStatusTaskRow = useMemo(() => {
    const activeTaskId = (project?.task_id || '').trim();
    if (!activeTaskId) return null;
    return projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId) || null;
  }, [project?.task_id, projectTasks]);

  const statusContextTaskRow = useMemo(() => {
    if (!requestedStatusTaskRow) return activeStatusTaskRow;
    if (!activeStatusTaskRow) return requestedStatusTaskRow;
    const requestedTaskId = String(requestedStatusTaskRow.task_id || '').trim();
    const activeTaskId = String(activeStatusTaskRow.task_id || '').trim();
    if (!requestedTaskId || requestedTaskId !== activeTaskId) {
      const activeState = String(activeStatusTaskRow.task_state || '').toUpperCase();
      const requestedState = String(requestedStatusTaskRow.task_state || '').toUpperCase();
      if ((activeState === 'QUEUED' || activeState === 'RUNNING') && requestedState !== 'QUEUED' && requestedState !== 'RUNNING') {
        return activeStatusTaskRow;
      }
    }
    return requestedStatusTaskRow;
  }, [requestedStatusTaskRow, activeStatusTaskRow]);
  const displayTaskState: TaskState = statusContextTaskRow?.task_state || project?.task_state || 'DRAFT';
  const displaySubmittedAt = statusContextTaskRow?.submitted_at ?? project?.submitted_at ?? null;
  const displayCompletedAt = statusContextTaskRow?.completed_at ?? project?.completed_at ?? null;
  const displayDurationSeconds = statusContextTaskRow?.duration_seconds ?? project?.duration_seconds ?? null;

  const progressPercent = useMemo(() => {
    if (['SUCCESS', 'FAILURE', 'REVOKED'].includes(displayTaskState)) return 100;
    if (!['QUEUED', 'RUNNING'].includes(displayTaskState)) return 0;
    const statusPayload = statusInfo && typeof statusInfo === 'object' ? (statusInfo as Record<string, unknown>) : null;
    const scopedStatusTaskId = String(statusPayload?.__task_id ?? statusPayload?.task_id ?? statusPayload?.taskId ?? '').trim();
    const contextTaskId = String(statusContextTaskRow?.task_id || project?.task_id || '').trim();
    const shouldUseScopedStatus = !scopedStatusTaskId || !contextTaskId || scopedStatusTaskId === contextTaskId;
    const explicit = shouldUseScopedStatus ? findProgressPercent(statusInfo) : null;
    if (explicit !== null) return Math.max(0, Math.min(100, explicit));
    if (displayTaskState === 'RUNNING') return 62;
    if (displayTaskState === 'QUEUED') return 18;
    return 0;
  }, [displayTaskState, statusInfo, statusContextTaskRow?.task_id, project?.task_id]);

  const waitingSeconds = useMemo(() => {
    if (!displaySubmittedAt) return null;
    if (!['QUEUED', 'RUNNING'].includes(displayTaskState)) return null;
    const duration = Math.floor((nowTs - new Date(displaySubmittedAt).getTime()) / 1000);
    return Math.max(0, duration);
  }, [displayTaskState, displaySubmittedAt, nowTs]);

  const isActiveRuntime = useMemo(() => {
    return displayTaskState === 'QUEUED' || displayTaskState === 'RUNNING';
  }, [displayTaskState]);

  const totalRuntimeSeconds = useMemo(() => {
    if (displayDurationSeconds !== null && displayDurationSeconds !== undefined) {
      return displayDurationSeconds;
    }
    if (displaySubmittedAt && displayCompletedAt) {
      const duration = (new Date(displayCompletedAt).getTime() - new Date(displaySubmittedAt).getTime()) / 1000;
      return Number.isFinite(duration) && duration >= 0 ? duration : null;
    }
    return null;
  }, [displayDurationSeconds, displaySubmittedAt, displayCompletedAt]);

  return {
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    displaySubmittedAt,
    displayCompletedAt,
    displayDurationSeconds,
    progressPercent,
    waitingSeconds,
    isActiveRuntime,
    totalRuntimeSeconds
  };
}
