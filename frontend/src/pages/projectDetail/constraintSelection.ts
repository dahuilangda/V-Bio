import type { PredictionConstraint } from '../../types/models';

export interface ConstraintSelectionOptions {
  toggle?: boolean;
  range?: boolean;
}

export interface ConstraintSelectionState {
  activeConstraintId: string | null;
  selectedContactConstraintIds: string[];
  anchorConstraintId: string | null;
}

export function computeConstraintSelectionState(params: {
  constraints: PredictionConstraint[];
  constraintId: string;
  currentState: ConstraintSelectionState;
  options?: ConstraintSelectionOptions;
}): ConstraintSelectionState {
  const { constraints, constraintId, currentState, options } = params;
  const target = constraints.find((item) => item.id === constraintId);
  if (!target) {
    return {
      activeConstraintId: constraintId,
      selectedContactConstraintIds: currentState.selectedContactConstraintIds,
      anchorConstraintId: currentState.anchorConstraintId,
    };
  }

  if (target.type !== 'contact') {
    return {
      activeConstraintId: constraintId,
      selectedContactConstraintIds: [],
      anchorConstraintId: null,
    };
  }

  const contactIds = constraints.filter((item) => item.type === 'contact').map((item) => item.id);
  const targetIndex = contactIds.indexOf(constraintId);
  if (targetIndex < 0) {
    return {
      activeConstraintId: constraintId,
      selectedContactConstraintIds: [constraintId],
      anchorConstraintId: constraintId,
    };
  }

  if (options?.range) {
    const anchorId =
      currentState.anchorConstraintId && contactIds.includes(currentState.anchorConstraintId)
        ? currentState.anchorConstraintId
        : constraintId;
    const anchorIndex = contactIds.indexOf(anchorId);
    const start = Math.min(anchorIndex, targetIndex);
    const end = Math.max(anchorIndex, targetIndex);
    const rangeIds = contactIds.slice(start, end + 1);
    const nextSelected = options.toggle
      ? Array.from(new Set([...currentState.selectedContactConstraintIds, ...rangeIds]))
      : rangeIds;
    return {
      activeConstraintId: constraintId,
      selectedContactConstraintIds: nextSelected,
      anchorConstraintId: constraintId,
    };
  }

  if (options?.toggle) {
    const wasSelected = currentState.selectedContactConstraintIds.includes(constraintId);
    const nextSelected = wasSelected
      ? currentState.selectedContactConstraintIds.filter((id) => id !== constraintId)
      : [...currentState.selectedContactConstraintIds, constraintId];

    let nextActive: string | null = currentState.activeConstraintId;
    if (nextSelected.length === 0) {
      nextActive = null;
    } else if (!wasSelected) {
      nextActive = constraintId;
    } else {
      nextActive = nextSelected[nextSelected.length - 1] || null;
    }

    return {
      activeConstraintId: nextActive,
      selectedContactConstraintIds: nextSelected,
      anchorConstraintId: constraintId,
    };
  }

  return {
    activeConstraintId: constraintId,
    selectedContactConstraintIds: [constraintId],
    anchorConstraintId: constraintId,
  };
}
