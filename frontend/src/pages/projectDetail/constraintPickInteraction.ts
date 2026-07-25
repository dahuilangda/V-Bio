import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { ConstraintResiduePick } from '../../components/project/ConstraintEditor';
import type { PredictionConstraint } from '../../types/models';
import { buildDefaultConstraint } from './constraintWorkspaceUtils';
import { applyConstraintPickToConstraints } from './constraintPickUpdater';
import { normalizeConstraintResiduePick } from './constraintPickNormalization';

interface DraftWithConstraints {
  inputConfig: {
    constraints: PredictionConstraint[];
  };
}

export function applyConstraintResiduePickInteraction<TDraft extends DraftWithConstraints>(params: {
  pick: MolstarResiduePick;
  activeChainInfos: Array<{ id: string; type: 'protein' | 'ligand' | 'dna' | 'rna'; componentId: string; residueCount?: number }>;
  selectedTemplatePreview: { componentId: string; chainId?: string | null } | null;
  selectedTemplateResidueIndexMap: Record<string, Record<number, number>>;
  setPickedResidue: Dispatch<SetStateAction<ConstraintResiduePick | null>>;
  canEdit: boolean;
  draft: TDraft | null;
  ligandChainOptions: Array<{ id: string }>;
  constraintsSupported: boolean;
  isBondOnlyBackend: boolean;
  constraintPickSlotRef: MutableRefObject<Record<string, 'first' | 'second'>>;
  updateConstraintPickSlot: (updater: (prev: Record<string, 'first' | 'second'>) => Record<string, 'first' | 'second'>) => void;
  constraintSelectionAnchorRef: MutableRefObject<string | null>;
  setSelectedContactConstraintIds: Dispatch<SetStateAction<string[]>>;
  setActiveConstraintId: Dispatch<SetStateAction<string | null>>;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  activeConstraintId: string | null;
  selectedContactConstraintIds: string[];
}): void {
  const {
    pick,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    draft,
    ligandChainOptions,
    constraintsSupported,
    isBondOnlyBackend,
    constraintPickSlotRef,
    updateConstraintPickSlot,
    constraintSelectionAnchorRef,
    setSelectedContactConstraintIds,
    setActiveConstraintId,
    setDraft,
    activeConstraintId,
    selectedContactConstraintIds,
  } = params;

  const normalizedPick = normalizeConstraintResiduePick({
    pick,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
  });
  if (!normalizedPick) return;

  const resolvedChainId = normalizedPick.chainId;
  const normalizedResidue = normalizedPick.residue;
  const nextPicked = {
    chainId: resolvedChainId,
    residue: normalizedResidue,
    atomName: normalizedPick.atomName,
  };

  setPickedResidue((prev) => {
    if (
      prev &&
      prev.chainId === nextPicked.chainId &&
      prev.residue === nextPicked.residue &&
      (prev.atomName || '') === (nextPicked.atomName || '')
    ) {
      return prev;
    }
    return nextPicked;
  });

  if (!canEdit || !constraintsSupported) return;

  const currentConstraints = draft?.inputConfig.constraints || [];
  if (currentConstraints.length === 0) {
    const created = buildDefaultConstraint({
      picked: { chainId: resolvedChainId, residue: normalizedResidue },
      activeChainInfos,
      ligandChainOptions,
      isBondOnlyBackend,
    });
    if (created.type === 'contact') {
      updateConstraintPickSlot((prev) => ({ ...prev, [created.id]: 'second' }));
      setSelectedContactConstraintIds([created.id]);
      constraintSelectionAnchorRef.current = created.id;
    }
    setActiveConstraintId(created.id);
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            inputConfig: {
              ...prev.inputConfig,
              constraints: [...prev.inputConfig.constraints, created],
            },
          }
        : prev
    );
    return;
  }

  const pickResult = applyConstraintPickToConstraints({
    constraints: currentConstraints,
    activeConstraintId,
    selectedContactConstraintIds,
    resolvedChainId,
    normalizedResidue,
    pickedAtomName: normalizedPick.atomName,
    pickSlotByConstraintId: constraintPickSlotRef.current,
  });
  updateConstraintPickSlot(() => pickResult.nextPickSlotByConstraintId);
  if (!pickResult.changed) return;

  setDraft((prev) =>
    prev
      ? {
          ...prev,
          inputConfig: {
            ...prev.inputConfig,
            constraints: pickResult.nextConstraints,
          },
        }
      : prev
  );
}
