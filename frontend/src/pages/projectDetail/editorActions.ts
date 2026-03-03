import type { Dispatch, SetStateAction } from 'react';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import type { InputComponent, PredictionConstraint } from '../../types/models';
import type { ProteinTemplateUpload } from '../../types/models';
import { PEPTIDE_DESIGNED_LIGAND_TOKEN } from '../../utils/projectInputs';

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
    options?: {
      seed?: number | null;
      peptideDesignMode?: 'linear' | 'cyclic' | 'bicyclic';
      peptideBinderLength?: number;
      peptideUseInitialSequence?: boolean;
      peptideInitialSequence?: string;
      peptideSequenceMask?: string;
      peptideIterations?: number;
      peptidePopulationSize?: number;
      peptideEliteSize?: number;
      peptideMutationRate?: number;
      peptideBicyclicLinkerCcd?: 'SEZ' | '29N' | 'BS3';
      peptideBicyclicCysPositionMode?: 'auto' | 'manual';
      peptideBicyclicFixTerminalCys?: boolean;
      peptideBicyclicIncludeExtraCys?: boolean;
      peptideBicyclicCys1Pos?: number;
      peptideBicyclicCys2Pos?: number;
      peptideBicyclicCys3Pos?: number;
    };
  };
}

function patchDraftOptions<TDraft extends DraftLike>(
  setDraft: Dispatch<SetStateAction<TDraft | null>>,
  patch: (options: NonNullable<DraftLike['inputConfig']['options']>) => NonNullable<DraftLike['inputConfig']['options']>
): void {
  setDraft((d) =>
    d
      ? {
          ...d,
          inputConfig: {
            ...d.inputConfig,
            options: patch(((d.inputConfig as any).options || {}) as NonNullable<DraftLike['inputConfig']['options']>)
          }
        }
      : d
  );
}

function clampInteger(value: number, minValue: number, maxValue: number, fallback: number): number {
  return Math.max(minValue, Math.min(maxValue, Math.floor(Number(value) || fallback)));
}

function normalizeBicyclicPositions(options: NonNullable<DraftLike['inputConfig']['options']>): Pick<
  NonNullable<DraftLike['inputConfig']['options']>,
  'peptideBicyclicCys1Pos' | 'peptideBicyclicCys2Pos' | 'peptideBicyclicCys3Pos'
> {
  const binderLength = Math.max(8, Math.floor(Number(options.peptideBinderLength) || 15));
  const fixTerminal = options.peptideBicyclicFixTerminalCys !== false;
  const cys1Pos = clampInteger(Number(options.peptideBicyclicCys1Pos), 1, Math.max(1, binderLength - 2), 3);
  const cys2Upper = fixTerminal ? Math.max(1, binderLength - 2) : Math.max(1, binderLength - 1);
  const cys2Pos = clampInteger(Number(options.peptideBicyclicCys2Pos), 1, cys2Upper, Math.floor(binderLength / 2));
  const cys3Pos = fixTerminal
    ? binderLength
    : clampInteger(Number(options.peptideBicyclicCys3Pos), 1, binderLength, binderLength);

  return {
    peptideBicyclicCys1Pos: cys1Pos,
    peptideBicyclicCys2Pos: cys2Pos,
    peptideBicyclicCys3Pos: cys3Pos
  };
}

const PEPTIDE_INITIAL_SEQUENCE_CHARS = new Set([
  'A',
  'R',
  'N',
  'D',
  'C',
  'Q',
  'E',
  'G',
  'H',
  'I',
  'L',
  'K',
  'M',
  'F',
  'P',
  'S',
  'T',
  'W',
  'Y',
  'V'
]);

const PEPTIDE_MASK_CHARS = new Set([...Array.from(PEPTIDE_INITIAL_SEQUENCE_CHARS), 'X']);

function normalizePeptideInitialSequence(value: unknown, binderLength: number): string {
  if (typeof value !== 'string') return '';
  return value
    .replace(/[\s_-]/g, '')
    .toUpperCase()
    .split('')
    .filter((char) => PEPTIDE_INITIAL_SEQUENCE_CHARS.has(char))
    .join('')
    .slice(0, Math.max(0, binderLength));
}

function normalizePeptideMask(value: unknown, binderLength: number): string {
  const cleaned =
    typeof value === 'string'
      ? value
          .replace(/[\s_-]/g, '')
          .toUpperCase()
          .split('')
          .filter((char) => PEPTIDE_MASK_CHARS.has(char))
          .join('')
      : '';
  if (binderLength <= 0) return '';
  if (!cleaned) return 'X'.repeat(binderLength);
  return cleaned.slice(0, binderLength).padEnd(binderLength, 'X');
}

function normalizePeptideInitializationOptions(
  options: NonNullable<DraftLike['inputConfig']['options']>
): Pick<NonNullable<DraftLike['inputConfig']['options']>, 'peptideUseInitialSequence' | 'peptideInitialSequence' | 'peptideSequenceMask'> {
  const binderLength = Math.max(5, Math.floor(Number(options.peptideBinderLength) || 20));
  return {
    peptideUseInitialSequence: options.peptideUseInitialSequence === true,
    peptideInitialSequence: normalizePeptideInitialSequence(options.peptideInitialSequence, binderLength),
    peptideSequenceMask: normalizePeptideMask(options.peptideSequenceMask, binderLength)
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
  const normalizedComponentId = String(componentId || '').trim();
  const isDesignedPeptideLigand = field === 'ligand' && normalizedComponentId === PEPTIDE_DESIGNED_LIGAND_TOKEN;
  const nextOption = optionSource.find((item) => item.componentId === normalizedComponentId) || null;
  const nextChain = isDesignedPeptideLigand ? PEPTIDE_DESIGNED_LIGAND_TOKEN : nextOption?.chainId || null;
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
  patchDraftOptions(setDraft, (options) => ({
    ...options,
    seed
  }));
}

export function handleRuntimePeptideDesignModeChangeAction<TDraft extends DraftLike>(params: {
  peptideDesignMode: 'linear' | 'cyclic' | 'bicyclic';
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideDesignMode, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const currentLength = Number(options.peptideBinderLength);
    const minLength = peptideDesignMode === 'bicyclic' ? 8 : 5;
    const fallbackLength = peptideDesignMode === 'bicyclic' ? 15 : 20;
    const peptideBinderLength = Number.isFinite(currentLength)
      ? Math.max(minLength, Math.floor(currentLength))
      : fallbackLength;
    const nextOptions = {
      ...options,
      peptideDesignMode,
      peptideBinderLength,
      peptideUseInitialSequence: options.peptideUseInitialSequence === true,
      peptideInitialSequence: options.peptideInitialSequence || '',
      peptideSequenceMask: options.peptideSequenceMask || '',
      peptideBicyclicLinkerCcd: options.peptideBicyclicLinkerCcd || 'SEZ',
      peptideBicyclicCysPositionMode: options.peptideBicyclicCysPositionMode || 'auto',
      peptideBicyclicFixTerminalCys: options.peptideBicyclicFixTerminalCys !== false,
      peptideBicyclicIncludeExtraCys: options.peptideBicyclicIncludeExtraCys === true
    };
    return {
      ...nextOptions,
      ...normalizePeptideInitializationOptions(nextOptions),
      ...normalizeBicyclicPositions(nextOptions)
    };
  });
}

export function handleRuntimePeptideBinderLengthChangeAction<TDraft extends DraftLike>(params: {
  peptideBinderLength: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBinderLength, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const mode = options.peptideDesignMode === 'bicyclic' ? 'bicyclic' : 'other';
    const minLength = mode === 'bicyclic' ? 8 : 5;
    const nextOptions = {
      ...options,
      peptideBinderLength: Math.max(minLength, Math.min(80, Math.floor(Number(peptideBinderLength) || 20)))
    };
    return {
      ...nextOptions,
      ...normalizePeptideInitializationOptions(nextOptions),
      ...normalizeBicyclicPositions(nextOptions)
    };
  });
}

export function handleRuntimePeptideUseInitialSequenceChangeAction<TDraft extends DraftLike>(params: {
  peptideUseInitialSequence: boolean;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideUseInitialSequence, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideUseInitialSequence
    };
    return {
      ...nextOptions,
      ...normalizePeptideInitializationOptions(nextOptions)
    };
  });
}

export function handleRuntimePeptideInitialSequenceChangeAction<TDraft extends DraftLike>(params: {
  peptideInitialSequence: string;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideInitialSequence, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideInitialSequence
    };
    return {
      ...nextOptions,
      ...normalizePeptideInitializationOptions(nextOptions)
    };
  });
}

export function handleRuntimePeptideSequenceMaskChangeAction<TDraft extends DraftLike>(params: {
  peptideSequenceMask: string;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideSequenceMask, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideSequenceMask
    };
    return {
      ...nextOptions,
      ...normalizePeptideInitializationOptions(nextOptions)
    };
  });
}

export function handleRuntimePeptideIterationsChangeAction<TDraft extends DraftLike>(params: {
  peptideIterations: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideIterations, setDraft } = params;
  patchDraftOptions(setDraft, (options) => ({
    ...options,
    peptideIterations: Math.max(2, Math.min(100, Math.floor(Number(peptideIterations) || 12)))
  }));
}

export function handleRuntimePeptidePopulationSizeChangeAction<TDraft extends DraftLike>(params: {
  peptidePopulationSize: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptidePopulationSize, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const population = Math.max(2, Math.min(100, Math.floor(Number(peptidePopulationSize) || 16)));
    const currentElite = Number(options.peptideEliteSize);
    const elite =
      Number.isFinite(currentElite)
        ? Math.max(1, Math.min(population - 1, Math.floor(currentElite)))
        : Math.min(5, population - 1);
    return {
      ...options,
      peptidePopulationSize: population,
      peptideEliteSize: elite
    };
  });
}

export function handleRuntimePeptideEliteSizeChangeAction<TDraft extends DraftLike>(params: {
  peptideEliteSize: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideEliteSize, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const population = Math.max(2, Math.floor(Number(options.peptidePopulationSize) || 16));
    return {
      ...options,
      peptideEliteSize: Math.max(1, Math.min(population - 1, Math.floor(Number(peptideEliteSize) || 5)))
    };
  });
}

export function handleRuntimePeptideMutationRateChangeAction<TDraft extends DraftLike>(params: {
  peptideMutationRate: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideMutationRate, setDraft } = params;
  patchDraftOptions(setDraft, (options) => ({
    ...options,
    peptideMutationRate: Math.max(0.01, Math.min(1, Math.round((Number(peptideMutationRate) || 0.25) * 100) / 100))
  }));
}

export function handleRuntimePeptideBicyclicLinkerCcdChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicLinkerCcd: 'SEZ' | '29N' | 'BS3';
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicLinkerCcd, setDraft } = params;
  patchDraftOptions(setDraft, (options) => ({
    ...options,
    peptideBicyclicLinkerCcd
  }));
}

export function handleRuntimePeptideBicyclicCysPositionModeChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicCysPositionMode: 'auto' | 'manual';
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicCysPositionMode, setDraft } = params;
  patchDraftOptions(setDraft, (options) => ({
    ...options,
    peptideBicyclicCysPositionMode
  }));
}

export function handleRuntimePeptideBicyclicFixTerminalCysChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicFixTerminalCys: boolean;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicFixTerminalCys, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideBicyclicFixTerminalCys
    };
    return {
      ...nextOptions,
      ...normalizeBicyclicPositions(nextOptions)
    };
  });
}

export function handleRuntimePeptideBicyclicIncludeExtraCysChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicIncludeExtraCys: boolean;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicIncludeExtraCys, setDraft } = params;
  patchDraftOptions(setDraft, (options) => ({
    ...options,
    peptideBicyclicIncludeExtraCys
  }));
}

export function handleRuntimePeptideBicyclicCys1PosChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicCys1Pos: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicCys1Pos, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideBicyclicCys1Pos
    };
    return {
      ...nextOptions,
      ...normalizeBicyclicPositions(nextOptions)
    };
  });
}

export function handleRuntimePeptideBicyclicCys2PosChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicCys2Pos: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicCys2Pos, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideBicyclicCys2Pos
    };
    return {
      ...nextOptions,
      ...normalizeBicyclicPositions(nextOptions)
    };
  });
}

export function handleRuntimePeptideBicyclicCys3PosChangeAction<TDraft extends DraftLike>(params: {
  peptideBicyclicCys3Pos: number;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
}): void {
  const { peptideBicyclicCys3Pos, setDraft } = params;
  patchDraftOptions(setDraft, (options) => {
    const nextOptions = {
      ...options,
      peptideBicyclicCys3Pos
    };
    return {
      ...nextOptions,
      ...normalizeBicyclicPositions(nextOptions)
    };
  });
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
