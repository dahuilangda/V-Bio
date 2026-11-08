import type { InputComponent, ProjectInputConfig } from '../types/models';
import { assignChainIdsForComponents } from './chainAssignments';

export interface LeadOptimizationChainContext {
  targetChain: string;
  ligandChain: string;
}

export function deriveLeadOptimizationChainContext(components: InputComponent[]): LeadOptimizationChainContext {
  const assignments = assignChainIdsForComponents(components);
  let target = 'A';
  let ligand = 'L';
  for (let index = 0; index < components.length; index += 1) {
    const firstChain = assignments[index]?.[0] || '';
    if (!firstChain) continue;
    if (components[index].type === 'ligand' && ligand === 'L') {
      ligand = firstChain;
    }
    if (components[index].type !== 'ligand' && target === 'A') {
      target = firstChain;
    }
  }
  return { targetChain: target, ligandChain: ligand };
}

export function withLeadOptimizationLigandSmiles(
  inputConfig: ProjectInputConfig,
  ligandSmiles: string
): ProjectInputConfig {
  let hasLigand = false;
  const nextComponents = inputConfig.components.map((component) => {
    if (component.type !== 'ligand') return component;
    hasLigand = true;
    return {
      ...component,
      sequence: ligandSmiles
    };
  });
  if (!hasLigand) {
    const existingIds = new Set(nextComponents.map((component) => component.id));
    let autoId = 'leadopt_ligand_auto';
    let cursor = 2;
    while (existingIds.has(autoId)) {
      autoId = `leadopt_ligand_auto_${cursor}`;
      cursor += 1;
    }
    nextComponents.push({
      id: autoId,
      type: 'ligand',
      numCopies: 1,
      sequence: ligandSmiles,
      inputMethod: 'smiles'
    });
  }
  return {
    ...inputConfig,
    components: nextComponents
  };
}

/** lead-opt 预测记录的 backend 词表：别名归一 + 引擎白名单（与配置矩阵保持一致）。 */
const LEAD_OPT_BACKEND_ALIASES: Record<string, string> = {
  boltz2: 'boltz',
  nesso1: 'nesso',
  'nesso-1': 'nesso'
};

const LEAD_OPT_BACKEND_IDS = new Set([
  'boltz',
  'alphafold3',
  'protenix',
  'nesso',
  'pocketxmol',
  'protenix2dock',
  'mol1',
  'mol1dock',
  'rfd4'
]);

export function normalizeLeadOptPredictionBackend(value: unknown): string {
  const token = String(value || '').trim().toLowerCase();
  const aliased = LEAD_OPT_BACKEND_ALIASES[token];
  if (aliased) return aliased;
  return LEAD_OPT_BACKEND_IDS.has(token) ? token : '';
}
