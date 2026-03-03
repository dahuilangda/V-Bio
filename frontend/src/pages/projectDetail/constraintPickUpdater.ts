import type { PredictionConstraint } from '../../types/models';

export type ConstraintPickSlot = 'first' | 'second';

export interface ApplyConstraintPickInput {
  constraints: PredictionConstraint[];
  activeConstraintId: string | null;
  selectedContactConstraintIds: string[];
  resolvedChainId: string;
  normalizedResidue: number;
  pickedAtomName?: string | null;
  pickSlotByConstraintId: Record<string, ConstraintPickSlot>;
}

export interface ApplyConstraintPickResult {
  changed: boolean;
  nextConstraints: PredictionConstraint[];
  nextPickSlotByConstraintId: Record<string, ConstraintPickSlot>;
}

export function applyConstraintPickToConstraints(input: ApplyConstraintPickInput): ApplyConstraintPickResult {
  const {
    constraints,
    activeConstraintId,
    selectedContactConstraintIds,
    resolvedChainId,
    normalizedResidue,
    pickedAtomName,
    pickSlotByConstraintId,
  } = input;

  if (!constraints.length) {
    return {
      changed: false,
      nextConstraints: constraints,
      nextPickSlotByConstraintId: { ...pickSlotByConstraintId },
    };
  }

  const activeIndex = activeConstraintId ? constraints.findIndex((item) => item.id === activeConstraintId) : -1;
  const activeConstraint = activeIndex >= 0 ? constraints[activeIndex] : null;
  const selectedContactIdSet = new Set(selectedContactConstraintIds);
  const selectedContactIndexes = constraints.reduce<number[]>((acc, item, index) => {
    if (item.type === 'contact' && selectedContactIdSet.has(item.id)) {
      acc.push(index);
    }
    return acc;
  }, []);

  let targetIndexes: number[] = [];
  if (activeConstraint?.type === 'contact') {
    targetIndexes = selectedContactIndexes.length > 0 ? selectedContactIndexes : activeIndex >= 0 ? [activeIndex] : [];
  } else if (activeIndex >= 0) {
    targetIndexes = [activeIndex];
  } else if (selectedContactIndexes.length > 0) {
    targetIndexes = selectedContactIndexes;
  } else if (constraints.length > 0) {
    targetIndexes = [0];
  }

  if (targetIndexes.length === 0) {
    return {
      changed: false,
      nextConstraints: constraints,
      nextPickSlotByConstraintId: { ...pickSlotByConstraintId },
    };
  }

  const targetIndexSet = new Set(targetIndexes);
  const nextPickSlots: Record<string, ConstraintPickSlot> = { ...pickSlotByConstraintId };

  let hasChanges = false;
  const nextConstraints = constraints.map((item, index) => {
    if (!targetIndexSet.has(index)) return item;

    if (item.type === 'contact') {
      const token1Matches = item.token1_chain === resolvedChainId;
      const token2Matches = item.token2_chain === resolvedChainId;
      const slot: ConstraintPickSlot =
        token1Matches && !token2Matches
          ? 'first'
          : token2Matches && !token1Matches
            ? 'second'
            : nextPickSlots[item.id] || 'first';

      const updated =
        slot === 'first'
          ? { ...item, token1_chain: resolvedChainId, token1_residue: normalizedResidue }
          : { ...item, token2_chain: resolvedChainId, token2_residue: normalizedResidue };

      nextPickSlots[item.id] = slot === 'first' ? 'second' : 'first';
      if (
        updated.token1_chain !== item.token1_chain ||
        updated.token1_residue !== item.token1_residue ||
        updated.token2_chain !== item.token2_chain ||
        updated.token2_residue !== item.token2_residue
      ) {
        hasChanges = true;
        return updated;
      }
      return item;
    }

    if (item.type === 'bond') {
      const atom1Matches = item.atom1_chain === resolvedChainId;
      const atom2Matches = item.atom2_chain === resolvedChainId;
      const slot: ConstraintPickSlot =
        atom1Matches && !atom2Matches
          ? 'first'
          : atom2Matches && !atom1Matches
            ? 'second'
            : nextPickSlots[item.id] || 'first';
      const atomName = (pickedAtomName || (slot === 'first' ? item.atom1_atom : item.atom2_atom) || 'CA').toUpperCase();

      const updated =
        slot === 'first'
          ? {
              ...item,
              atom1_chain: resolvedChainId,
              atom1_residue: normalizedResidue,
              atom1_atom: atomName,
            }
          : {
              ...item,
              atom2_chain: resolvedChainId,
              atom2_residue: normalizedResidue,
              atom2_atom: atomName,
            };

      nextPickSlots[item.id] = slot === 'first' ? 'second' : 'first';
      if (
        updated.atom1_chain !== item.atom1_chain ||
        updated.atom1_residue !== item.atom1_residue ||
        updated.atom1_atom !== item.atom1_atom ||
        updated.atom2_chain !== item.atom2_chain ||
        updated.atom2_residue !== item.atom2_residue ||
        updated.atom2_atom !== item.atom2_atom
      ) {
        hasChanges = true;
        return updated;
      }
      return item;
    }

    const exists = item.contacts.some((contact) => contact[0] === resolvedChainId && contact[1] === normalizedResidue);
    if (exists) return item;
    hasChanges = true;
    return {
      ...item,
      contacts: [...item.contacts, [resolvedChainId, normalizedResidue] as [string, number]],
    };
  });

  return {
    changed: hasChanges,
    nextConstraints,
    nextPickSlotByConstraintId: nextPickSlots,
  };
}
