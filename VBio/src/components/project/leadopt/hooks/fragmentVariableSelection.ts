import type { LigandFragmentItem } from '../../LigandFragmentSketcher';

export interface VariableSelectionResolution {
  mergedFragment: LigandFragmentItem | null;
  effectiveItems: LigandFragmentItem[];
  variableSmilesList: string[];
}

function uniqueSmiles(items: LigandFragmentItem[]): string[] {
  const dedup = new Set<string>();
  const result: string[] = [];
  items.forEach((item) => {
    const smiles = String(item.smiles || '').trim();
    if (!smiles || dedup.has(smiles)) return;
    dedup.add(smiles);
    result.push(smiles);
  });
  return result;
}

function normalizeAtomSet(fragment: LigandFragmentItem): string {
  const atomIndices = Array.isArray(fragment.atom_indices) ? fragment.atom_indices : [];
  const normalized = atomIndices
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value >= 0)
    .map((value) => Math.floor(value))
    .sort((a, b) => a - b);
  return normalized.join(',');
}

export function resolveVariableSelection(
  selectedItems: LigandFragmentItem[],
  allFragments: LigandFragmentItem[]
): VariableSelectionResolution {
  const normalizedSelected = selectedItems.filter((item) => String(item.fragment_id || '').trim());
  if (normalizedSelected.length === 0) {
    return {
      mergedFragment: null,
      effectiveItems: [],
      variableSmilesList: []
    };
  }
  if (normalizedSelected.length === 1) {
    return {
      mergedFragment: null,
      effectiveItems: normalizedSelected,
      variableSmilesList: uniqueSmiles(normalizedSelected)
    };
  }

  // If multi-selected atoms exactly match an existing fragment, treat it as a single replacement site.
  const unionAtoms = new Set<number>();
  normalizedSelected.forEach((fragment) => {
    (Array.isArray(fragment.atom_indices) ? fragment.atom_indices : []).forEach((value) => {
      const atomIndex = Number(value);
      if (Number.isFinite(atomIndex) && atomIndex >= 0) unionAtoms.add(Math.floor(atomIndex));
    });
  });
  if (unionAtoms.size > 0 && Array.isArray(allFragments) && allFragments.length > 0) {
    const unionKey = Array.from(unionAtoms.values())
      .sort((a, b) => a - b)
      .join(',');
    const merged = allFragments.find((fragment) => normalizeAtomSet(fragment) === unionKey) || null;
    if (merged) {
      return {
        mergedFragment: merged,
        effectiveItems: [merged],
        variableSmilesList: uniqueSmiles([merged])
      };
    }
  }

  return {
    mergedFragment: null,
    effectiveItems: normalizedSelected,
    variableSmilesList: uniqueSmiles(normalizedSelected)
  };
}

export function inferQueryModeFromSelection(
  selectedItems: LigandFragmentItem[],
  allFragments: LigandFragmentItem[]
): 'one-to-many' | 'many-to-many' {
  const selection = resolveVariableSelection(selectedItems, allFragments);
  return selection.effectiveItems.length > 1 ? 'many-to-many' : 'one-to-many';
}
