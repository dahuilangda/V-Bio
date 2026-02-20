import type { Dispatch, SetStateAction } from 'react';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { InputComponent, PredictionConstraint } from '../../types/models';
import type { ProteinTemplateUpload } from '../../types/models';

interface DraftLike {
  inputConfig: {
    version: number;
    components: InputComponent[];
    constraints: PredictionConstraint[];
    properties: {
      affinity: boolean;
      target?: string | null;
      ligand?: string | null;
      binder?: string | null;
    };
  };
}

export function scrollToEditorBlock(elementId: string): void {
  window.setTimeout(() => {
    const target = document.getElementById(elementId);
    if (!target) return;
    const targetTop = target.getBoundingClientRect().top + window.scrollY - 132;
    window.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
  }, 40);
}

export function jumpToComponentAction(params: {
  componentId: string;
  setWorkspaceTab: Dispatch<SetStateAction<'results' | 'basics' | 'components' | 'constraints'>>;
  setActiveComponentId: Dispatch<SetStateAction<string | null>>;
  normalizedDraftComponents: InputComponent[];
  setSidebarTypeOpen: Dispatch<SetStateAction<Record<InputComponent['type'], boolean>>>;
}): void {
  const { componentId, setWorkspaceTab, setActiveComponentId, normalizedDraftComponents, setSidebarTypeOpen } = params;
  setWorkspaceTab('components');
  setActiveComponentId(componentId);
  const targetType = normalizedDraftComponents.find((item) => item.id === componentId)?.type;
  if (targetType) {
    setSidebarTypeOpen((prev) => ({ ...prev, [targetType]: true }));
  }
  scrollToEditorBlock(`component-card-${componentId}`);
}

export function addComponentToDraftAction<TDraft extends DraftLike>(params: {
  type: InputComponent['type'];
  createInputComponent: (type: InputComponent['type']) => InputComponent;
  setWorkspaceTab: Dispatch<SetStateAction<'results' | 'basics' | 'components' | 'constraints'>>;
  setActiveComponentId: Dispatch<SetStateAction<string | null>>;
  setSidebarTypeOpen: Dispatch<SetStateAction<Record<InputComponent['type'], boolean>>>;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { type, createInputComponent, setWorkspaceTab, setActiveComponentId, setSidebarTypeOpen, setDraft } = params;
  const nextComponent = createInputComponent(type);
  setWorkspaceTab('components');
  setActiveComponentId(nextComponent.id);
  setSidebarTypeOpen((prev) => ({ ...prev, [type]: true }));
  setDraft((d) =>
    d
      ? {
          ...d,
          inputConfig: {
            ...d.inputConfig,
            version: 1,
            components: [...d.inputConfig.components, nextComponent],
          },
        }
      : d
  );
}

export function addConstraintFromSidebarAction<TDraft extends DraftLike>(params: {
  draft: TDraft | null;
  buildDefaultConstraint: (preferredType?: 'contact' | 'bond' | 'pocket') => PredictionConstraint;
  isBondOnlyBackend: boolean;
  setWorkspaceTab: Dispatch<SetStateAction<'results' | 'basics' | 'components' | 'constraints'>>;
  setSidebarConstraintsOpen: Dispatch<SetStateAction<boolean>>;
  setActiveConstraintId: Dispatch<SetStateAction<string | null>>;
  setSelectedContactConstraintIds: Dispatch<SetStateAction<string[]>>;
  constraintSelectionAnchorRef: { current: string | null };
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const {
    draft,
    buildDefaultConstraint,
    isBondOnlyBackend,
    setWorkspaceTab,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    setDraft,
  } = params;
  if (!draft) return;
  const next = buildDefaultConstraint(isBondOnlyBackend ? 'bond' : undefined);
  setWorkspaceTab('constraints');
  setSidebarConstraintsOpen(true);
  setActiveConstraintId(next.id);
  if (next.type === 'contact') {
    setSelectedContactConstraintIds([next.id]);
    constraintSelectionAnchorRef.current = next.id;
  } else {
    setSelectedContactConstraintIds([]);
    constraintSelectionAnchorRef.current = null;
  }
  setDraft((prev) =>
    prev
      ? {
          ...prev,
          inputConfig: {
            ...prev.inputConfig,
            constraints: [...prev.inputConfig.constraints, next],
          },
        }
      : prev
  );
}

export function setAffinityEnabledFromWorkspaceAction<TDraft extends DraftLike>(params: {
  enabled: boolean;
  canEnableAffinityFromWorkspace: boolean;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { enabled, canEnableAffinityFromWorkspace, setDraft } = params;
  if (enabled && !canEnableAffinityFromWorkspace) return;
  setDraft((prev) =>
    prev
      ? {
          ...prev,
          inputConfig: {
            ...prev.inputConfig,
            properties: {
              ...prev.inputConfig.properties,
              affinity: enabled,
            },
          },
        }
      : prev
  );
}

export function setAffinityComponentFromWorkspaceAction<TDraft extends DraftLike>(params: {
  field: 'target' | 'ligand';
  componentId: string | null;
  workspaceTargetOptions: Array<{ componentId: string; chainId: string }>;
  workspaceLigandSelectableOptions: Array<{ componentId: string; chainId: string }>;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { field, componentId, workspaceTargetOptions, workspaceLigandSelectableOptions, setDraft } = params;
  const optionSource = field === 'target' ? workspaceTargetOptions : workspaceLigandSelectableOptions;
  const nextOption = optionSource.find((item) => item.componentId === componentId) || null;
  const nextChain = nextOption?.chainId || null;
  setDraft((prev) =>
    prev
      ? {
          ...prev,
          inputConfig: {
            ...prev.inputConfig,
            properties: {
              ...prev.inputConfig.properties,
              [field]: nextChain,
              binder: field === 'ligand' ? nextChain : prev.inputConfig.properties.binder,
            },
          },
        }
      : prev
  );
}

export function handleRuntimeBackendChangeAction<TDraft extends DraftLike>(params: {
  backend: string;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  filterConstraintsByBackend: (constraints: PredictionConstraint[], backend: string) => PredictionConstraint[];
}): void {
  const { backend, setDraft, filterConstraintsByBackend } = params;
  setDraft((d) =>
    d
      ? {
          ...d,
          backend,
          inputConfig: {
            ...d.inputConfig,
            constraints: filterConstraintsByBackend(d.inputConfig.constraints, backend),
          },
        }
      : d
  );
}

export function handleRuntimeSeedChangeAction<TDraft extends DraftLike>(params: {
  seed: number | null;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { seed, setDraft } = params;
  setDraft((d) =>
    d
      ? {
          ...d,
          inputConfig: {
            ...d.inputConfig,
            options: {
              ...(d.inputConfig as any).options,
              seed,
            },
          },
        }
      : d
  );
}

export function handleTaskNameChangeAction<TDraft extends { taskName: string }>(params: {
  value: string;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { value, setDraft } = params;
  setDraft((d) => (d ? { ...d, taskName: value } : d));
}

export function handleTaskSummaryChangeAction<TDraft extends { taskSummary: string }>(params: {
  value: string;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { value, setDraft } = params;
  setDraft((d) => (d ? { ...d, taskSummary: value } : d));
}

export function handleOpenTaskHistoryAction(params: {
  event: { preventDefault: () => void };
  taskHistoryPath: string;
  setRunRedirectTaskId: Dispatch<SetStateAction<string | null>>;
  navigate: (to: string) => void;
}): void {
  const { event, taskHistoryPath, setRunRedirectTaskId, navigate } = params;
  event.preventDefault();
  setRunRedirectTaskId(null);
  if (window.location.pathname === taskHistoryPath) return;
  navigate(taskHistoryPath);
  window.setTimeout(() => {
    if (window.location.pathname !== taskHistoryPath) {
      window.location.assign(taskHistoryPath);
    }
  }, 120);
}

export function handlePredictionComponentsChangeAction<TDraft extends DraftLike>(params: {
  components: InputComponent[];
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { components, setDraft } = params;
  setDraft((d) =>
    d
      ? {
          ...d,
          inputConfig: {
            ...d.inputConfig,
            version: 1,
            components
          }
        }
      : d
  );
}

export function handlePredictionProteinTemplateChangeAction(params: {
  componentId: string;
  upload: ProteinTemplateUpload | null;
  setPickedResidue: Dispatch<
    SetStateAction<{
      chainId: string;
      residue: number;
      atomName?: string;
    } | null>
  >;
  setProteinTemplates: Dispatch<SetStateAction<Record<string, ProteinTemplateUpload>>>;
}): void {
  const { componentId, upload, setPickedResidue, setProteinTemplates } = params;
  setPickedResidue(null);
  setProteinTemplates((prev) => {
    const next = { ...prev };
    if (upload) {
      next[componentId] = upload;
    } else {
      delete next[componentId];
    }
    return next;
  });
}

export function handlePredictionTemplateResiduePickAction(params: {
  pick: MolstarResiduePick;
  setPickedResidue: Dispatch<
    SetStateAction<{
      chainId: string;
      residue: number;
      atomName?: string;
    } | null>
  >;
}): void {
  const { pick, setPickedResidue } = params;
  setPickedResidue({
    chainId: pick.chainId,
    residue: pick.residue,
    atomName: pick.atomName
  });
}

export function handleLeadOptimizationLigandSmilesChangeAction<TDraft extends { inputConfig: DraftLike['inputConfig'] }>(params: {
  value: string;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  withLeadOptimizationLigandSmiles: (
    inputConfig: TDraft['inputConfig'],
    value: string
  ) => TDraft['inputConfig'];
}): void {
  const { value, setDraft, withLeadOptimizationLigandSmiles } = params;
  setDraft((d) => {
    if (!d) return d;
    return {
      ...d,
      inputConfig: withLeadOptimizationLigandSmiles(d.inputConfig, value)
    };
  });
}
