import { useEffect } from 'react';
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

interface UseProjectRuntimeEffectsInput {
  projectTaskId: string | null;
  projectTaskState: string | null;
  projectTasksDependency: unknown;
  refreshStatus: (options?: { silent?: boolean }) => Promise<void>;
  statusContextTaskRow: RuntimeTaskLike | null;
  runtimeResultTask: RuntimeTaskLike | null;
  structureTaskId: string | null;
  structureText: string;
  pullResultForViewer: (
    taskId: string,
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode }
  ) => Promise<void>;
  isPeptideDesignWorkflow: boolean;
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
  structureTaskId,
  structureText,
  pullResultForViewer,
  isPeptideDesignWorkflow,
  workspaceTab,
  activeConstraintId,
  selectedContactConstraintIdsLength,
  setActiveConstraintId,
  setSelectedContactConstraintIds,
  constraintSelectionAnchorRef
}: UseProjectRuntimeEffectsInput) {
  useEffect(() => {
    if (!projectTaskId) return;
    if (!['QUEUED', 'RUNNING'].includes(String(projectTaskState || '').toUpperCase())) return;

    const timer = setInterval(() => {
      void refreshStatus({ silent: true });
    }, 4000);

    return () => clearInterval(timer);
  }, [projectTaskId, projectTaskState, projectTasksDependency, refreshStatus]);

  useEffect(() => {
    if (isPeptideDesignWorkflow) return;
    const contextTask = statusContextTaskRow || runtimeResultTask;
    const contextTaskId = String(contextTask?.task_id || '').trim();
    if (!contextTaskId) return;
    if (contextTask?.task_state !== 'SUCCESS') return;
    if (hasLeadOptMmpOnlySnapshot(contextTask)) return;
    if (structureTaskId === contextTaskId && structureText.trim()) return;
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
    structureText,
    pullResultForViewer,
    isPeptideDesignWorkflow
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
