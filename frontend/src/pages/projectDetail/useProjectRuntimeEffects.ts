import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import { createAdaptivePollScheduler } from '../../utils/adaptivePollScheduler';

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

interface UseProjectRuntimeEffectsInput {
  projectTaskId: string | null;
  projectTaskState: string | null;
  refreshStatus: (options?: { silent?: boolean; taskId?: string }) => Promise<boolean>;
  statusContextTaskRow: RuntimeTaskLike | null;
  runtimeResultTask: RuntimeTaskLike | null;
  activeResultTask: RuntimeTaskLike | null;
  structureTaskId: string | null;
  pullResultForViewer: (
    taskId: string,
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode; preferredStructureName?: string }
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
  refreshStatus,
  statusContextTaskRow,
  runtimeResultTask,
  structureTaskId,
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
  // Latest refreshStatus without re-arming the poll loop (see the polling effect below).
  const refreshStatusRef = useRef(refreshStatus);
  refreshStatusRef.current = refreshStatus;
  useEffect(() => {
    if (isLeadOptimizationWorkflow) return;
    const pollingTaskId = String(statusContextTaskRow?.task_id || runtimeResultTask?.task_id || projectTaskId || '').trim();
    if (!pollingTaskId) return;
    const normalizedState = String(
      statusContextTaskRow?.task_state || runtimeResultTask?.task_state || projectTaskState || ''
    ).toUpperCase();
    if (normalizedState !== 'QUEUED' && normalizedState !== 'RUNNING') return;

    // The effect must restart only when the polling TARGET changes (task id or state).
    // refreshStatus is identity-unstable (it closes over project/projectTasks, which the
    // runtime overlays refresh on every poll) — depending on it directly re-armed this
    // effect every poll and perpetually reset the 5s timer. The ref above always holds
    // the latest refreshStatus.

    // Adaptive cadence (see adaptivePollScheduler): fast while progress keeps changing,
    // slower after three flat ticks, doubled while hidden, catch-up tick on tab return —
    // the tick's `changed` return value drives the fast/slow switch.
    const scheduler = createAdaptivePollScheduler({
      resolveIntervals: () => {
        const activeMs = normalizedState === 'RUNNING' ? 5000 : 9000;
        return { activeMs, idleMs: Math.max(activeMs * 2, 12000) };
      },
      idleAfterUnchangedTicks: 3,
      hiddenIntervalMultiplier: 2,
      maxIntervalMs: 30000,
      tick: () => refreshStatusRef.current({ silent: true, taskId: pollingTaskId })
    });
    scheduler.start();

    return () => {
      scheduler.stop();
    };
  }, [
    isLeadOptimizationWorkflow,
    projectTaskId,
    projectTaskState,
    statusContextTaskRow?.task_id,
    statusContextTaskRow?.task_state,
    runtimeResultTask?.task_id,
    runtimeResultTask?.task_state
  ]);

  useEffect(() => {
    if (isLeadOptimizationWorkflow) return;
    if (isPeptideDesignWorkflow) return;
    // Results-tab only: this pull downloads and unzips the FULL result archive (often MBs on
    // large complexes). It used to fire on any tab as soon as a SUCCESS task was focused, so
    // merely opening a project (or landing on ?tab=basics) paid the download + parse. The
    // effect re-runs when workspaceTab changes, so the fetch lands exactly when the user
    // opens the results tab — and 'results' IS the default tab, so landing there is unchanged.
    if (workspaceTab !== 'results') return;
    const contextTask = statusContextTaskRow || runtimeResultTask;
    const contextTaskId = String(contextTask?.task_id || '').trim();
    if (!contextTaskId) return;
    if (contextTask?.task_state !== 'SUCCESS') return;
    if (hasLeadOptMmpOnlySnapshot(contextTask)) return;

    const hasResultLoaded = structureTaskId === contextTaskId;
    if (hasResultLoaded) {
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
    projectTaskId,
    structureTaskId,
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
