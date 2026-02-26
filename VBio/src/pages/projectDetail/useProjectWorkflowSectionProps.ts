import type { CSSProperties, Dispatch, KeyboardEvent, PointerEvent, ReactNode, RefObject, SetStateAction } from 'react';
import type { InputComponent, ProteinTemplateUpload } from '../../types/models';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { AffinitySignalCard } from '../../components/project/AffinityWorkspace';
import type { LeadOptCandidatesUiState } from '../../components/project/leadopt/LeadOptCandidatesPanel';
import type { LeadOptPersistedUploads } from '../../components/project/leadopt/hooks/useLeadOptReferenceFragment';
import type {
  LeadOptMmpPersistedSnapshot,
  LeadOptPredictionRecord
} from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';
import {
  buildAffinityWorkflowSectionProps,
  buildLeadOptimizationWorkflowSectionProps,
  buildPredictionWorkflowSectionProps,
  buildProjectResultsSectionProps,
  buildWorkflowRuntimeSettingsSectionProps
} from './workflowSectionProps';
import { handleLeadOptimizationLigandSmilesChangeAction } from './editorActions';
import { withLeadOptimizationLigandSmiles } from '../../utils/leadOptimization';
import type { ProjectWorkspaceDraft, WorkspaceTab } from './workspaceTypes';

interface UseProjectWorkflowSectionPropsInput {
  isPredictionWorkflow: boolean;
  isPeptideDesignWorkflow: boolean;
  isAffinityWorkflow: boolean;
  workflowTitle: string;
  workflowShortTitle: string;
  projectTaskState: string;
  projectTaskId: string;
  statusInfo: Record<string, unknown> | null;
  progressPercent: number;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: CSSProperties;
  onResultsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResultsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  snapshotCards: AffinitySignalCard[];
  snapshotConfidence: Record<string, unknown> | null;
  resultChainIds: string[];
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  displayStructureText: string;
  displayStructureFormat: 'pdb' | 'cif';
  displayStructureColorMode: 'default' | 'alphafold';
  displayStructureName: string;
  confidenceBackend: string;
  projectBackend: string;
  predictionLigandPreview: ReactNode;
  predictionLigandRadarSmiles: string;
  hasAffinityDisplayStructure: boolean;
  affinityDisplayStructureText: string;
  affinityDisplayStructureFormat: 'pdb' | 'cif';
  affinityResultLigandSmiles: string;
  affinityTargetChainIds: string[];
  affinityLigandChainId: string;
  snapshotLigandAtomPlddts: number[];
  snapshotPlddt: number | null;
  snapshotIptm: number | null;
  snapshotSelectedPairIptm: number | null;
  selectedResultLigandSequence: string;
  canEdit: boolean;
  submitting: boolean;
  affinityTargetFileName: string;
  affinityLigandFileName: string;
  affinityLigandSmiles: string;
  affinityPreviewLigandSmiles: string;
  affinityUseMsa: boolean;
  affinityConfidenceOnlyUiValue: boolean;
  affinityConfidenceOnlyUiLocked: boolean;
  affinityHasLigand: boolean;
  affinityPreviewStructureText: string;
  affinityPreviewStructureFormat: 'pdb' | 'cif';
  affinityPreviewLigandOverlayText: string;
  affinityPreviewLigandOverlayFormat: 'pdb' | 'cif';
  onAffinityTargetFileChange: (file: File | null) => void;
  onAffinityLigandFileChange: (file: File | null) => void;
  onAffinityUseMsaChange: (checked: boolean) => void;
  onAffinityConfidenceOnlyChange: (checked: boolean) => void;
  setAffinityLigandSmiles: (value: string) => void;
  leadOptProteinSequence: string;
  leadOptLigandSmiles: string;
  leadOptTargetChain: string;
  leadOptLigandChain: string;
  leadOptReferenceScopeKey?: string;
  leadOptPersistedReferenceUploads?: LeadOptPersistedUploads;
  onLeadOptReferenceUploadsChange?: (uploads: LeadOptPersistedUploads) => void;
  onLeadOptMmpTaskQueued?: (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
    querySmiles: string;
    referenceUploads: LeadOptPersistedUploads;
  }) => void | Promise<void>;
  onLeadOptMmpTaskCompleted?: (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => void | Promise<void>;
  onLeadOptMmpTaskFailed?: (payload: { taskId: string; error: string }) => void | Promise<void>;
  onLeadOptUiStateChange?: (payload: { uiState: LeadOptCandidatesUiState }) => void | Promise<void>;
  onLeadOptPredictionQueued?: (payload: { taskId: string; backend: string; candidateSmiles: string }) => void | Promise<void>;
  onLeadOptPredictionStateChange?: (payload: {
    records: Record<string, LeadOptPredictionRecord>;
    referenceRecords: Record<string, LeadOptPredictionRecord>;
    summary: {
      total: number;
      queued: number;
      running: number;
      success: number;
      failure: number;
      latestTaskId: string;
    };
  }) => void | Promise<void>;
  onLeadOptNavigateToResults?: () => void;
  leadOptInitialMmpSnapshot?: LeadOptMmpPersistedSnapshot | null;
  setDraft: Dispatch<SetStateAction<ProjectWorkspaceDraft | null>>;
  setWorkspaceTab: Dispatch<SetStateAction<WorkspaceTab>>;
  onRegisterLeadOptHeaderRunAction?: (action: (() => void | Promise<void>) | null) => void;
  workspaceTab: WorkspaceTab;
  componentsWorkspaceRef: RefObject<HTMLDivElement>;
  isComponentsResizing: boolean;
  componentsGridStyle: CSSProperties;
  onComponentsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onComponentsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  components: InputComponent[];
  onComponentsChange: (components: InputComponent[]) => void;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  onProteinTemplateChange: (componentId: string, upload: ProteinTemplateUpload | null) => void;
  activeComponentId: string | null;
  setActiveComponentId: Dispatch<SetStateAction<string | null>>;
  onProteinTemplateResiduePick: (pick: MolstarResiduePick) => void;
  predictionConstraintsWorkspaceProps: ReturnType<typeof buildPredictionWorkflowSectionProps>['constraintsWorkspaceProps'];
  predictionComponentsSidebarProps: ReturnType<typeof buildPredictionWorkflowSectionProps>['componentsSidebarProps'];
  backend: string;
  seed: number | null;
  peptideDesignMode: 'linear' | 'cyclic' | 'bicyclic';
  peptideBinderLength: number;
  peptideUseInitialSequence: boolean;
  peptideInitialSequence: string;
  peptideSequenceMask: string;
  peptideIterations: number;
  peptidePopulationSize: number;
  peptideEliteSize: number;
  peptideMutationRate: number;
  peptideBicyclicLinkerCcd: 'SEZ' | '29N' | 'BS3';
  peptideBicyclicCysPositionMode: 'auto' | 'manual';
  peptideBicyclicFixTerminalCys: boolean;
  peptideBicyclicIncludeExtraCys: boolean;
  peptideBicyclicCys1Pos: number;
  peptideBicyclicCys2Pos: number;
  peptideBicyclicCys3Pos: number;
  onBackendChange: (backend: string) => void;
  onSeedChange: (seed: number | null) => void;
  onPeptideDesignModeChange: (mode: 'linear' | 'cyclic' | 'bicyclic') => void;
  onPeptideBinderLengthChange: (value: number) => void;
  onPeptideUseInitialSequenceChange: (value: boolean) => void;
  onPeptideInitialSequenceChange: (value: string) => void;
  onPeptideSequenceMaskChange: (value: string) => void;
  onPeptideIterationsChange: (value: number) => void;
  onPeptidePopulationSizeChange: (value: number) => void;
  onPeptideEliteSizeChange: (value: number) => void;
  onPeptideMutationRateChange: (value: number) => void;
  onPeptideBicyclicLinkerCcdChange: (value: 'SEZ' | '29N' | 'BS3') => void;
  onPeptideBicyclicCysPositionModeChange: (value: 'auto' | 'manual') => void;
  onPeptideBicyclicFixTerminalCysChange: (value: boolean) => void;
  onPeptideBicyclicIncludeExtraCysChange: (value: boolean) => void;
  onPeptideBicyclicCys1PosChange: (value: number) => void;
  onPeptideBicyclicCys2PosChange: (value: number) => void;
  onPeptideBicyclicCys3PosChange: (value: number) => void;
}

interface UseProjectWorkflowSectionPropsResult {
  projectResultsSectionProps: ReturnType<typeof buildProjectResultsSectionProps>;
  affinityWorkflowSectionProps: ReturnType<typeof buildAffinityWorkflowSectionProps>;
  leadOptimizationWorkflowSectionProps: ReturnType<typeof buildLeadOptimizationWorkflowSectionProps>;
  predictionWorkflowSectionProps: ReturnType<typeof buildPredictionWorkflowSectionProps>;
  workflowRuntimeSettingsSectionProps: ReturnType<typeof buildWorkflowRuntimeSettingsSectionProps>;
}

export function useProjectWorkflowSectionProps({
  isPredictionWorkflow,
  isPeptideDesignWorkflow,
  isAffinityWorkflow,
  workflowTitle,
  workflowShortTitle,
  projectTaskState,
  projectTaskId,
  statusInfo,
  progressPercent,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onResultsResizerPointerDown,
  onResultsResizerKeyDown,
  snapshotCards,
  snapshotConfidence,
  resultChainIds,
  selectedResultTargetChainId,
  selectedResultLigandChainId,
  displayStructureText,
  displayStructureFormat,
  displayStructureColorMode,
  displayStructureName,
  confidenceBackend,
  projectBackend,
  predictionLigandPreview,
  predictionLigandRadarSmiles,
  hasAffinityDisplayStructure,
  affinityDisplayStructureText,
  affinityDisplayStructureFormat,
  affinityResultLigandSmiles,
  affinityTargetChainIds,
  affinityLigandChainId,
  snapshotLigandAtomPlddts,
  snapshotPlddt,
  snapshotIptm,
  snapshotSelectedPairIptm,
  selectedResultLigandSequence,
  canEdit,
  submitting,
  affinityTargetFileName,
  affinityLigandFileName,
  affinityLigandSmiles,
  affinityPreviewLigandSmiles,
  affinityUseMsa,
  affinityConfidenceOnlyUiValue,
  affinityConfidenceOnlyUiLocked,
  affinityHasLigand,
  affinityPreviewStructureText,
  affinityPreviewStructureFormat,
  affinityPreviewLigandOverlayText,
  affinityPreviewLigandOverlayFormat,
  onAffinityTargetFileChange,
  onAffinityLigandFileChange,
  onAffinityUseMsaChange,
  onAffinityConfidenceOnlyChange,
  setAffinityLigandSmiles,
  leadOptProteinSequence,
  leadOptLigandSmiles,
  leadOptTargetChain,
  leadOptLigandChain,
  leadOptReferenceScopeKey,
  leadOptPersistedReferenceUploads,
  onLeadOptReferenceUploadsChange,
  onLeadOptMmpTaskQueued,
  onLeadOptMmpTaskCompleted,
  onLeadOptMmpTaskFailed,
  onLeadOptUiStateChange,
  onLeadOptPredictionQueued,
  onLeadOptPredictionStateChange,
  onLeadOptNavigateToResults,
  leadOptInitialMmpSnapshot,
  setDraft,
  setWorkspaceTab,
  onRegisterLeadOptHeaderRunAction,
  workspaceTab,
  componentsWorkspaceRef,
  isComponentsResizing,
  componentsGridStyle,
  onComponentsResizerPointerDown,
  onComponentsResizerKeyDown,
  components,
  onComponentsChange,
  proteinTemplates,
  onProteinTemplateChange,
  activeComponentId,
  setActiveComponentId,
  onProteinTemplateResiduePick,
  predictionConstraintsWorkspaceProps,
  predictionComponentsSidebarProps,
  backend,
  seed,
  onBackendChange,
  onSeedChange,
  peptideDesignMode,
  peptideBinderLength,
  peptideUseInitialSequence,
  peptideInitialSequence,
  peptideSequenceMask,
  peptideIterations,
  peptidePopulationSize,
  peptideEliteSize,
  peptideMutationRate,
  peptideBicyclicLinkerCcd,
  peptideBicyclicCysPositionMode,
  peptideBicyclicFixTerminalCys,
  peptideBicyclicIncludeExtraCys,
  peptideBicyclicCys1Pos,
  peptideBicyclicCys2Pos,
  peptideBicyclicCys3Pos,
  onPeptideDesignModeChange,
  onPeptideBinderLengthChange,
  onPeptideUseInitialSequenceChange,
  onPeptideInitialSequenceChange,
  onPeptideSequenceMaskChange,
  onPeptideIterationsChange,
  onPeptidePopulationSizeChange,
  onPeptideEliteSizeChange,
  onPeptideMutationRateChange,
  onPeptideBicyclicLinkerCcdChange,
  onPeptideBicyclicCysPositionModeChange,
  onPeptideBicyclicFixTerminalCysChange,
  onPeptideBicyclicIncludeExtraCysChange,
  onPeptideBicyclicCys1PosChange,
  onPeptideBicyclicCys2PosChange,
  onPeptideBicyclicCys3PosChange
}: UseProjectWorkflowSectionPropsInput): UseProjectWorkflowSectionPropsResult {
  const onLeadOptimizationLigandSmilesChange = (value: string) => {
    handleLeadOptimizationLigandSmilesChangeAction({
      value,
      setDraft,
      withLeadOptimizationLigandSmiles
    });
  };
  const affinityEffectiveLigandSmiles = affinityLigandSmiles.trim() || affinityPreviewLigandSmiles.trim();

  const projectResultsSectionProps = buildProjectResultsSectionProps({
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    workflowTitle,
    workflowShortTitle,
    projectTaskState,
    projectTaskId,
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onResizerPointerDown: onResultsResizerPointerDown,
    onResizerKeyDown: onResultsResizerKeyDown,
    snapshotCards,
    snapshotConfidence: snapshotConfidence || {},
    resultChainIds,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    displayStructureText,
    displayStructureFormat,
    displayStructureColorMode,
    displayStructureName,
    confidenceBackend,
    projectBackend,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    hasAffinityDisplayStructure,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    affinityLigandSmiles: affinityResultLigandSmiles,
    affinityPrimaryTargetChainId: affinityTargetChainIds[0] || null,
    affinityLigandAtomPlddts: snapshotLigandAtomPlddts,
    affinityLigandConfidenceHint: snapshotPlddt,
    selectedResultLigandSequence,
    peptideFallbackPlddt: snapshotPlddt,
    peptideFallbackIptm: snapshotSelectedPairIptm ?? snapshotIptm,
    statusInfo,
    progressPercent
  });
  const affinityWorkflowSectionProps = buildAffinityWorkflowSectionProps({
    canEdit,
    submitting,
    backend,
    targetFileName: affinityTargetFileName,
    ligandFileName: affinityLigandFileName,
    ligandSmiles: affinityEffectiveLigandSmiles,
    ligandEditorInput: affinityEffectiveLigandSmiles,
    useMsa: affinityUseMsa,
    confidenceOnly: affinityConfidenceOnlyUiValue,
    confidenceOnlyLocked: affinityConfidenceOnlyUiLocked,
    confidenceOnlyHint: affinityConfidenceOnlyUiLocked
      ? affinityHasLigand
        ? 'Only small-molecule ligand supports activity.'
        : 'No ligand uploaded: confidence only.'
      : '',
    previewTargetStructureText: affinityPreviewStructureText,
    previewTargetStructureFormat: affinityPreviewStructureFormat,
    previewLigandStructureText: affinityPreviewLigandOverlayText,
    previewLigandStructureFormat: affinityPreviewLigandOverlayFormat,
    previewLigandChainId: affinityLigandChainId,
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onTargetFileChange: onAffinityTargetFileChange,
    onLigandFileChange: onAffinityLigandFileChange,
    onUseMsaChange: onAffinityUseMsaChange,
    onConfidenceOnlyChange: onAffinityConfidenceOnlyChange,
    onBackendChange,
    onLigandSmilesChange: setAffinityLigandSmiles,
    onResizerPointerDown: onResultsResizerPointerDown,
    onResizerKeyDown: onResultsResizerKeyDown
  });
  const leadOptimizationWorkflowSectionProps = buildLeadOptimizationWorkflowSectionProps({
    workspaceTab,
    canEdit,
    submitting,
    backend,
    onNavigateToResults: onLeadOptNavigateToResults || (() => setWorkspaceTab('results')),
    onRegisterHeaderRunAction: onRegisterLeadOptHeaderRunAction,
    proteinSequence: leadOptProteinSequence,
    ligandSmiles: leadOptLigandSmiles,
    targetChain: leadOptTargetChain,
    ligandChain: leadOptLigandChain,
    onLigandSmilesChange: onLeadOptimizationLigandSmilesChange,
    referenceScopeKey: leadOptReferenceScopeKey,
    persistedReferenceUploads: leadOptPersistedReferenceUploads,
    onReferenceUploadsChange: onLeadOptReferenceUploadsChange,
    onMmpTaskQueued: onLeadOptMmpTaskQueued,
    onMmpTaskCompleted: onLeadOptMmpTaskCompleted,
    onMmpTaskFailed: onLeadOptMmpTaskFailed,
    onMmpUiStateChange: onLeadOptUiStateChange,
    onPredictionQueued: onLeadOptPredictionQueued,
    onPredictionStateChange: onLeadOptPredictionStateChange,
    initialMmpSnapshot: leadOptInitialMmpSnapshot
  });
  const predictionWorkflowSectionProps = buildPredictionWorkflowSectionProps({
    workspaceTab,
    canEdit,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    onComponentsResizerPointerDown,
    onComponentsResizerKeyDown,
    components,
    onComponentsChange,
    proteinTemplates,
    onProteinTemplateChange,
    activeComponentId,
    onActiveComponentIdChange: (id: string | null) => setActiveComponentId(id),
    onProteinTemplateResiduePick,
    constraintsWorkspaceProps: predictionConstraintsWorkspaceProps,
    componentsSidebarProps: predictionComponentsSidebarProps
  });
  const workflowRuntimeSettingsSectionProps = buildWorkflowRuntimeSettingsSectionProps({
    canEdit,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    backend,
    seed: seed ?? null,
    peptideDesignMode,
    peptideBinderLength,
    peptideUseInitialSequence,
    peptideInitialSequence,
    peptideSequenceMask,
    peptideIterations,
    peptidePopulationSize,
    peptideEliteSize,
    peptideMutationRate,
    peptideBicyclicLinkerCcd,
    peptideBicyclicCysPositionMode,
    peptideBicyclicFixTerminalCys,
    peptideBicyclicIncludeExtraCys,
    peptideBicyclicCys1Pos,
    peptideBicyclicCys2Pos,
    peptideBicyclicCys3Pos,
    onBackendChange,
    onSeedChange,
    onPeptideDesignModeChange,
    onPeptideBinderLengthChange,
    onPeptideUseInitialSequenceChange,
    onPeptideInitialSequenceChange,
    onPeptideSequenceMaskChange,
    onPeptideIterationsChange,
    onPeptidePopulationSizeChange,
    onPeptideEliteSizeChange,
    onPeptideMutationRateChange,
    onPeptideBicyclicLinkerCcdChange,
    onPeptideBicyclicCysPositionModeChange,
    onPeptideBicyclicFixTerminalCysChange,
    onPeptideBicyclicIncludeExtraCysChange,
    onPeptideBicyclicCys1PosChange,
    onPeptideBicyclicCys2PosChange,
    onPeptideBicyclicCys3PosChange
  });

  return {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  };
}
