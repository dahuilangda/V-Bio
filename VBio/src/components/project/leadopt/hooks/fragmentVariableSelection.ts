import type { LigandFragmentItem } from '../../LigandFragmentSketcher';

export interface VariableSelectionResolution {
  mergedFragment: LigandFragmentItem | null;
  effectiveItems: LigandFragmentItem[];
  variableSmilesList: string[];
}

export type LigandAtomBond = [number, number];

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

function normalizeAtomIndices(values: unknown): number[] {
  if (!Array.isArray(values)) return [];
  return Array.from(
    new Set(
      values
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
    )
  ).sort((a, b) => a - b);
}

function normalizeAtomBonds(bonds: LigandAtomBond[] | undefined): LigandAtomBond[] {
  if (!Array.isArray(bonds)) return [];
  const dedup = new Set<string>();
  const result: LigandAtomBond[] = [];
  bonds.forEach((pair) => {
    if (!Array.isArray(pair) || pair.length < 2) return;
    const left = Number(pair[0]);
    const right = Number(pair[1]);
    if (!Number.isFinite(left) || !Number.isFinite(right)) return;
    const a = Math.floor(left);
    const b = Math.floor(right);
    if (a < 0 || b < 0 || a === b) return;
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    const key = `${lo}:${hi}`;
    if (dedup.has(key)) return;
    dedup.add(key);
    result.push([lo, hi]);
  });
  return result;
}

function unionAtomIndices(items: LigandFragmentItem[]): number[] {
  return normalizeAtomIndices(items.flatMap((item) => item.atom_indices || []));
}

function buildSyntheticMergedFragment(items: LigandFragmentItem[], atomIndices: number[]): LigandFragmentItem {
  const normalizedItems = [...items].sort((a, b) => String(a.fragment_id || '').localeCompare(String(b.fragment_id || '')));
  const fragmentId = `merged:${normalizedItems.map((item) => String(item.fragment_id || '').trim()).filter(Boolean).join('+')}`;
  const anchor = normalizedItems[0] || null;
  const plainDisplayQuery = String(anchor?.display_smiles || '').trim();
  return {
    fragment_id: fragmentId || `merged:${atomIndices.join(',')}`,
    // Use non-attachment query text here; backend must derive true attachment query from atom_indices.
    smiles: plainDisplayQuery,
    display_smiles: plainDisplayQuery,
    atom_indices: atomIndices,
    heavy_atoms: atomIndices.length,
    attachment_count: undefined,
    num_frags: normalizedItems.length,
    recommended_action: 'variable',
    color: '#5f6f87',
    rule_coverage: Number(anchor?.rule_coverage || 0),
    quality_score: Number(anchor?.quality_score || 0)
  };
}

function connect(adjacency: Map<string, Set<string>>, left: string, right: string): void {
  if (!left || !right || left === right) return;
  if (!adjacency.has(left)) adjacency.set(left, new Set<string>());
  if (!adjacency.has(right)) adjacency.set(right, new Set<string>());
  adjacency.get(left)?.add(right);
  adjacency.get(right)?.add(left);
}

function buildConnectedComponents(
  selectedItems: LigandFragmentItem[],
  atomBonds: LigandAtomBond[] | undefined
): LigandFragmentItem[][] {
  const items = selectedItems.filter((item) => String(item.fragment_id || '').trim());
  if (items.length <= 1) return items.length === 0 ? [] : [items];
  const adjacency = new Map<string, Set<string>>();
  const idToItem = new Map<string, LigandFragmentItem>();
  const idToAtoms = new Map<string, Set<number>>();
  const atomToFragmentIds = new Map<number, Set<string>>();

  items.forEach((item) => {
    const fragmentId = String(item.fragment_id || '').trim();
    if (!fragmentId) return;
    const atomSet = new Set(normalizeAtomIndices(item.atom_indices || []));
    idToItem.set(fragmentId, item);
    idToAtoms.set(fragmentId, atomSet);
    if (!adjacency.has(fragmentId)) adjacency.set(fragmentId, new Set<string>());
    atomSet.forEach((atomIndex) => {
      if (!atomToFragmentIds.has(atomIndex)) atomToFragmentIds.set(atomIndex, new Set<string>());
      atomToFragmentIds.get(atomIndex)?.add(fragmentId);
    });
  });

  const fragmentIds = Array.from(idToItem.keys());
  for (let i = 0; i < fragmentIds.length; i += 1) {
    const leftId = fragmentIds[i];
    const leftAtoms = idToAtoms.get(leftId) || new Set<number>();
    for (let j = i + 1; j < fragmentIds.length; j += 1) {
      const rightId = fragmentIds[j];
      const rightAtoms = idToAtoms.get(rightId) || new Set<number>();
      const [small, large] = leftAtoms.size <= rightAtoms.size ? [leftAtoms, rightAtoms] : [rightAtoms, leftAtoms];
      for (const atomIndex of small) {
        if (!large.has(atomIndex)) continue;
        connect(adjacency, leftId, rightId);
        break;
      }
    }
  }

  const normalizedBonds = normalizeAtomBonds(atomBonds);
  normalizedBonds.forEach(([leftAtom, rightAtom]) => {
    const leftFragments = atomToFragmentIds.get(leftAtom);
    const rightFragments = atomToFragmentIds.get(rightAtom);
    if (!leftFragments || !rightFragments) return;
    leftFragments.forEach((leftId) => {
      rightFragments.forEach((rightId) => connect(adjacency, leftId, rightId));
    });
  });

  const visited = new Set<string>();
  const components: LigandFragmentItem[][] = [];
  fragmentIds.forEach((rootId) => {
    if (visited.has(rootId)) return;
    const stack = [rootId];
    const componentIds: string[] = [];
    while (stack.length > 0) {
      const next = stack.pop();
      if (!next || visited.has(next)) continue;
      visited.add(next);
      componentIds.push(next);
      (adjacency.get(next) || new Set<string>()).forEach((neighbor) => {
        if (!visited.has(neighbor)) stack.push(neighbor);
      });
    }
    const componentItems = componentIds
      .map((id) => idToItem.get(id))
      .filter((item): item is LigandFragmentItem => Boolean(item));
    if (componentItems.length > 0) components.push(componentItems);
  });
  return components;
}

export function resolveVariableSelection(
  selectedItems: LigandFragmentItem[],
  allFragments: LigandFragmentItem[],
  atomBonds?: LigandAtomBond[]
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

  const components = buildConnectedComponents(normalizedSelected, atomBonds);
  const effectiveItems = components.map((component) => {
    if (component.length === 1) return component[0];
    const unionAtoms = unionAtomIndices(component);
    const unionKey = unionAtoms.join(',');
    const merged =
      unionKey && Array.isArray(allFragments) && allFragments.length > 0
        ? allFragments.find((fragment) => normalizeAtomSet(fragment) === unionKey) || null
        : null;
    if (merged) return merged;
    return buildSyntheticMergedFragment(component, unionAtoms);
  });

  const mergedFragment = effectiveItems.length === 1 && components.some((component) => component.length > 1)
    ? effectiveItems[0]
    : null;

  return {
    mergedFragment,
    effectiveItems,
    variableSmilesList: uniqueSmiles(normalizedSelected)
  };
}

export function inferQueryModeFromSelection(
  selectedItems: LigandFragmentItem[],
  allFragments: LigandFragmentItem[],
  atomBonds?: LigandAtomBond[]
): 'one-to-many' | 'many-to-many' {
  const selection = resolveVariableSelection(selectedItems, allFragments, atomBonds);
  return selection.effectiveItems.length > 1 ? 'many-to-many' : 'one-to-many';
}
