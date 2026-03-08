import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import type { DownloadResultMode } from '../../api/backendTaskApi';

interface RuntimeTaskLike {
  id: string;
  task_id: string | null;
  task_state: string;
  status_text?: string;
  confidence?: Record<string, unknown>;
  structure_name?: string | null;
}

function hasLeadOptMmpOnlySnapshot(task: RuntimeTaskLike | null): boolean {
  if (!task) return false;
  if (task.confidence && typeof task.confidence === 'object') {
    const leadOptMmp = (task.confidence as Record<string, unknown>).lead_opt_mmp;
    if (leadOptMmp && typeof leadOptMmp === 'object') return true;
  }
  return String(task.status_text || '').toUpperCase().includes('MMP');
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function hasPeptideCandidateRows(confidence: Record<string, unknown> | undefined): boolean {
  if (!confidence) return false;
  const peptide = asRecord(confidence.peptide_design);
  const progress = asRecord(confidence.progress);
  const peptideProgress = asRecord(peptide.progress);
  const sources = [confidence, peptide, progress, peptideProgress];
  return sources.some(
    (source) =>
      (Array.isArray(source.best_sequences) && source.best_sequences.length > 0) ||
      (Array.isArray(source.current_best_sequences) && source.current_best_sequences.length > 0) ||
      (Array.isArray(source.candidates) && source.candidates.length > 0)
  );
}

interface UseProjectRuntimeEffectsInput {
  projectTaskId: string | null;
  projectTaskState: string | null;
  projectTasksDependency: unknown;
  refreshStatus: (options?: { silent?: boolean; taskId?: string }) => Promise<void>;
  statusContextTaskRow: RuntimeTaskLike | null;
  runtimeResultTask: RuntimeTaskLike | null;
  activeResultTask: RuntimeTaskLike | null;
  structureTaskId: string | null;
  structureText: string;
  pullResultForViewer: (
    taskId: string,
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode }
  ) => Promise<void>;
  isPeptideDesignWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  activeConstraintId: string | null;
  selectedContactConstraintIdsLength: number;
  setActiveConstraintId: (id: string | null) => void;
  setSelectedContactConstraintIds: (ids: string[]) => void;
  constraintSelectionAnchorRef: MutableRefObject<string | null>;
}

export function useProjectRuntimeEffects({
  projectTaskId,
  projectTaskState,
  projectTasksDependency,
  refreshStatus,
  statusContextTaskRow,
  runtimeResultTask,
  activeResultTask,
  structureTaskId,
  structureText,
  pullResultForViewer,
  isPeptideDesignWorkflow,
  isLeadOptimizationWorkflow,
  workspaceTab,
  activeConstraintId,
  selectedContactConstraintIdsLength,
  setActiveConstraintId,
  setSelectedContactConstraintIds,
  constraintSelectionAnchorRef
}: UseProjectRuntimeEffectsInput) {
  const peptideAutoPullAttemptRef = useRef('');

  useEffect(() => {
    if (isLeadOptimizationWorkflow) return;
    const pollingTaskId = String(statusContextTaskRow?.task_id || runtimeResultTask?.task_id || projectTaskId || '').trim();
    if (!pollingTaskId) return;
    const normalizedState = String(
      statusContextTaskRow?.task_state || runtimeResultTask?.task_state || projectTaskState || ''
    ).toUpperCase();
    if (normalizedState !== 'QUEUED' && normalizedState !== 'RUNNING') return;

    let cancelled = false;
    let inFlight = false;
    let timer: number | null = null;
    const computeDelayMs = () => {
      const baseDelay = normalizedState === 'RUNNING' ? 5000 : 9000;
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
        return baseDelay * 2;
      }
      return baseDelay;
    };
    const scheduleNext = () => {
      if (cancelled) return;
      timer = window.setTimeout(() => {
        void tick();
      }, computeDelayMs());
    };
    const tick = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        await refreshStatus({ silent: true, taskId: pollingTaskId });
      } finally {
        inFlight = false;
        scheduleNext();
      }
    };

    scheduleNext();

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    isLeadOptimizationWorkflow,
    projectTaskId,
    projectTaskState,
    projectTasksDependency,
    refreshStatus,
    statusContextTaskRow?.task_id,
    statusContextTaskRow?.task_state,
    runtimeResultTask?.task_id,
    runtimeResultTask?.task_state
  ]);

  useEffect(() => {
    if (isLeadOptimizationWorkflow) return;
    const contextTask = isPeptideDesignWorkflow
      ? activeResultTask || statusContextTaskRow || runtimeResultTask
      : statusContextTaskRow || runtimeResultTask;
    const contextTaskId = String(contextTask?.task_id || '').trim();
    if (!contextTaskId) return;
    if (contextTask?.task_state !== 'SUCCESS') return;
    if (hasLeadOptMmpOnlySnapshot(contextTask)) return;

    const hasStructureLoaded = structureTaskId === contextTaskId && structureText.trim().length > 0;
    if (isPeptideDesignWorkflow) {
      if (workspaceTab !== 'results') return;
      if (hasPeptideCandidateRows(contextTask?.confidence)) {
        peptideAutoPullAttemptRef.current = '';
        return;
      }
      if (peptideAutoPullAttemptRef.current === contextTaskId) return;
      peptideAutoPullAttemptRef.current = contextTaskId;
    } else if (hasStructureLoaded) {
      return;
    }

    const activeRuntimeTaskId = String(projectTaskId || '').trim();
    const resultMode: DownloadResultMode = 'view';
    void pullResultForViewer(contextTaskId, {
      taskRowId: contextTask?.id || undefined,
      persistProject: activeRuntimeTaskId === contextTaskId,
      resultMode
    });
  }, [
    statusContextTaskRow,
    runtimeResultTask,
    activeResultTask,
    projectTaskId,
    structureTaskId,
    structureText,
    pullResultForViewer,
    isPeptideDesignWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab
  ]);

  useEffect(() => {
    if (workspaceTab !== 'constraints') return;
    if (!activeConstraintId && selectedContactConstraintIdsLength === 0) return;

    const onGlobalPointerDown = (event: globalThis.PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        setActiveConstraintId(null);
        setSelectedContactConstraintIds([]);
        constraintSelectionAnchorRef.current = null;
        return;
      }

      const keepSelection =
        Boolean(target.closest('.constraint-item')) ||
        Boolean(target.closest('.component-sidebar-link-constraint')) ||
        Boolean(target.closest('.molstar-host')) ||
        Boolean(target.closest('button, a, input, select, textarea, label, [role="button"], [contenteditable="true"]'));

      if (!keepSelection) {
        setActiveConstraintId(null);
        setSelectedContactConstraintIds([]);
        constraintSelectionAnchorRef.current = null;
      }
    };

    document.addEventListener('pointerdown', onGlobalPointerDown, true);
    return () => {
      document.removeEventListener('pointerdown', onGlobalPointerDown, true);
    };
  }, [
    workspaceTab,
    activeConstraintId,
    selectedContactConstraintIdsLength,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef
  ]);
}
