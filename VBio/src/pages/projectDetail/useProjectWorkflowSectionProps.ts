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
  isAffinityWorkflow: boolean;
  workflowTitle: string;
  workflowShortTitle: string;
  projectTaskState: string;
  projectTaskId: string;
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
  snapshotLigandAtomPlddts: number[];
  snapshotPlddt: number | null;
  canEdit: boolean;
  submitting: boolean;
  affinityTargetFileName: string;
  affinityLigandFileName: string;
  affinityLigandSmiles: string;
  affinityPreviewLigandStructureText: string;
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
  onBackendChange: (backend: string) => void;
  onSeedChange: (seed: number | null) => void;
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
  isAffinityWorkflow,
  workflowTitle,
  workflowShortTitle,
  projectTaskState,
  projectTaskId,
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
  snapshotLigandAtomPlddts,
  snapshotPlddt,
  canEdit,
  submitting,
  affinityTargetFileName,
  affinityLigandFileName,
  affinityLigandSmiles,
  affinityPreviewLigandStructureText,
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
  onSeedChange
}: UseProjectWorkflowSectionPropsInput): UseProjectWorkflowSectionPropsResult {
  const onLeadOptimizationLigandSmilesChange = (value: string) => {
    handleLeadOptimizationLigandSmilesChangeAction({
      value,
      setDraft,
      withLeadOptimizationLigandSmiles
    });
  };

  const projectResultsSectionProps = buildProjectResultsSectionProps({
    isPredictionWorkflow,
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
    affinityLigandConfidenceHint: snapshotPlddt
  });
  const affinityWorkflowSectionProps = buildAffinityWorkflowSectionProps({
    canEdit,
    submitting,
    backend,
    targetFileName: affinityTargetFileName,
    ligandFileName: affinityLigandFileName,
    ligandSmiles: affinityLigandSmiles,
    ligandEditorInput: affinityLigandSmiles.trim() || affinityPreviewLigandStructureText,
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
    isAffinityWorkflow,
    backend,
    seed: seed ?? null,
    onBackendChange,
    onSeedChange
  });

  return {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  };
}
