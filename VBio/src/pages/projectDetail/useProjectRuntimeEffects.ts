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

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
}

function readPeptideCandidateRows(confidence: unknown): Array<Record<string, unknown>> {
  const payload = asRecord(confidence);
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

function rowHasStructureText(row: Record<string, unknown>): boolean {
  const structureText = String(
    row.structure_text ?? row.structureText ?? row.cif_text ?? row.pdb_text ?? row.content ?? ''
  ).trim();
  return Boolean(structureText);
}

function needsPeptideCandidateHydration(task: RuntimeTaskLike | null): boolean {
  if (!task) return false;
  const rows = readPeptideCandidateRows(task.confidence);
  if (rows.length === 0) return false;
  return rows.some((row) => !rowHasStructureText(row));
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
    const contextTask = statusContextTaskRow || runtimeResultTask;
    const contextTaskId = String(contextTask?.task_id || '').trim();
    if (!contextTaskId) return;
    if (contextTask?.task_state !== 'SUCCESS') return;
    if (hasLeadOptMmpOnlySnapshot(contextTask)) return;
    const requirePeptideHydration = isPeptideDesignWorkflow && needsPeptideCandidateHydration(contextTask);
    if (!requirePeptideHydration && structureTaskId === contextTaskId && structureText.trim()) return;

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
