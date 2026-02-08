import type { InputComponent, MoleculeType } from '../types/models';

const CHAIN_POOL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

export interface ChainInfo {
  id: string;
  type: MoleculeType;
  componentId: string;
  componentIndex: number;
  copyIndex: number;
  residueCount: number;
}

export function chainIdAt(index: number): string {
  if (index < CHAIN_POOL.length) return CHAIN_POOL[index];
  return `CHAIN_${index + 1}`;
}

export function assignChainIdsForComponents(components: InputComponent[]): string[][] {
  let cursor = 0;
  return components.map((comp) => {
    const copies = Math.max(1, Number(comp.numCopies || 1));
    const ids: string[] = [];
    for (let i = 0; i < copies; i += 1) {
      ids.push(chainIdAt(cursor));
      cursor += 1;
    }
    return ids;
  });
}

export function buildChainInfos(components: InputComponent[]): ChainInfo[] {
  const assignments = assignChainIdsForComponents(components);
  const infos: ChainInfo[] = [];

  components.forEach((comp, compIndex) => {
    if (!comp.sequence.trim()) return;
    const chainIds = assignments[compIndex] || [];
    const residueCount = comp.type === 'ligand' ? 1 : comp.sequence.replace(/\s+/g, '').length;
    chainIds.forEach((id, copyIndex) => {
      infos.push({
        id,
        type: comp.type,
        componentId: comp.id,
        componentIndex: compIndex,
        copyIndex,
        residueCount
      });
    });
  });

  return infos;
}
