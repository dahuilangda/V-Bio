import { useCallback, useMemo } from 'react';
import type { InputComponent, ProjectInputConfig } from '../../types/models';
import { buildChainInfos } from '../../utils/chainAssignments';
import { componentTypeLabel } from '../../utils/projectInputs';
import { nonEmptyComponents } from './projectDraftUtils';

type ChainInfo = ReturnType<typeof buildChainInfos>[number];
type WorkspaceOption = {
  componentId: string;
  componentIndex: number;
  chainId: string;
  type: InputComponent['type'];
  label: string;
  isSmallMolecule: boolean;
};

export interface UseWorkspaceAffinitySelectionParams {
  normalizedDraftComponents: InputComponent[];
  draftProperties: ProjectInputConfig['properties'] | null | undefined;
}

export interface UseWorkspaceAffinitySelectionResult {
  activeChainInfos: ChainInfo[];
  chainInfoById: Map<string, ChainInfo>;
  ligandChainOptions: ChainInfo[];
  workspaceAffinityOptions: WorkspaceOption[];
  workspaceTargetOptions: WorkspaceOption[];
  workspaceLigandOptions: WorkspaceOption[];
  selectedWorkspaceTarget: { chainId: string | null; componentId: string | null };
  workspaceLigandSelectableOptions: WorkspaceOption[];
  selectedWorkspaceLigand: { chainId: string | null; componentId: string | null };
  selectedWorkspaceLigandOption: WorkspaceOption | null;
  canEnableAffinityFromWorkspace: boolean;
  affinityEnableDisabledReason: string;
}

export function useWorkspaceAffinitySelection(
  params: UseWorkspaceAffinitySelectionParams
): UseWorkspaceAffinitySelectionResult {
  const { normalizedDraftComponents, draftProperties } = params;

  const activeChainInfos = useMemo(() => {
    return buildChainInfos(nonEmptyComponents(normalizedDraftComponents));
  }, [normalizedDraftComponents]);

  const chainInfoById = useMemo(() => {
    const byId = new Map<string, ChainInfo>();
    for (const info of activeChainInfos) {
      byId.set(info.id, info);
    }
    return byId;
  }, [activeChainInfos]);

  const ligandChainOptions = useMemo(() => {
    return activeChainInfos.filter((item) => item.type === 'ligand');
  }, [activeChainInfos]);

  const chainToComponentIdMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const info of activeChainInfos) {
      map.set(info.id, info.componentId);
    }
    return map;
  }, [activeChainInfos]);

  const workspaceAffinityOptions = useMemo(() => {
    const firstChainByComponentId = new Map<string, string>();
    for (const info of activeChainInfos) {
      if (!firstChainByComponentId.has(info.componentId)) {
        firstChainByComponentId.set(info.componentId, info.id);
      }
    }
    return normalizedDraftComponents
      .map((component, index) => {
        const chainId = firstChainByComponentId.get(component.id) || null;
        if (!chainId) return null;
        const isSmallMolecule = component.type === 'ligand' && component.inputMethod !== 'ccd';
        return {
          componentId: component.id,
          componentIndex: index + 1,
          chainId,
          type: component.type,
          label: `Comp ${index + 1} · ${componentTypeLabel(component.type)}`,
          isSmallMolecule
        };
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item));
  }, [activeChainInfos, normalizedDraftComponents]);

  const workspaceTargetOptions = useMemo(() => {
    return workspaceAffinityOptions.filter((item) => item.type !== 'ligand');
  }, [workspaceAffinityOptions]);

  const workspaceLigandOptions = useMemo(() => {
    return workspaceAffinityOptions;
  }, [workspaceAffinityOptions]);

  const resolveChainFromProperty = useCallback(
    (
      rawChainId: string | null | undefined,
      options: Array<{ componentId: string; chainId: string }>
    ): { chainId: string | null; componentId: string | null } => {
      const chainId = String(rawChainId || '').trim();
      if (!chainId) {
        const first = options[0];
        return {
          chainId: first?.chainId || null,
          componentId: first?.componentId || null
        };
      }
      const byChain = options.find((item) => item.chainId === chainId);
      if (byChain) {
        return { chainId: byChain.chainId, componentId: byChain.componentId };
      }
      const componentId = chainToComponentIdMap.get(chainId) || null;
      if (componentId) {
        const byComponent = options.find((item) => item.componentId === componentId);
        if (byComponent) {
          return { chainId: byComponent.chainId, componentId: byComponent.componentId };
        }
      }
      const first = options[0];
      return {
        chainId: first?.chainId || null,
        componentId: first?.componentId || null
      };
    },
    [chainToComponentIdMap]
  );

  const selectedWorkspaceTarget = useMemo(() => {
    return resolveChainFromProperty(draftProperties?.target, workspaceTargetOptions);
  }, [draftProperties?.target, resolveChainFromProperty, workspaceTargetOptions]);

  const workspaceLigandSelectableOptions = useMemo(() => {
    if (!selectedWorkspaceTarget.componentId) return workspaceLigandOptions;
    return workspaceLigandOptions.filter((item) => item.componentId !== selectedWorkspaceTarget.componentId);
  }, [selectedWorkspaceTarget.componentId, workspaceLigandOptions]);

  const selectedWorkspaceLigand = useMemo(() => {
    const rawLigand = String(draftProperties?.ligand || '').trim();
    if (rawLigand) {
      const resolved = resolveChainFromProperty(rawLigand, workspaceLigandOptions);
      if (resolved.componentId && resolved.componentId !== selectedWorkspaceTarget.componentId) {
        return resolved;
      }
      return { chainId: null, componentId: null };
    }
    const optionsWithoutTarget = workspaceLigandSelectableOptions;
    const defaultSmallMolecule =
      optionsWithoutTarget.find((item) => item.isSmallMolecule) ||
      optionsWithoutTarget[0] ||
      null;
    return {
      chainId: defaultSmallMolecule?.chainId || null,
      componentId: defaultSmallMolecule?.componentId || null
    };
  }, [
    draftProperties?.ligand,
    resolveChainFromProperty,
    selectedWorkspaceTarget.componentId,
    workspaceLigandOptions,
    workspaceLigandSelectableOptions
  ]);

  const selectedWorkspaceLigandOption = useMemo(() => {
    if (!selectedWorkspaceLigand.componentId) return null;
    return workspaceLigandOptions.find((item) => item.componentId === selectedWorkspaceLigand.componentId) || null;
  }, [selectedWorkspaceLigand.componentId, workspaceLigandOptions]);

  const canEnableAffinityFromWorkspace = useMemo(() => {
    return Boolean(
      selectedWorkspaceTarget.chainId &&
        selectedWorkspaceLigand.chainId &&
        selectedWorkspaceLigandOption &&
        selectedWorkspaceLigandOption.isSmallMolecule
    );
  }, [selectedWorkspaceLigand.chainId, selectedWorkspaceLigandOption, selectedWorkspaceTarget.chainId]);

  const affinityEnableDisabledReason = useMemo(() => {
    if (!selectedWorkspaceTarget.chainId) return 'Choose a target component first.';
    if (!selectedWorkspaceLigand.chainId) return 'Choose a ligand component first.';
    if (!selectedWorkspaceLigandOption?.isSmallMolecule) return '';
    return '';
  }, [selectedWorkspaceLigand.chainId, selectedWorkspaceLigandOption?.isSmallMolecule, selectedWorkspaceTarget.chainId]);

  return {
    activeChainInfos,
    chainInfoById,
    ligandChainOptions,
    workspaceAffinityOptions,
    workspaceTargetOptions,
    workspaceLigandOptions,
    selectedWorkspaceTarget,
    workspaceLigandSelectableOptions,
    selectedWorkspaceLigand,
    selectedWorkspaceLigandOption,
    canEnableAffinityFromWorkspace,
    affinityEnableDisabledReason,
  };
}
