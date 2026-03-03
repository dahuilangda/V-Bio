import { submitAffinityTaskFromDraft } from './affinitySubmission';
import { submitPredictionTaskFromDraft } from './predictionSubmission';

export interface WorkflowSubmitterContext {
  [key: string]: any;
}

export function createWorkflowSubmitters(c: WorkflowSubmitterContext) {
  const submitAffinityTask = async () => {
    if (!c.project || !c.draft) return;
    await submitAffinityTaskFromDraft({
      project: c.project,
      draft: c.draft,
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
      setRunMenuOpen: c.setRunMenuOpen,
      setProjectTasks: c.setProjectTasks,
      setProject: c.setProject,
      setStatusInfo: c.setStatusInfo,
      showRunQueuedNotice: c.showRunQueuedNotice,
      normalizeConfigForBackend: c.normalizeConfigForBackend,
      computeUseMsaFlag: c.computeUseMsaFlag,
      createDraftFingerprint: c.createDraftFingerprint,
      createComputationFingerprint: c.createComputationFingerprint,
      createProteinTemplatesFingerprint: c.createProteinTemplatesFingerprint,
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
    if (!c.project || !c.draft) return;
    await submitPredictionTaskFromDraft({
      project: c.project,
      draft: c.draft,
      isPeptideDesignWorkflow: Boolean(c.isPeptideDesignWorkflow),
      workspaceTab: c.workspaceTab,
      proteinTemplates: c.proteinTemplates,
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
