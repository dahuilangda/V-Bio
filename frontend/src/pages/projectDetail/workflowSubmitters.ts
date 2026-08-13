import type { MutableRefObject } from 'react';
import { submitAffinityTaskFromDraft } from './affinitySubmission';
import { submitPredictionTaskFromDraft } from './predictionSubmission';

export interface WorkflowSubmitterContext {
  [key: string]: any;
  /** A ref to the latest draft so the submit closures always read the current value, not a stale
   * closure capture. This fixes the race where applyPatch (setDraft, async) is immediately followed
   * by submit — without the ref, the submit closures captured the pre-patch draft. */
  draftRef: MutableRefObject<unknown>;
}

export function createWorkflowSubmitters(c: WorkflowSubmitterContext) {
  const submitAffinityTask = async () => {
    const draft = c.draftRef.current as Record<string, unknown> | null;
    if (!c.project || !draft) return;
    await submitAffinityTaskFromDraft({
      project: c.project,
      draft: draft as any,
      workspaceTab: c.workspaceTab,
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
      resolveEditableDraftTaskRowId: c.resolveEditableDraftTaskRowId,
      rememberAffinityUploadsForTaskRow: c.rememberAffinityUploadsForTaskRow,
      patch: c.patch,
      patchTask: c.patchTask,
      updateProjectTask: c.updateProjectTask,
      sortProjectTasks: c.sortProjectTasks,
      saveProjectInputConfig: c.saveProjectInputConfig
    });
  };

  const submitPredictionTask = async () => {
    const draft = c.draftRef.current as Record<string, unknown> | null;
    if (!c.project || !draft) return;
    await submitPredictionTaskFromDraft({
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
