export function readIndexedValue(source: any, index: number): any {
  if (source == null || !Number.isFinite(index) || index < 0) return undefined;
  if (typeof source.value === 'function') {
    try {
      return source.value(index);
    } catch {
      // no-op
    }
  }
  if (typeof source.get === 'function') {
    try {
      return source.get(index);
    } catch {
      // no-op
    }
  }
  if (Array.isArray(source) || ArrayBuffer.isView(source)) {
    return (source as any)[index];
  }
  if (typeof source === 'object') {
    if (Object.prototype.hasOwnProperty.call(source, index)) {
      return source[index];
    }
    if ('data' in source) {
      const nested = readIndexedValue((source as Record<string, unknown>).data, index);
      if (nested !== undefined) return nested;
    }
    if ('array' in source) {
      const nested = readIndexedValue((source as Record<string, unknown>).array, index);
      if (nested !== undefined) return nested;
    }
  }
  return undefined;
}

export function readFirstIndex(indices: any): number | null {
  if (!indices) return null;
  const molstarLib = (window as any)?.molstar?.lib;
  const orderedSet =
    molstarLib?.molDataInt?.OrderedSet ??
    molstarLib?.molData?.int?.OrderedSet ??
    molstarLib?.molDataIntOrderedSet;

  if (orderedSet) {
    try {
      const hasSize = typeof orderedSet.size === 'function';
      const size = hasSize ? Number(orderedSet.size(indices)) : NaN;
      if (Number.isFinite(size) && size > 0) {
        if (typeof orderedSet.getAt === 'function') {
          const value = Number(orderedSet.getAt(indices, 0));
          if (Number.isFinite(value)) return value;
        }
        if (typeof orderedSet.min === 'function') {
          const value = Number(orderedSet.min(indices));
          if (Number.isFinite(value)) return value;
        }
      }
    } catch {
      // fallback to generic index readers below
    }
  }

  if (Array.isArray(indices) && indices.length > 0 && Number.isFinite(indices[0])) {
    return Number(indices[0]);
  }
  if (typeof indices.getAt === 'function') {
    try {
      const value = Number(indices.getAt(0));
      if (Number.isFinite(value)) return value;
    } catch {
      // no-op
    }
  }
  if (typeof indices.value === 'function') {
    try {
      const value = Number(indices.value(0));
      if (Number.isFinite(value)) return value;
    } catch {
      // no-op
    }
  }
  if (typeof indices[Symbol.iterator] === 'function') {
    const iter = indices[Symbol.iterator]();
    const first = iter.next();
    if (!first.done && Number.isFinite(first.value)) {
      return Number(first.value);
    }
  }
  if (typeof indices.start === 'number' && Number.isFinite(indices.start) && indices.start >= 0) {
    return Number(indices.start);
  }
  if (typeof indices.min === 'number' && Number.isFinite(indices.min) && indices.min >= 0) {
    return Number(indices.min);
  }
  return null;
}

function createOrderedSet(indices: number[]): any {
  const uniqueSorted = Array.from(new Set(indices.filter((value) => Number.isFinite(value) && value >= 0))).sort(
    (a, b) => a - b
  );
  if (!uniqueSorted.length) return null;

  const molstarLib = (window as any)?.molstar?.lib;
  const orderedSet =
    molstarLib?.molDataInt?.OrderedSet ??
    molstarLib?.molData?.int?.OrderedSet ??
    molstarLib?.molDataIntOrderedSet;

  try {
    if (orderedSet?.ofSortedArray) {
      return orderedSet.ofSortedArray(Int32Array.from(uniqueSorted));
    }
    if (orderedSet?.ofSingleton && uniqueSorted.length === 1) {
      return orderedSet.ofSingleton(uniqueSorted[0]);
    }
  } catch {
    // fallback below
  }

  return Int32Array.from(uniqueSorted);
}

function getStructureDataCandidates(viewer: any): any[] {
  const structures = viewer?.plugin?.managers?.structure?.hierarchy?.current?.structures;
  if (!Array.isArray(structures)) return [];

  const result: any[] = [];
  for (const entry of structures) {
    const structure = entry?.cell?.obj?.data ?? entry?.obj?.data ?? entry?.data ?? entry?.structure ?? null;
    if (structure) result.push(structure);
  }
  return result;
}

function buildResidueLociForStructure(structure: any, chainId: string, residue: number): any | null {
  if (!structure || !Array.isArray(structure.units)) return null;
  const elements: Array<{ unit: any; indices: any }> = [];

  for (const unit of structure.units) {
    const hierarchy = unit?.model?.atomicHierarchy;
    const unitElements = unit?.elements;
    if (!hierarchy || !unitElements) continue;

    const matches: number[] = [];
    const unitLength =
      typeof unitElements.length === 'number' && unitElements.length > 0
        ? unitElements.length
        : Math.max(0, Number(readFirstIndex(unitElements)) + 1);
    for (let indexInUnit = 0; indexInUnit < unitLength; indexInUnit += 1) {
      const atomIndex = Number(readIndexedValue(unitElements, indexInUnit) ?? indexInUnit);
      if (!Number.isFinite(atomIndex) || atomIndex < 0) continue;

      const residueIndex = Number(readIndexedValue(hierarchy?.residueAtomSegments?.index, atomIndex));
      const chainIndex = Number(readIndexedValue(hierarchy?.chainAtomSegments?.index, atomIndex));

      const chain =
        String(
          readIndexedValue(hierarchy?.chains?.auth_asym_id, chainIndex) ??
            readIndexedValue(hierarchy?.chains?.label_asym_id, chainIndex) ??
            ''
        ).trim() || null;
      if (!chain || chain !== chainId) continue;

      const seq = Number.parseInt(
        String(
          readIndexedValue(hierarchy?.residues?.auth_seq_id, residueIndex) ??
            readIndexedValue(hierarchy?.residues?.label_seq_id, residueIndex) ??
            readIndexedValue(hierarchy?.residues?.seq_id, residueIndex) ??
            ''
        ),
        10
      );
      if (!Number.isFinite(seq) || seq !== residue) continue;
      matches.push(indexInUnit);
    }

    if (!matches.length) continue;
    const indices = createOrderedSet(matches);
    if (!indices) continue;
    elements.push({ unit, indices });
  }

  if (!elements.length) return null;
  return {
    kind: 'element-loci',
    structure,
    elements
  };
}

export function buildResidueLoci(viewer: any, chainId: string, residue: number): any | null {
  const structures = getStructureDataCandidates(viewer);
  for (const structure of structures) {
    const loci = buildResidueLociForStructure(structure, chainId, residue);
    if (loci) return loci;
  }
  return null;
}

function normalizeAtomName(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, '').trim().toUpperCase();
}

function buildAtomLociForStructure(
  structure: any,
  chainId: string,
  residue: number,
  atomName: string,
  atomIndex?: number
): any | null {
  if (!structure || !Array.isArray(structure.units)) return null;
  const normalizedAtomName = normalizeAtomName(atomName);
  const expectedAtomIndex =
    Number.isFinite(atomIndex) && Number(atomIndex) >= 0 ? Math.floor(Number(atomIndex)) : null;
  if (!normalizedAtomName && expectedAtomIndex === null) return null;

  const elements: Array<{ unit: any; indices: any }> = [];
  for (const unit of structure.units) {
    const hierarchy = unit?.model?.atomicHierarchy;
    const unitElements = unit?.elements;
    if (!hierarchy || !unitElements) continue;

    const matches: number[] = [];
    let residueHeavyAtomOrdinal = -1;
    const unitLength =
      typeof unitElements.length === 'number' && unitElements.length > 0
        ? unitElements.length
        : Math.max(0, Number(readFirstIndex(unitElements)) + 1);

    for (let indexInUnit = 0; indexInUnit < unitLength; indexInUnit += 1) {
      const atomIndex = Number(readIndexedValue(unitElements, indexInUnit) ?? indexInUnit);
      if (!Number.isFinite(atomIndex) || atomIndex < 0) continue;

      const residueIndex = Number(readIndexedValue(hierarchy?.residueAtomSegments?.index, atomIndex));
      const chainIndex = Number(readIndexedValue(hierarchy?.chainAtomSegments?.index, atomIndex));

      const chain =
        String(
          readIndexedValue(hierarchy?.chains?.auth_asym_id, chainIndex) ??
            readIndexedValue(hierarchy?.chains?.label_asym_id, chainIndex) ??
            ''
        ).trim() || null;
      if (!chain || chain !== chainId) continue;

      const seq = Number.parseInt(
        String(
          readIndexedValue(hierarchy?.residues?.auth_seq_id, residueIndex) ??
            readIndexedValue(hierarchy?.residues?.label_seq_id, residueIndex) ??
            readIndexedValue(hierarchy?.residues?.seq_id, residueIndex) ??
            ''
        ),
        10
      );
      if (!Number.isFinite(seq) || seq !== residue) continue;

      const atomSymbol = String(readIndexedValue(hierarchy?.atoms?.type_symbol, atomIndex) ?? '')
        .trim()
        .toUpperCase();
      const isHydrogen = atomSymbol === 'H';
      if (!isHydrogen) residueHeavyAtomOrdinal += 1;

      const atomToken = normalizeAtomName(
        readIndexedValue(hierarchy?.atoms?.auth_atom_id, atomIndex) ??
          readIndexedValue(hierarchy?.atoms?.label_atom_id, atomIndex)
      );
      const matchedByIndex = expectedAtomIndex !== null && !isHydrogen && residueHeavyAtomOrdinal === expectedAtomIndex;
      const matchedByName = expectedAtomIndex === null && normalizedAtomName ? atomToken === normalizedAtomName : false;
      if (!matchedByIndex && !matchedByName) continue;
      matches.push(indexInUnit);
    }

    if (!matches.length) continue;
    const indices = createOrderedSet(matches);
    if (!indices) continue;
    elements.push({ unit, indices });
  }

  if (!elements.length) return null;
  return {
    kind: 'element-loci',
    structure,
    elements
  };
}

export function buildAtomLoci(viewer: any, chainId: string, residue: number, atomName: string, atomIndex?: number): any | null {
  const structures = getStructureDataCandidates(viewer);
  for (const structure of structures) {
    const loci = buildAtomLociForStructure(structure, chainId, residue, atomName, atomIndex);
    if (loci) return loci;
  }
  return null;
}
