import type { InputComponent } from '../../types/models';
import { buildRunUiState } from './workflowRuntimeState';
import type { WorkspaceTab } from './workspaceTypes';

interface UseProjectRunStateInput {
  workspaceTab: WorkspaceTab;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  hasIncompleteComponents: boolean;
  componentCompletion: { filledCount: number; total: number };
  submitting: boolean;
  saving: boolean;
  runRedirectTaskId: string | null;
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

interface UseProjectRunStateResult {
  componentStepLabel: string;
  isRunRedirecting: boolean;
  showQuickRunFab: boolean;
  affinityConfidenceOnlyUiValue: boolean;
  affinityConfidenceOnlyUiLocked: boolean;
  runBlockedReason: string;
  runDisabled: boolean;
  canOpenRunMenu: boolean;
  sidebarTypeOrder: InputComponent['type'][];
}

export function useProjectRunState(input: UseProjectRunStateInput): UseProjectRunStateResult {
  const runUiState = buildRunUiState({
    workspaceTab: input.workspaceTab,
    isPredictionWorkflow: input.isPredictionWorkflow,
    isAffinityWorkflow: input.isAffinityWorkflow,
    isLeadOptimizationWorkflow: input.isLeadOptimizationWorkflow,
    hasIncompleteComponents: input.hasIncompleteComponents,
    componentCompletion: input.componentCompletion,
    submitting: input.submitting,
    saving: input.saving,
    isRunRedirecting: Boolean(input.runRedirectTaskId),
    showFloatingRunButton: input.showFloatingRunButton,
    affinityTargetFilePresent: input.affinityTargetFilePresent,
    affinityPreviewLoading: input.affinityPreviewLoading,
    affinityPreviewCurrent: input.affinityPreviewCurrent,
    affinityPreviewError: input.affinityPreviewError,
    affinityTargetChainCount: input.affinityTargetChainCount,
    affinityLigandChainId: input.affinityLigandChainId,
    affinityLigandSmiles: input.affinityLigandSmiles,
    affinityHasLigand: input.affinityHasLigand,
    affinitySupportsActivity: input.affinitySupportsActivity,
    affinityConfidenceOnly: input.affinityConfidenceOnly,
    affinityConfidenceOnlyLocked: input.affinityConfidenceOnlyLocked,
    draftBackend: input.draftBackend
  });

  return {
    ...runUiState,
    sidebarTypeOrder: ['protein', 'ligand', 'dna', 'rna']
  };
}
