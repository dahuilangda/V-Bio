import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { PredictionConstraint } from '../../types/models';
import { computeConstraintSelectionState } from './constraintSelection';
import { applyConstraintResiduePickInteraction } from './constraintPickInteraction';

interface DraftLike {
  inputConfig: {
    constraints: PredictionConstraint[];
  };
}

export function useConstraintWorkspaceActions<TDraft extends DraftLike>(params: {
  draft: TDraft | null;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  activeConstraintId: string | null;
  setActiveConstraintId: Dispatch<SetStateAction<string | null>>;
  selectedContactConstraintIds: string[];
  setSelectedContactConstraintIds: Dispatch<SetStateAction<string[]>>;
  selectedConstraintTemplateComponentId: string | null;
  setSelectedConstraintTemplateComponentId: Dispatch<SetStateAction<string | null>>;
  resolveTemplateComponentIdForConstraint: (constraint: PredictionConstraint) => string | null;
  constraintSelectionAnchorRef: MutableRefObject<string | null>;
  setWorkspaceTab: Dispatch<SetStateAction<'results' | 'basics' | 'components' | 'constraints'>>;
  setSidebarConstraintsOpen: Dispatch<SetStateAction<boolean>>;
  scrollToEditorBlock: (elementId: string) => void;
  constraintPickSlotRef: MutableRefObject<Record<string, 'first' | 'second'>>;
  activeChainInfos: Array<{ id: string; type: 'protein' | 'ligand' | 'dna' | 'rna'; componentId: string; residueCount?: number }>;
  selectedTemplatePreview: { componentId: string; chainId?: string | null } | null;
  selectedTemplateResidueIndexMap: Record<string, Record<number, number>>;
  setPickedResidue: Dispatch<SetStateAction<any>>;
  canEdit: boolean;
  ligandChainOptions: Array<{ id: string }>;
  isBondOnlyBackend: boolean;
}) {
  const {
    draft,
    setDraft,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintSelectionAnchorRef,
    setWorkspaceTab,
    setSidebarConstraintsOpen,
    scrollToEditorBlock,
    constraintPickSlotRef,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    ligandChainOptions,
    isBondOnlyBackend,
  } = params;

  const clearConstraintSelection = () => {
    setActiveConstraintId(null);
    setSelectedContactConstraintIds([]);
    constraintSelectionAnchorRef.current = null;
  };

  const selectConstraint = (constraintId: string, options?: { toggle?: boolean; range?: boolean }) => {
    if (!draft) return;
    const constraints = draft.inputConfig.constraints;
    const target = constraints.find((item) => item.id === constraintId);
    if (!target) {
      setActiveConstraintId(constraintId);
      return;
    }

    const suggestedTemplateComponentId = resolveTemplateComponentIdForConstraint(target);
    if (suggestedTemplateComponentId && suggestedTemplateComponentId !== selectedConstraintTemplateComponentId) {
      setSelectedConstraintTemplateComponentId(suggestedTemplateComponentId);
    }

    const nextSelection = computeConstraintSelectionState({
      constraints,
      constraintId,
      currentState: {
        activeConstraintId,
        selectedContactConstraintIds,
        anchorConstraintId: constraintSelectionAnchorRef.current,
      },
      options,
    });

    setActiveConstraintId(nextSelection.activeConstraintId);
    setSelectedContactConstraintIds(nextSelection.selectedContactConstraintIds);
    constraintSelectionAnchorRef.current = nextSelection.anchorConstraintId;
  };

  const jumpToConstraint = (constraintId: string, options?: { toggle?: boolean; range?: boolean }) => {
    setWorkspaceTab('constraints');
    selectConstraint(constraintId, options);
    setSidebarConstraintsOpen(true);
    scrollToEditorBlock(`constraint-card-${constraintId}`);
  };

  const navigateConstraint = (step: -1 | 1) => {
    if (!draft || draft.inputConfig.constraints.length === 0) return;
    const currentIndex = draft.inputConfig.constraints.findIndex((item) => item.id === activeConstraintId);
    const safeIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex =
      (safeIndex + step + draft.inputConfig.constraints.length) % draft.inputConfig.constraints.length;
    selectConstraint(draft.inputConfig.constraints[nextIndex].id);
  };

  const applyPickToSelectedConstraint = (pick: MolstarResiduePick) => {
    applyConstraintResiduePickInteraction({
      pick,
      activeChainInfos,
      selectedTemplatePreview,
      selectedTemplateResidueIndexMap,
      setPickedResidue,
      canEdit,
      draft,
      ligandChainOptions,
      isBondOnlyBackend,
      constraintPickSlotRef,
      constraintSelectionAnchorRef,
      setSelectedContactConstraintIds,
      setActiveConstraintId,
      setDraft,
      activeConstraintId,
      selectedContactConstraintIds,
    });
  };

  return {
    clearConstraintSelection,
    selectConstraint,
    jumpToConstraint,
    navigateConstraint,
    applyPickToSelectedConstraint,
  };
}
