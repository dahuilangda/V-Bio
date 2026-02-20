import type {
  CSSProperties,
  Dispatch,
  KeyboardEvent,
  PointerEvent,
  RefObject,
  SetStateAction
} from 'react';
import type {
  InputComponent,
  PredictionConstraint,
  ProjectInputConfig
} from '../../types/models';
import type { MolstarResidueHighlight, MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { ProjectWorkspaceDraft } from './workspaceTypes';
import {
  type BuildPredictionComponentsSidebarParams,
  type BuildPredictionConstraintsWorkspaceParams,
  buildPredictionComponentsSidebarProps,
  buildPredictionConstraintsWorkspaceProps
} from './workflowSectionProps';

interface UsePredictionWorkspacePropsInput {
  draft: ProjectWorkspaceDraft;
  setDraft: Dispatch<SetStateAction<ProjectWorkspaceDraft | null>>;
  filterConstraintsByBackend: (
    constraints: PredictionConstraint[],
    backend: string
  ) => PredictionConstraint[];

  constraintsWorkspaceRef: RefObject<HTMLDivElement | null>;
  isConstraintsResizing: boolean;
  constraintsGridStyle: CSSProperties;
  constraintCount: number;
  activeConstraintIndex: number;
  constraintTemplateOptions: BuildPredictionConstraintsWorkspaceParams['constraintTemplateOptions'];
  selectedTemplatePreview: BuildPredictionConstraintsWorkspaceParams['selectedTemplatePreview'];
  setSelectedConstraintTemplateComponentId: (componentId: string | null) => void;
  constraintPickModeEnabled: boolean;
  setConstraintPickModeEnabled: Dispatch<SetStateAction<boolean>>;
  canEdit: boolean;
  setWorkspaceTab: Dispatch<SetStateAction<'results' | 'basics' | 'components' | 'constraints'>>;
  navigateConstraint: (delta: -1 | 1) => void;
  pickedResidue: { chainId: string; residue: number; atomName?: string } | null;
  hasConstraintStructure: boolean;
  constraintStructureText: string;
  constraintStructureFormat: 'cif' | 'pdb';
  constraintViewerHighlightResidues: MolstarResidueHighlight[];
  constraintViewerActiveResidue: MolstarResidueHighlight | null;
  applyPickToSelectedConstraint: (pick: MolstarResiduePick) => void;
  handleConstraintsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  handleConstraintsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  clearConstraintSelection: () => void;
  activeConstraintId: string | null;
  selectedContactConstraintIds: string[];
  selectConstraint: (id: string, options?: { toggle?: boolean; range?: boolean }) => void;
  allowedConstraintTypes: Array<'contact' | 'bond' | 'pocket'>;
  isBondOnlyBackend: boolean;

  hasIncompleteComponents: boolean;
  componentCompletion: { incompleteCount: number };
  sidebarTypeOrder: Array<'protein' | 'ligand' | 'dna' | 'rna'>;
  componentTypeBuckets: BuildPredictionComponentsSidebarParams['componentTypeBuckets'];
  sidebarTypeOpen: BuildPredictionComponentsSidebarParams['sidebarTypeOpen'];
  setSidebarTypeOpen: Dispatch<SetStateAction<BuildPredictionComponentsSidebarParams['sidebarTypeOpen']>>;
  addComponentToDraft: (type: InputComponent['type']) => void;
  activeComponentId: string | null;
  jumpToComponent: (id: string) => void;
  sidebarConstraintsOpen: boolean;
  setSidebarConstraintsOpen: Dispatch<SetStateAction<boolean>>;
  addConstraintFromSidebar: () => void;
  hasActiveChains: boolean;
  selectedContactConstraintIdSet: Set<string>;
  jumpToConstraint: (id: string, options?: { toggle?: boolean; range?: boolean }) => void;
  constraintLabel: (type: 'contact' | 'bond' | 'pocket') => string;
  formatConstraintCombo: (constraint: PredictionConstraint) => string;
  formatConstraintDetail: (constraint: PredictionConstraint) => string;
  canEnableAffinityFromWorkspace: boolean;
  setAffinityEnabledFromWorkspace: (enabled: boolean) => void;
  selectedWorkspaceTarget: BuildPredictionComponentsSidebarParams['selectedWorkspaceTarget'];
  selectedWorkspaceLigand: BuildPredictionComponentsSidebarParams['selectedWorkspaceLigand'];
  workspaceTargetOptions: BuildPredictionComponentsSidebarParams['workspaceTargetOptions'];
  workspaceLigandSelectableOptions: BuildPredictionComponentsSidebarParams['workspaceLigandSelectableOptions'];
  setAffinityComponentFromWorkspace: (role: 'target' | 'ligand', componentId: string | null) => void;
  affinityEnableDisabledReason: string;

}

interface UsePredictionWorkspacePropsResult {
  predictionConstraintsWorkspaceProps: ReturnType<typeof buildPredictionConstraintsWorkspaceProps>;
  predictionComponentsSidebarProps: ReturnType<typeof buildPredictionComponentsSidebarProps>;
}

export function usePredictionWorkspaceProps({
  draft,
  setDraft,
  filterConstraintsByBackend,
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
  activeConstraintId,
  selectedContactConstraintIds,
  selectConstraint,
  allowedConstraintTypes,
  isBondOnlyBackend,
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
  addConstraintFromSidebar,
  hasActiveChains,
  selectedContactConstraintIdSet,
  jumpToConstraint,
  constraintLabel,
  formatConstraintCombo,
  formatConstraintDetail,
  canEnableAffinityFromWorkspace,
  setAffinityEnabledFromWorkspace,
  selectedWorkspaceTarget,
  selectedWorkspaceLigand,
  workspaceTargetOptions,
  workspaceLigandSelectableOptions,
  setAffinityComponentFromWorkspace,
  affinityEnableDisabledReason
}: UsePredictionWorkspacePropsInput): UsePredictionWorkspacePropsResult {
  const predictionConstraintsWorkspaceProps = buildPredictionConstraintsWorkspaceProps({
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
    components: draft.inputConfig.components,
    constraints: draft.inputConfig.constraints,
    properties: draft.inputConfig.properties,
    activeConstraintId,
    selectedContactConstraintIds,
    selectConstraint,
    allowedConstraintTypes,
    isBondOnlyBackend,
    onConstraintsChange: (constraints: PredictionConstraint[]) =>
      setDraft((d) =>
        d
          ? {
              ...d,
              inputConfig: {
                ...d.inputConfig,
                constraints: filterConstraintsByBackend(constraints, d.backend)
              }
            }
          : d
      ),
    onPropertiesChange: (properties: ProjectInputConfig['properties']) =>
      setDraft((d) =>
        d
          ? {
              ...d,
              inputConfig: {
                ...d.inputConfig,
                properties
              }
            }
          : d
      ),
  });

  const predictionComponentsSidebarProps = buildPredictionComponentsSidebarProps({
    canEdit,
    components: draft.inputConfig.components,
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
    constraints: draft.inputConfig.constraints,
    activeConstraintId,
    selectedContactConstraintIdSet,
    jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    properties: draft.inputConfig.properties,
    canEnableAffinityFromWorkspace,
    setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason
  });

  return {
    predictionConstraintsWorkspaceProps,
    predictionComponentsSidebarProps
  };
}
