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

export function handleRunAction(params: {
  runDisabled: boolean;
  submitTask: (notifyEmailOverride?: string) => Promise<void>;
  notifyEmailOverride?: string;
}): void {
  const { runDisabled, submitTask, notifyEmailOverride } = params;
  if (runDisabled) return;
  // submit re-throws for the Copilot chain and already surfaced the error via
  // setError; this .catch keeps the Run button path from an unhandled rejection
  void submitTask(notifyEmailOverride).catch(() => {});
}

export function handleRunCurrentDraft(params: {
  setRunMenuOpen: Dispatch<SetStateAction<boolean>>;
  submitTask: () => Promise<void>;
}): void {
  const { setRunMenuOpen, submitTask } = params;
  setRunMenuOpen(false);
  void submitTask().catch(() => {});
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
  /** Dialog-confirmed email riding WITH this submit (beats the draft-write race). */
  notifyEmailOverride?: string;
  project: Project | null;
  draft: unknown;
  submitInFlightRef: MutableRefObject<boolean>;
  workflowKey: string;
  getWorkflowDefinition: (taskType: string) => { key: string; title: string };
  setError: Dispatch<SetStateAction<string | null>>;
  submitAffinityTask: (notifyEmailOverride?: string) => Promise<void>;
  submitPredictionTask: (notifyEmailOverride?: string) => Promise<void>;
}): Promise<void> {
  const {
    notifyEmailOverride,
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
    await submitAffinityTask(notifyEmailOverride);
    return;
  }
  if (workflowKey === 'lead_optimization') {
    setError('Lead Optimization run is only available from the Lead Optimization workspace actions.');
    return;
  }

  const workflow = getWorkflowDefinition(project.task_type);
  if (workflow.key !== 'prediction' && workflow.key !== 'peptide_design' && workflow.key !== 'virtual_screening') {
    setError(`${workflow.title} runner is not wired yet in React UI.`);
    return;
  }

  await submitPredictionTask(notifyEmailOverride);
}
