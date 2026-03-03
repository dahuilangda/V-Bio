import type { InputComponent, ProjectInputConfig } from '../../types/models';

export interface ProjectDraftLike {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

export function applyAffinityChainsToDraftState<T extends ProjectDraftLike>(
  prev: T | null,
  targetChainId: string,
  ligandChainId: string,
  forceEnable = false
): T | null {
  if (!prev) return prev;
  const target = String(targetChainId || '').trim() || null;
  const ligand = String(ligandChainId || '').trim() || null;

  const nextAffinity = forceEnable ? true : prev.inputConfig.properties.affinity;
  const same =
    prev.inputConfig.properties.affinity === nextAffinity &&
    prev.inputConfig.properties.target === target &&
    prev.inputConfig.properties.ligand === ligand &&
    prev.inputConfig.properties.binder === ligand;
  if (same) return prev;

  return {
    ...prev,
    inputConfig: {
      ...prev.inputConfig,
      properties: {
        ...prev.inputConfig.properties,
        affinity: nextAffinity,
        target,
        ligand,
        binder: ligand,
      },
    },
  };
}

export function applyUseMsaToProteinComponents<T extends ProjectDraftLike>(
  prev: T | null,
  checked: boolean,
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean
): T | null {
  if (!prev) return prev;

  let changed = false;
  const nextComponents = prev.inputConfig.components.map((component) => {
    if (component.type !== 'protein') return component;
    const current = component.useMsa !== false;
    if (current === checked) return component;
    changed = true;
    return {
      ...component,
      useMsa: checked,
    };
  });

  const nextUseMsa = computeUseMsaFlag(nextComponents, checked);
  if (!changed && prev.use_msa === nextUseMsa) return prev;

  return {
    ...prev,
    use_msa: nextUseMsa,
    inputConfig: {
      ...prev.inputConfig,
      components: nextComponents,
    },
  };
}
