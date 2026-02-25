export interface BuildRunUiStateParams {
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  hasIncompleteComponents: boolean;
  componentCompletion: { filledCount: number; total: number };
  submitting: boolean;
  saving: boolean;
  isRunRedirecting: boolean;
  showFloatingRunButton: boolean;
  affinityTargetFilePresent: boolean;
  affinityPreviewLoading: boolean;
  affinityPreviewCurrent: boolean;
  affinityPreviewError: string;
  affinityTargetChainCount: number;
  affinityLigandChainId: string;
  affinityLigandSmiles: string;
  affinityHasLigand: boolean;
  affinitySupportsActivity: boolean;
  affinityConfidenceOnly: boolean;
  affinityConfidenceOnlyLocked: boolean;
  draftBackend: string;
}

export interface RunUiStateResult {
  componentStepLabel: string;
  isRunRedirecting: boolean;
  showQuickRunFab: boolean;
  affinityUseActivity: boolean;
  affinityConfidenceOnlyUiValue: boolean;
  affinityConfidenceOnlyUiLocked: boolean;
  affinityReadyReason: string;
  runBlockedReason: string;
  runDisabled: boolean;
  canOpenRunMenu: boolean;
}

export function buildRunUiState(params: BuildRunUiStateParams): RunUiStateResult {
  const {
    workspaceTab,
    isPredictionWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    submitting,
    saving,
    isRunRedirecting,
    showFloatingRunButton,
    affinityTargetFilePresent,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError,
    affinityTargetChainCount,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
  } = params;

  const componentStepLabel = 'Components';
  const showQuickRunFab = showFloatingRunButton && !isRunRedirecting;

  const affinityBackendSupportsActivity = true;
  const affinityConfidenceOnlyForced = !affinityBackendSupportsActivity;
  const affinityConfidenceOnlyUiValue = affinityConfidenceOnlyForced ? true : affinityConfidenceOnly;
  const affinityConfidenceOnlyUiLocked = affinityConfidenceOnlyLocked || affinityConfidenceOnlyForced;
  const affinityUseActivity =
    affinityBackendSupportsActivity &&
    !affinityConfidenceOnlyUiValue &&
    affinityHasLigand &&
    (affinitySupportsActivity || Boolean(affinityLigandSmiles.trim()));

  const affinityReadyReason = workspaceTab !== 'components'
    ? 'Open Component tab to prepare affinity inputs.'
    : !affinityTargetFilePresent
      ? 'Upload target structure first.'
      : affinityPreviewLoading
        ? 'Building preview input...'
        : !affinityPreviewCurrent
          ? affinityPreviewError || 'Failed to prepare preview input from uploaded files.'
          : affinityUseActivity && !affinityTargetChainCount
            ? 'No target chain could be inferred from target structure.'
            : affinityUseActivity && !affinityLigandChainId.trim()
              ? 'No ligand chain is available for activity mode.'
              : affinityUseActivity && !affinityLigandSmiles.trim()
                ? 'Ligand SMILES is required for activity mode.'
                : '';

  const runBlockedReason = isPredictionWorkflow
    ? hasIncompleteComponents
      ? `Complete all components before run (${componentCompletion.filledCount}/${componentCompletion.total} ready).`
      : ''
    : isAffinityWorkflow
      ? affinityReadyReason
      : isLeadOptimizationWorkflow
        ? workspaceTab === 'components'
          ? ''
          : 'Open Fragments tab to run.'
        : 'Runner UI for this workflow is being integrated.';

  const runDisabled =
    submitting ||
    saving ||
    isRunRedirecting ||
    (!isPredictionWorkflow && !isAffinityWorkflow && !isLeadOptimizationWorkflow) ||
    (isPredictionWorkflow && hasIncompleteComponents) ||
    (isAffinityWorkflow && Boolean(affinityReadyReason)) ||
    (isLeadOptimizationWorkflow && workspaceTab !== 'components');

  return {
    componentStepLabel,
    isRunRedirecting,
    showQuickRunFab,
    affinityUseActivity,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    affinityReadyReason,
    runBlockedReason,
    runDisabled,
    canOpenRunMenu: false
  };
}
