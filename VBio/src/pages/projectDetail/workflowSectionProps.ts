import type { CSSProperties, Dispatch, KeyboardEvent, PointerEvent, RefObject, SetStateAction } from 'react';
import type { MolstarResidueHighlight, MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { ProjectResultsSectionProps } from '../../components/project/ProjectResultsSection';
import type { InputComponent, PredictionConstraint, PredictionConstraintType, PredictionProperties } from '../../types/models';
import type { AffinityWorkflowSectionProps } from './AffinityWorkflowSection';
import type { LeadOptimizationWorkflowSectionProps } from './LeadOptimizationWorkflowSection';
import type { PredictionComponentsSidebarProps } from './PredictionComponentsSidebar';
import type { PredictionConstraintsWorkspaceProps } from './PredictionConstraintsWorkspace';
import type { PredictionWorkflowSectionProps } from './PredictionWorkflowSection';
import type { WorkflowRuntimeSettingsSectionProps } from './WorkflowRuntimeSettingsSection';
import type { MetricTone } from './projectMetrics';

export interface BuildPredictionConstraintsWorkspaceParams {
  constraintsWorkspaceRef: RefObject<HTMLDivElement | null>;
  isConstraintsResizing: boolean;
  constraintsGridStyle: CSSProperties;
  constraintCount: number;
  activeConstraintIndex: number;
  constraintTemplateOptions: PredictionConstraintsWorkspaceProps['constraintTemplateOptions'];
  selectedTemplatePreview: PredictionConstraintsWorkspaceProps['selectedTemplatePreview'];
  setSelectedConstraintTemplateComponentId: (componentId: string | null) => void;
  constraintPickModeEnabled: boolean;
  setConstraintPickModeEnabled: Dispatch<SetStateAction<boolean>>;
  canEdit: boolean;
  setWorkspaceTab: Dispatch<SetStateAction<'results' | 'basics' | 'components' | 'constraints'>>;
  navigateConstraint: (delta: -1 | 1) => void;
  pickedResidue: PredictionConstraintsWorkspaceProps['pickedResidue'];
  hasConstraintStructure: boolean;
  constraintStructureText: string;
  constraintStructureFormat: 'cif' | 'pdb';
  constraintViewerHighlightResidues: MolstarResidueHighlight[];
  constraintViewerActiveResidue: MolstarResidueHighlight | null;
  applyPickToSelectedConstraint: (pick: MolstarResiduePick) => void;
  handleConstraintsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  handleConstraintsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  clearConstraintSelection: () => void;
  components: InputComponent[];
  constraints: PredictionConstraint[];
  properties: PredictionProperties;
  activeConstraintId: string | null;
  selectedContactConstraintIds: string[];
  selectConstraint: (id: string, options?: { toggle?: boolean; range?: boolean }) => void;
  allowedConstraintTypes: PredictionConstraintType[];
  isBondOnlyBackend: boolean;
  onConstraintsChange: (constraints: PredictionConstraint[]) => void;
  onPropertiesChange: (properties: PredictionProperties) => void;
}

export function buildPredictionConstraintsWorkspaceProps(
  params: BuildPredictionConstraintsWorkspaceParams
): Omit<PredictionConstraintsWorkspaceProps, 'visible'> {
  const {
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions,
    selectedTemplatePreview,
    setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    canEdit,
    setWorkspaceTab,
    navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    applyPickToSelectedConstraint,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    clearConstraintSelection,
    components,
    constraints,
    properties,
    activeConstraintId,
    selectedContactConstraintIds,
    selectConstraint,
    allowedConstraintTypes,
    isBondOnlyBackend,
    onConstraintsChange,
    onPropertiesChange
  } = params;

  return {
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions,
    selectedTemplatePreview,
    onSelectedConstraintTemplateComponentIdChange: setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    onToggleConstraintPickMode: () => setConstraintPickModeEnabled((prev) => !prev),
    canEdit,
    onBackToComponents: () => setWorkspaceTab('components'),
    onNavigateConstraint: navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    onApplyPickToSelectedConstraint: applyPickToSelectedConstraint,
    onConstraintsResizerPointerDown: handleConstraintsResizerPointerDown,
    onConstraintsResizerKeyDown: handleConstraintsResizerKeyDown,
    onClearConstraintSelection: clearConstraintSelection,
    components,
    constraints,
    properties,
    activeConstraintId,
    selectedConstraintIds: selectedContactConstraintIds,
    onSelectedConstraintIdChange: (id: string | null) => {
      if (id) {
        selectConstraint(id);
      } else {
        clearConstraintSelection();
      }
    },
    onConstraintClick: (id: string, options?: { toggle?: boolean; range?: boolean }) =>
      selectConstraint(id, {
        toggle: Boolean(options?.toggle),
        range: Boolean(options?.range)
      }),
    allowedConstraintTypes,
    isBondOnlyBackend,
    onConstraintsChange,
    onPropertiesChange,
    disabled: !canEdit
  };
}

export interface BuildPredictionComponentsSidebarParams {
  canEdit: boolean;
  components: InputComponent[];
  hasIncompleteComponents: boolean;
  componentCompletion: { incompleteCount: number };
  sidebarTypeOrder: Array<'protein' | 'ligand' | 'dna' | 'rna'>;
  componentTypeBuckets: PredictionComponentsSidebarProps['componentTypeBuckets'];
  sidebarTypeOpen: PredictionComponentsSidebarProps['sidebarTypeOpen'];
  setSidebarTypeOpen: Dispatch<SetStateAction<PredictionComponentsSidebarProps['sidebarTypeOpen']>>;
  addComponentToDraft: (type: InputComponent['type']) => void;
  activeComponentId: string | null;
  jumpToComponent: (id: string) => void;
  sidebarConstraintsOpen: boolean;
  setSidebarConstraintsOpen: Dispatch<SetStateAction<boolean>>;
  constraintCount: number;
  addConstraintFromSidebar: () => void;
  hasActiveChains: boolean;
  constraints: PredictionConstraint[];
  activeConstraintId: string | null;
  selectedContactConstraintIdSet: Set<string>;
  jumpToConstraint: (id: string, options?: { toggle?: boolean; range?: boolean }) => void;
  constraintLabel: (type: PredictionConstraintType) => string;
  formatConstraintCombo: (constraint: PredictionConstraint) => string;
  formatConstraintDetail: (constraint: PredictionConstraint) => string;
  properties: PredictionProperties;
  canEnableAffinityFromWorkspace: boolean;
  setAffinityEnabledFromWorkspace: (enabled: boolean) => void;
  selectedWorkspaceTarget: PredictionComponentsSidebarProps['selectedWorkspaceTarget'];
  selectedWorkspaceLigand: PredictionComponentsSidebarProps['selectedWorkspaceLigand'];
  workspaceTargetOptions: PredictionComponentsSidebarProps['workspaceTargetOptions'];
  workspaceLigandSelectableOptions: PredictionComponentsSidebarProps['workspaceLigandSelectableOptions'];
  setAffinityComponentFromWorkspace: (role: 'target' | 'ligand', componentId: string | null) => void;
  affinityEnableDisabledReason: string;
}

export function buildPredictionComponentsSidebarProps(
  params: BuildPredictionComponentsSidebarParams
): Omit<PredictionComponentsSidebarProps, 'visible'> {
  const {
    canEdit,
    components,
    hasIncompleteComponents,
    componentCompletion,
    sidebarTypeOrder,
    componentTypeBuckets,
    sidebarTypeOpen,
    setSidebarTypeOpen,
    addComponentToDraft,
    activeComponentId,
    jumpToComponent,
    sidebarConstraintsOpen,
    setSidebarConstraintsOpen,
    constraintCount,
    addConstraintFromSidebar,
    hasActiveChains,
    constraints,
    activeConstraintId,
    selectedContactConstraintIdSet,
    jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    properties,
    canEnableAffinityFromWorkspace,
    setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason
  } = params;

  return {
    canEdit,
    components,
    hasIncompleteComponents,
    componentCompletion,
    sidebarTypeOrder,
    componentTypeBuckets,
    sidebarTypeOpen,
    onSidebarTypeToggle: (type: 'protein' | 'ligand' | 'dna' | 'rna') =>
      setSidebarTypeOpen((prev) => ({ ...prev, [type]: !prev[type] })),
    onAddComponent: addComponentToDraft,
    activeComponentId,
    onJumpToComponent: jumpToComponent,
    sidebarConstraintsOpen,
    onSidebarConstraintsToggle: () => setSidebarConstraintsOpen((prev) => !prev),
    constraintCount,
    onAddConstraint: addConstraintFromSidebar,
    hasActiveChains,
    constraints,
    activeConstraintId,
    selectedContactConstraintIdSet,
    onJumpToConstraint: jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    properties,
    canEnableAffinityFromWorkspace,
    onSetAffinityEnabledFromWorkspace: setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    onSetAffinityComponentFromWorkspace: setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason
  };
}

export function buildProjectResultsSectionProps(
  params: Omit<ProjectResultsSectionProps, 'snapshotCards'> & {
    snapshotCards: Array<{ key: string; label: string; value: string; detail: string; tone: MetricTone }>;
  }
): ProjectResultsSectionProps {
  return {
    ...params,
    snapshotCards: params.snapshotCards
  };
}

export function buildAffinityWorkflowSectionProps(
  params: Omit<AffinityWorkflowSectionProps, 'visible'>
): Omit<AffinityWorkflowSectionProps, 'visible'> {
  return params;
}

export function buildLeadOptimizationWorkflowSectionProps(
  params: Omit<LeadOptimizationWorkflowSectionProps, 'visible'>
): Omit<LeadOptimizationWorkflowSectionProps, 'visible'> {
  return params;
}

export function buildPredictionWorkflowSectionProps(
  params: Omit<PredictionWorkflowSectionProps, 'visible'>
): Omit<PredictionWorkflowSectionProps, 'visible'> {
  return params;
}

export function buildWorkflowRuntimeSettingsSectionProps(
  params: Omit<WorkflowRuntimeSettingsSectionProps, 'visible'>
): Omit<WorkflowRuntimeSettingsSectionProps, 'visible'> {
  return params;
}
