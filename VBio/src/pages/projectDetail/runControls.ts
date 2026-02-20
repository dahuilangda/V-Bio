import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type { Project } from '../../types/models';

export function showRunQueuedNotice(params: {
  message: string;
  runSuccessNoticeTimerRef: MutableRefObject<number | null>;
  setRunSuccessNotice: Dispatch<SetStateAction<string | null>>;
}): void {
  const { message, runSuccessNoticeTimerRef, setRunSuccessNotice } = params;
  if (runSuccessNoticeTimerRef.current !== null) {
    window.clearTimeout(runSuccessNoticeTimerRef.current);
    runSuccessNoticeTimerRef.current = null;
  }
  setRunSuccessNotice(message);
  runSuccessNoticeTimerRef.current = window.setTimeout(() => {
    runSuccessNoticeTimerRef.current = null;
    setRunSuccessNotice(null);
  }, 4200);
}

export function handleRunAction(params: { runDisabled: boolean; submitTask: () => Promise<void> }): void {
  const { runDisabled, submitTask } = params;
  if (runDisabled) return;
  void submitTask();
}

export function handleRunCurrentDraft(params: {
  setRunMenuOpen: Dispatch<SetStateAction<boolean>>;
  submitTask: () => Promise<void>;
}): void {
  const { setRunMenuOpen, submitTask } = params;
  setRunMenuOpen(false);
  void submitTask();
}

export function handleRestoreSavedDraft(params: {
  setRunMenuOpen: Dispatch<SetStateAction<boolean>>;
  loadProject: () => Promise<void>;
}): void {
  const { setRunMenuOpen, loadProject } = params;
  setRunMenuOpen(false);
  void loadProject();
}

export function handleResetFromHeader(params: {
  saving: boolean;
  submitting: boolean;
  loading: boolean;
  hasUnsavedChanges: boolean;
  onRestore: () => void;
}): void {
  const { saving, submitting, loading, hasUnsavedChanges, onRestore } = params;
  if (saving || submitting || loading || !hasUnsavedChanges) return;
  if (!window.confirm('Discard unsaved changes and reset to the last saved version?')) {
    return;
  }
  onRestore();
}

export async function submitTaskByWorkflow(params: {
  project: Project | null;
  draft: unknown;
  submitInFlightRef: MutableRefObject<boolean>;
  workflowKey: string;
  getWorkflowDefinition: (taskType: string) => { key: string; title: string };
  setError: Dispatch<SetStateAction<string | null>>;
  submitAffinityTask: () => Promise<void>;
  submitPredictionTask: () => Promise<void>;
}): Promise<void> {
  const {
    project,
    draft,
    submitInFlightRef,
    workflowKey,
    getWorkflowDefinition,
    setError,
    submitAffinityTask,
    submitPredictionTask,
  } = params;

  if (!project || !draft) return;
  if (submitInFlightRef.current) return;

  if (workflowKey === 'affinity') {
    await submitAffinityTask();
    return;
  }
  if (workflowKey === 'lead_optimization') {
    setError('Lead Optimization run is only available from the Lead Optimization workspace actions.');
    return;
  }

  const workflow = getWorkflowDefinition(project.task_type);
  if (workflow.key !== 'prediction') {
    setError(`${workflow.title} runner is not wired yet in React UI.`);
    return;
  }

  await submitPredictionTask();
}
