import type { MutableRefObject } from 'react';
import { submitAffinityTaskFromDraft } from './affinitySubmission';
import { submitPredictionTaskFromDraft } from './predictionSubmission';

export interface WorkflowSubmitterContext {
  [key: string]: any;
  /** Latest draft via ref so submit closures never read a stale capture
   *  after a setDraft immediately followed by submit. */
  draftRef: MutableRefObject<unknown>;
  /** Logged-in user's email (notification default). */
  sessionEmail?: string | null;
}

export function createWorkflowSubmitters(c: WorkflowSubmitterContext) {
  const submitAffinityTask = async (notifyEmailOverride?: string) => {
    const draft = c.draftRef.current as Record<string, unknown> | null;
    if (!c.project || !draft) return;
    await submitAffinityTaskFromDraft({
      notifyEmailFallback: notifyEmailOverride || c.sessionEmail || undefined,
      project: c.project,
      draft: draft as any,
      affinityTargetFile: c.affinityTargetFile,
      affinityLigandFile: c.affinityLigandFile,
      affinityPreviewLoading: c.affinityPreviewLoading,
      affinityPreviewCurrent: c.affinityPreviewCurrent,
      affinityPreview: c.affinityPreview,
      affinityPreviewError: c.affinityPreviewError,
      affinityTargetChainIds: c.affinityTargetChainIds,
      affinityLigandChainId: c.affinityLigandChainId,
      affinityLigandSmiles: c.affinityLigandSmiles,
      affinityHasLigand: c.affinityHasLigand,
      affinitySupportsActivity: c.affinitySupportsActivity,
      affinityConfidenceOnly: c.affinityConfidenceOnly,
      affinityCurrentUploads: c.affinityCurrentUploads,
      proteinTemplates: c.proteinTemplates,
      submitInFlightRef: c.submitInFlightRef,
      runRedirectTimerRef: c.runRedirectTimerRef,
      runSuccessNoticeTimerRef: c.runSuccessNoticeTimerRef,
      setSubmitting: c.setSubmitting,
      setError: c.setError,
      setRunRedirectTaskId: c.setRunRedirectTaskId,
      setRunSuccessNotice: c.setRunSuccessNotice,
      setDraft: c.setDraft,
      setSavedDraftFingerprint: c.setSavedDraftFingerprint,
      setSavedComputationFingerprint: c.setSavedComputationFingerprint,
      setSavedTemplateFingerprint: c.setSavedTemplateFingerprint,
      setSavedAffinityUploadsFingerprint: c.setSavedAffinityUploadsFingerprint,
      setRunMenuOpen: c.setRunMenuOpen,
      syncWorkspaceTaskRow: c.syncWorkspaceTaskRow,
      setProjectTasks: c.setProjectTasks,
      setProject: c.setProject,
      setStatusInfo: c.setStatusInfo,
      showRunQueuedNotice: c.showRunQueuedNotice,
      normalizeConfigForBackend: c.normalizeConfigForBackend,
      computeUseMsaFlag: c.computeUseMsaFlag,
      createDraftFingerprint: c.createDraftFingerprint,
      createComputationFingerprint: c.createComputationFingerprint,
      createProteinTemplatesFingerprint: c.createProteinTemplatesFingerprint,
      createAffinityUploadsFingerprint: c.createAffinityUploadsFingerprint,
      buildAffinityUploadSnapshotComponents: c.buildAffinityUploadSnapshotComponents,
      persistDraftTaskSnapshot: c.persistDraftTaskSnapshot,
      findProjectTaskByTaskId: c.findProjectTaskByTaskId,
      deleteProjectTask: c.deleteProjectTask,
      resolveEditableDraftTaskRowId: c.resolveEditableDraftTaskRowId,
      rememberAffinityUploadsForTaskRow: c.rememberAffinityUploadsForTaskRow,
      patch: c.patch,
      patchTask: c.patchTask,
      updateProjectTask: c.updateProjectTask,
      sortProjectTasks: c.sortProjectTasks,
      saveProjectInputConfig: c.saveProjectInputConfig
    });
  };

  const submitPredictionTask = async (notifyEmailOverride?: string) => {
    const draft = c.draftRef.current as Record<string, unknown> | null;
    if (!c.project || !draft) return;
    await submitPredictionTaskFromDraft({
      notifyEmailFallback: notifyEmailOverride || c.sessionEmail || undefined,
      project: c.project,
      draft: draft as any,
      isPeptideDesignWorkflow: Boolean(c.isPeptideDesignWorkflow),
      isVirtualScreeningWorkflow: Boolean(c.isVirtualScreeningWorkflow),
      workspaceTab: c.workspaceTab,
      proteinTemplates: c.proteinTemplates,
      customResidueLibrary: c.customResidueLibrary,
      submitInFlightRef: c.submitInFlightRef,
      runRedirectTimerRef: c.runRedirectTimerRef,
      runSuccessNoticeTimerRef: c.runSuccessNoticeTimerRef,
      setWorkspaceTab: c.setWorkspaceTab,
      setSubmitting: c.setSubmitting,
      setError: c.setError,
      setRunRedirectTaskId: c.setRunRedirectTaskId,
      setRunSuccessNotice: c.setRunSuccessNotice,
      setDraft: c.setDraft,
      setSavedDraftFingerprint: c.setSavedDraftFingerprint,
      setSavedComputationFingerprint: c.setSavedComputationFingerprint,
      setSavedTemplateFingerprint: c.setSavedTemplateFingerprint,
      setRunMenuOpen: c.setRunMenuOpen,
      syncWorkspaceTaskRow: c.syncWorkspaceTaskRow,
      setProjectTasks: c.setProjectTasks,
      setProject: c.setProject,
      setStatusInfo: c.setStatusInfo,
      showRunQueuedNotice: c.showRunQueuedNotice,
      normalizeConfigForBackend: c.normalizeConfigForBackend,
      listIncompleteComponentOrders: c.listIncompleteComponentOrders,
      validateComponents: c.validateComponents,
      computeUseMsaFlag: c.computeUseMsaFlag,
      createDraftFingerprint: c.createDraftFingerprint,
      createComputationFingerprint: c.createComputationFingerprint,
      createProteinTemplatesFingerprint: c.createProteinTemplatesFingerprint,
      addTemplatesToTaskSnapshotComponents: c.addTemplatesToTaskSnapshotComponents,
      persistDraftTaskSnapshot: c.persistDraftTaskSnapshot,
      findProjectTaskByTaskId: c.findProjectTaskByTaskId,
      deleteProjectTask: c.deleteProjectTask,
      resolveEditableDraftTaskRowId: c.resolveEditableDraftTaskRowId,
      rememberTemplatesForTaskRow: c.rememberTemplatesForTaskRow,
      patch: c.patch,
      patchTask: c.patchTask,
      updateProjectTask: c.updateProjectTask,
      sortProjectTasks: c.sortProjectTasks,
      saveProjectInputConfig: c.saveProjectInputConfig
    });
  };

  return {
    submitAffinityTask,
    submitPredictionTask
  };
}
