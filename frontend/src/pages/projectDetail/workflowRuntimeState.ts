import { parseVirtualScreeningInput } from '../../utils/virtualScreening';
import type { InputComponent } from '../../types/models';

export interface BuildRunUiStateParams {
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  isPredictionWorkflow: boolean;
  isPeptideDesignWorkflow: boolean;
  isVirtualScreeningWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  hasIncompleteComponents: boolean;
  componentCompletion: { filledCount: number; total: number };
  virtualScreeningInput: string;
  virtualScreeningComponents: InputComponent[];
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
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    virtualScreeningInput,
    virtualScreeningComponents,
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

  const parsedScreening = isVirtualScreeningWorkflow
    ? parseVirtualScreeningInput(virtualScreeningInput)
    : null;
  const virtualScreeningProteins = virtualScreeningComponents.filter((component) => component.type === 'protein');
  const virtualScreeningUnsupported = virtualScreeningComponents.find(
    (component) => component.type !== 'protein' && component.type !== 'ligand'
  );
  const virtualScreeningIncomplete = virtualScreeningComponents.find(
    (component) => !String(component.sequence || '').trim()
  );
  const virtualScreeningInvalidProtein = virtualScreeningProteins.find((component) => {
    const sequence = String(component.sequence || '').replace(/\s+/g, '').toUpperCase();
    return Boolean(sequence) && !/^[ACDEFGHIKLMNPQRSTVWY]+$/.test(sequence);
  });
  const virtualScreeningReadyReason = !isVirtualScreeningWorkflow
    ? ''
    : virtualScreeningUnsupported
      ? 'Nesso-1 supports protein and ligand components only; remove DNA/RNA.'
      : virtualScreeningProteins.length === 0
        ? 'Add at least one target protein component before running.'
        : virtualScreeningIncomplete
          ? 'Complete every target-complex component before running.'
          : virtualScreeningInvalidProtein
            ? 'A protein contains residue codes unsupported by Nesso-1.'
      : parsedScreening && parsedScreening.errors.length > 0
        ? parsedScreening.errors[0]
        : !parsedScreening || parsedScreening.compounds.length === 0
          ? 'Add at least one compound SMILES before running.'
          : parsedScreening.compounds.length > 200
            ? 'Virtual Screening accepts at most 200 compounds per batch.'
            : '';

  const runBlockedReason = isVirtualScreeningWorkflow
    ? virtualScreeningReadyReason
    : isPeptideDesignWorkflow
    ? hasIncompleteComponents
      ? `Complete all components before run (${componentCompletion.filledCount}/${componentCompletion.total} ready).`
      : ''
    : isPredictionWorkflow
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
    (isVirtualScreeningWorkflow && Boolean(virtualScreeningReadyReason)) ||
    (isPredictionWorkflow && !isVirtualScreeningWorkflow && hasIncompleteComponents) ||
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
