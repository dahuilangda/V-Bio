import { useEffect, useMemo, useRef, useState } from 'react';
import { CustomResidueEditorModal } from './CustomResidueEditorModal';
import { CommitNumberInput } from '../../components/common/CommitNumberInput';
import { MemoLigand2DPreview } from '../../components/project/Ligand2DPreview';
import { buildCustomResidueCatalog, BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES, type ResidueCatalogEntry } from '../../components/project/residueCatalog';
import { AMINO_ACID_BACKBONE_SMARTS, rdkitMolHasAminoAcidBackbone } from '../../utils/inputValidation';
import { loadRDKitModule } from '../../utils/rdkit';
import type { CustomCcdMoleculeInput, CustomResidueBackbone, PeptideResiduePoolSelection } from '../../types/models';
import { normalizePredictionBackend } from './projectDraftUtils';
import { detectCustomResidueBackbone, firstBackboneSlotError, generateCustomResidueCode, validateBackboneSlots, validateCustomResidueBackbone, type BackboneSlotErrors } from '../../utils/constraintAtomOptions';
import { useAuth } from '../../hooks/useAuth';
import { InfoTip } from '../../components/common/InfoTip';
import { CUSTOM_RESIDUE_SCAFFOLD_SMILES } from './peptideCustomResidues';
import { PeptideCysSpectrum } from '../../components/project/PeptideCysSpectrum';
import { Field } from '../../components/common/Field';
import {
  resolveAnchorsAtLength,
  validateCysLayout,
  cysLayoutRangeNotice,
  type CysLayoutMode,
  type CysLayoutParams
} from '../../utils/peptideCysLayout';

type CysSlot = 'cys1' | 'cys2' | 'cys3';
type BicyclicLinkerType = 'SEZ' | '29N' | 'BS3';

// Canonical CCD SMILES used for RDKit previews.
const BICYCLIC_LINKERS: Array<{ type: BicyclicLinkerType; name: string; smiles: string }> = [
  { type: 'SEZ', name: '1,3,5-Trimethylbenzene', smiles: 'Cc1cc(C)cc(C)c1' },
  { type: '29N', name: 'Triazinane linker', smiles: 'CC(=O)CCN1CN(CC(=O)CC)CN(CC(=O)CC)C1' },
  { type: 'BS3', name: 'Bi(III) center', smiles: '[Bi+3]' }
];


type ResiduePlacementRule = 'any' | 'n_term' | 'c_term' | 'terminal';

function residuePlacementRule(entry: ResidueCatalogEntry): ResiduePlacementRule {
  return entry.placement || 'any';
}

function normalizePoolEntryKind(entry: ResidueCatalogEntry): PeptideResiduePoolSelection['kind'] {
  return entry.group === 'Natural' ? 'natural' : entry.custom ? 'custom' : 'preset';
}

function placementLabel(rule: ResiduePlacementRule): string {
  if (rule === 'n_term') return 'N-terminal only';
  if (rule === 'c_term') return 'C-terminal only';
  if (rule === 'terminal') return 'Terminal positions only';
  return 'Any editable position';
}

function normalizeCustomResidueCode(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12);
}

const PEPTIDE_EFFORT_PRESETS = [
  { id: 'quick', label: 'Quick screen', iterations: 6, populationSize: 8, eliteSize: 3 },
  { id: 'balanced', label: 'Balanced', iterations: 12, populationSize: 16, eliteSize: 5 },
  { id: 'thorough', label: 'Thorough', iterations: 24, populationSize: 32, eliteSize: 8 }
] as const;

const CUSTOM_BACKBONE_SLOTS = ['n', 'ca', 'c', 'o', 'oxt'] as const;
const CUSTOM_BACKBONE_SLOT_LABELS: Record<(typeof CUSTOM_BACKBONE_SLOTS)[number], string> = {
  n: 'N',
  ca: 'CA',
  c: 'C',
  o: 'O',
  oxt: 'OXT'
};



export interface WorkflowRuntimeSettingsSectionProps {
  isVisible: boolean;
  isEditable: boolean;
  isPredictionWorkflow: boolean;
  isPeptideDesignWorkflow: boolean;
  isAffinityWorkflow: boolean;
  backend: string;
  seed: number | null;
  isLowVram: boolean;
  peptideDesignMode: 'linear' | 'cyclic' | 'bicyclic';
  peptideChirality: 'l' | 'd';
  peptideStructureMode: 'auto' | 'helix' | 'hairpin' | 'strand_loop';
  peptideBinderLength: number;
  peptideLengthMin: number;
  peptideLengthMax: number;
  isPeptideUseInitialSequence: boolean;
  peptideInitialSequence: string;
  peptideStructureUpload: {
    fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
  } | null;
  onPeptideStructureUploadChange: (upload: {
    fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
  } | null) => void;
  peptideSequenceMask: string;
  peptideIterations: number;
  peptidePopulationSize: number;
  peptideEliteSize: number;
  peptideResiduePool: PeptideResiduePoolSelection[];
  hasPeptideResiduePool?: boolean;
  peptideNonNaturalMin: number;
  peptideNonNaturalMax: number;
  peptideCustomResidueLibrary: CustomCcdMoleculeInput[];
  onCustomResidueLibraryChange: (library: CustomCcdMoleculeInput[]) => void;
  peptideBicyclicLinkerCcd: BicyclicLinkerType;
  peptideBicyclicCysLayout: CysLayoutMode;
  peptideBicyclicRing1: number;
  peptideBicyclicRing2: number;
  peptideBicyclicRatio1: number;
  peptideBicyclicRatio2: number;
  peptideBicyclicRatio3: number;
  isPeptideBicyclicFixTerminalCys: boolean;
  isPeptideBicyclicIncludeExtraCys: boolean;
  peptideBicyclicCys1Pos: number;
  peptideBicyclicCys2Pos: number;
  peptideBicyclicCys3Pos: number;
  onBackendChange: (backend: string) => void;
  onSeedChange: (seed: number | null) => void;
  onLowVramChange: (isLowVram: boolean) => void;
  onPeptideDesignModeChange: (mode: 'linear' | 'cyclic' | 'bicyclic') => void;
  onPeptideChiralityChange: (chirality: 'l' | 'd') => void;
  onPeptideStructureModeChange: (
    mode: 'auto' | 'helix' | 'hairpin' | 'strand_loop'
  ) => void;
  onPeptideLengthRange: (min: number, max: number) => void;
  onPeptideUseInitialSequenceChange: (value: boolean) => void;
  onPeptideInitialSequenceChange: (value: string) => void;
  onPeptideSequenceMaskChange: (value: string) => void;
  onPeptideIterationsChange: (value: number) => void;
  onPeptidePopulationSizeChange: (value: number) => void;
  onPeptideEliteSizeChange: (value: number) => void;
  onPeptideResiduePoolChange: (value: PeptideResiduePoolSelection[]) => void;
  onPeptideNonNaturalRangeChange: (min: number, max: number) => void;
  onPeptideBicyclicLinkerCcdChange: (value: BicyclicLinkerType) => void;
  onPeptideBicyclicCysLayoutChange: (value: CysLayoutMode) => void;
  onPeptideBicyclicRingChange: (ring1: number, ring2: number) => void;
  onPeptideBicyclicRatioChange: (pct1: number, pct2: number, pct3?: number) => void;
  onPeptideBicyclicFixTerminalCysChange: (value: boolean) => void;
  onPeptideBicyclicIncludeExtraCysChange: (value: boolean) => void;
  onPeptideBicyclicCys1PosChange: (value: number) => void;
  onPeptideBicyclicCys2PosChange: (value: number) => void;
  onPeptideBicyclicCys3PosChange: (value: number) => void;
}

export function WorkflowRuntimeSettingsSection({
  isVisible,
  isEditable,
  isPredictionWorkflow,
  isPeptideDesignWorkflow,
  isAffinityWorkflow,
  backend,
  seed,
  isLowVram,
  peptideDesignMode,
  peptideChirality,
  peptideStructureMode,
  peptideBinderLength,
  peptideLengthMin,
  peptideLengthMax,
  isPeptideUseInitialSequence,
  peptideInitialSequence,
  peptideStructureUpload,
  onPeptideStructureUploadChange,
  peptideSequenceMask,
  peptideIterations,
  peptidePopulationSize,
  peptideEliteSize,
  peptideResiduePool,
  hasPeptideResiduePool = true,
  peptideNonNaturalMin,
  peptideNonNaturalMax,
  peptideCustomResidueLibrary,
  onCustomResidueLibraryChange,
  peptideBicyclicLinkerCcd,
  peptideBicyclicCysLayout,
  peptideBicyclicRing1,
  peptideBicyclicRing2,
  peptideBicyclicRatio1,
  peptideBicyclicRatio2,
  peptideBicyclicRatio3,
  isPeptideBicyclicFixTerminalCys,
  isPeptideBicyclicIncludeExtraCys,
  peptideBicyclicCys1Pos,
  peptideBicyclicCys2Pos,
  peptideBicyclicCys3Pos,
  onBackendChange,
  onSeedChange,
  onLowVramChange,
  onPeptideDesignModeChange,
  onPeptideChiralityChange,
  onPeptideStructureModeChange,
  onPeptideLengthRange,
  onPeptideUseInitialSequenceChange,
  onPeptideInitialSequenceChange,
  onPeptideSequenceMaskChange,
  onPeptideIterationsChange,
  onPeptidePopulationSizeChange,
  onPeptideEliteSizeChange,
  onPeptideResiduePoolChange,
  onPeptideNonNaturalRangeChange,
  onPeptideBicyclicLinkerCcdChange,
  onPeptideBicyclicCysLayoutChange,
  onPeptideBicyclicRingChange,
  onPeptideBicyclicRatioChange,
  onPeptideBicyclicFixTerminalCysChange,
  onPeptideBicyclicIncludeExtraCysChange,
  onPeptideBicyclicCys1PosChange,
  onPeptideBicyclicCys2PosChange,
  onPeptideBicyclicCys3PosChange
}: WorkflowRuntimeSettingsSectionProps) {
  const { session } = useAuth();
  const currentUserId = session?.userId ?? null;
  const [activeCysSlot, setActiveCysSlot] = useState<CysSlot>('cys1');
  const [structureUploadError, setStructureUploadError] = useState<string | null>(null);
  const [customEditorOpen, setCustomEditorOpen] = useState(false);
  const [customEditingCcd, setCustomEditingCcd] = useState('');
  const [customDraftName, setCustomDraftName] = useState('Custom residue');
  const [customDraftBaseResidue, setCustomDraftBaseResidue] = useState('A');
  const [customDraftSmiles, setCustomDraftSmiles] = useState(CUSTOM_RESIDUE_SCAFFOLD_SMILES);
  const [customDraftValid, setCustomDraftValid] = useState(false);
  // manual backbone atom slots (0-based indices); auto-prefilled from RDKit,
  // corrected by clicking atoms in the 2D, saved on the residue as `backbone`
  const [customDraftBackbone, setCustomDraftBackbone] = useState<Partial<CustomResidueBackbone>>({});
  const [customDraftAmidated, setCustomDraftAmidated] = useState(false);
  // 'failed': Auto found no backbone; keep the manual picks and show the reason
  const [customDraftAutoStatus, setCustomDraftAutoStatus] = useState<'idle' | 'failed'>('idle');
  // per-slot errors for the manual override (empty = valid); blocks Save
  const [customDraftSlotErrors, setCustomDraftSlotErrors] = useState<BackboneSlotErrors>({});
  const [armedBackboneSlot, setArmedBackboneSlot] = useState<(typeof CUSTOM_BACKBONE_SLOTS)[number] | null>(null);
  const skipBackboneAutoDetectRef = useRef(false);
  const prevCustomDraftAmidatedRef = useRef(customDraftAmidated);
  // true once the user clicked any atom: SMILES edits then validate the picks
  // instead of auto-detecting over them; cleared by Auto and on open
  const manualOverrideBackboneRef = useRef(false);
  const normalizedBackend = isAffinityWorkflow ? 'boltz' : normalizePredictionBackend(backend);
  // peptide design only offers the docking engines (+ AF3); migrate a legacy
  // 'boltz' so the select never shows an unmatched value
  const peptideBackendAllowed =
    normalizedBackend === 'boltz2dock' || normalizedBackend === 'protenix2dock' || normalizedBackend === 'alphafold3';
  useEffect(() => {
    if (isPeptideDesignWorkflow && !peptideBackendAllowed) {
      onBackendChange('protenix2dock');
    }
  }, [isPeptideDesignWorkflow, peptideBackendAllowed, onBackendChange]);
  // local mirror for instant clicks; external value changes win
  const [backendMirror, setBackendMirror] = useState<string>(normalizedBackend);
  const [residuePoolOpen, setResiduePoolOpen] = useState(false);
  useEffect(() => {
    setBackendMirror(normalizedBackend);
  }, [normalizedBackend]);
  const displayedBackend = isPeptideDesignWorkflow ? backendMirror : normalizedBackend;

  const handlePeptideBackendSelect = (value: string) => {
    setBackendMirror(value);
    onBackendChange(value);
  };
  const canEditRuntimeIdentity = isEditable || isPredictionWorkflow || isPeptideDesignWorkflow || isAffinityWorkflow;
  const isBicyclicMode = isPeptideDesignWorkflow && peptideDesignMode === 'bicyclic';
  // constrained rings are Protenix-only; migrate stale selections before submit
  useEffect(() => {
    if (
      isPeptideDesignWorkflow &&
      peptideDesignMode !== 'linear' &&
      normalizedBackend !== 'protenix' &&
      canEditRuntimeIdentity
    ) {
      onBackendChange('protenix2dock');
    }
  }, [isPeptideDesignWorkflow, peptideDesignMode, normalizedBackend, canEditRuntimeIdentity, onBackendChange]);
  // rail reference length: range max when set, else the legacy binder length
  const cysReferenceLength = Math.max(8, peptideLengthMax || peptideBinderLength || 20);
  // preview length = the range the backend designs in: locked uses that exact
  // length, open uses its max, fully unset falls back to the fixed binder length
  const lengthLocked = peptideLengthMin === peptideLengthMax;
  const effectiveDesignLength = lengthLocked
    ? peptideLengthMin
    : (peptideLengthMax || peptideBinderLength || 20);
  const cysPositionAuto = peptideBicyclicCysLayout === 'auto';
  const cysLayoutParams: CysLayoutParams = useMemo(
    () => ({
      ring: { ring1: peptideBicyclicRing1, ring2: peptideBicyclicRing2 },
      ratio: { pct1: peptideBicyclicRatio1, pct2: peptideBicyclicRatio2, pct3: peptideBicyclicRatio3 },
      absolute: {
        cys1: peptideBicyclicCys1Pos,
        cys2: peptideBicyclicCys2Pos,
        cys3: isPeptideBicyclicFixTerminalCys ? cysReferenceLength : peptideBicyclicCys3Pos
      }
    }),
    [
      peptideBicyclicRing1,
      peptideBicyclicRing2,
      peptideBicyclicRatio1,
      peptideBicyclicRatio2,
      peptideBicyclicRatio3,
      peptideBicyclicCys1Pos,
      peptideBicyclicCys2Pos,
      peptideBicyclicCys3Pos,
      isPeptideBicyclicFixTerminalCys,
      cysReferenceLength
    ]
  );
  const resolvedCysAnchors = useMemo(
    () => resolveAnchorsAtLength(peptideBicyclicCysLayout, cysLayoutParams, cysReferenceLength, isPeptideBicyclicFixTerminalCys),
    [peptideBicyclicCysLayout, cysLayoutParams, cysReferenceLength, isPeptideBicyclicFixTerminalCys]
  );
  const cysLayoutError = useMemo(
    () => (isBicyclicMode ? validateCysLayout({
      mode: peptideBicyclicCysLayout,
      layout: cysLayoutParams,
      lengthMin: peptideLengthMin,
      lengthMax: peptideLengthMax,
      fixTerminalCys: isPeptideBicyclicFixTerminalCys
    }) : null),
    [isBicyclicMode, peptideBicyclicCysLayout, cysLayoutParams, peptideLengthMin, peptideLengthMax, isPeptideBicyclicFixTerminalCys]
  );
  const cysLayoutNotice = useMemo(
    () => (isBicyclicMode && !cysLayoutError ? cysLayoutRangeNotice({
      mode: peptideBicyclicCysLayout,
      layout: cysLayoutParams,
      lengthMin: peptideLengthMin,
      lengthMax: peptideLengthMax,
      fixTerminalCys: isPeptideBicyclicFixTerminalCys
    }) : null),
    [isBicyclicMode, cysLayoutError, peptideBicyclicCysLayout, cysLayoutParams, peptideLengthMin, peptideLengthMax, isPeptideBicyclicFixTerminalCys]
  );
  const cysSlotValueMap = useMemo(
    () => ({
      cys1: resolvedCysAnchors?.[0] ?? peptideBicyclicCys1Pos,
      cys2: resolvedCysAnchors?.[1] ?? peptideBicyclicCys2Pos,
      cys3: resolvedCysAnchors?.[2]
        ?? (isPeptideBicyclicFixTerminalCys ? cysReferenceLength : peptideBicyclicCys3Pos)
    }),
    [resolvedCysAnchors, peptideBicyclicCys1Pos, peptideBicyclicCys2Pos, peptideBicyclicCys3Pos, isPeptideBicyclicFixTerminalCys, cysReferenceLength]
  );
  const cysSlotMaxMap = useMemo(
    () => ({
      cys1: Math.max(1, cysReferenceLength - 2),
      cys2: isPeptideBicyclicFixTerminalCys
        ? Math.max(1, cysReferenceLength - 2)
        : Math.max(1, cysReferenceLength - 1),
      cys3: cysReferenceLength
    }),
    [cysReferenceLength, isPeptideBicyclicFixTerminalCys]
  );
  const positions = useMemo(
    () => Array.from({ length: Math.max(1, cysReferenceLength) }, (_, idx) => idx + 1),
    [cysReferenceLength]
  );
  const spectrumResolver = useMemo(
    () => (length: number) => resolveAnchorsAtLength(
      peptideBicyclicCysLayout, cysLayoutParams, length, isPeptideBicyclicFixTerminalCys),
    [peptideBicyclicCysLayout, cysLayoutParams, isPeptideBicyclicFixTerminalCys]
  );
  const rangeIsOpen = peptideLengthMin !== peptideLengthMax;
  const normalizedInitialSequence = useMemo(
    () =>
      String(peptideInitialSequence || '')
        .replace(/[\s_-]/g, '')
        .toUpperCase()
        .slice(0, effectiveDesignLength),
    [peptideInitialSequence, effectiveDesignLength]
  );
  const normalizedSequenceMask = useMemo(() => {
    const normalized = String(peptideSequenceMask || '')
      .replace(/[\s_-]/g, '')
      .toUpperCase()
      .replace(/[^ARNDCQEGHILKMFPSTWYVX]/g, '')
      .slice(0, effectiveDesignLength);
    if (!normalized) return 'X'.repeat(Math.max(1, effectiveDesignLength));
    return normalized.padEnd(Math.max(1, effectiveDesignLength), 'X');
  }, [peptideSequenceMask, effectiveDesignLength]);
  const maskChars = useMemo(() => normalizedSequenceMask.split(''), [normalizedSequenceMask]);

  useEffect(() => {
    let cancelled = false;
    const amidatedChanged = prevCustomDraftAmidatedRef.current !== customDraftAmidated;
    prevCustomDraftAmidatedRef.current = customDraftAmidated;
    // debounce JSME drawing so rapid SMILES changes don't flicker the picks
    const timer = window.setTimeout(() => {
      const validate = async () => {
        const smiles = customDraftSmiles.trim();
        if (!smiles) {
          setCustomDraftValid(false);
          setCustomDraftBackbone({});
          return;
        }
        try {
          const rdkit = await loadRDKitModule();
          if (cancelled) return;
          const valid = rdkitMolHasAminoAcidBackbone(rdkit, smiles, true);
          setCustomDraftValid(valid);
          // amidation flips the terminal element; re-detect keeping the user's picks
          if (amidatedChanged) {
            skipBackboneAutoDetectRef.current = false;
            const anchors = manualOverrideBackboneRef.current ? customDraftBackbone : {};
            setCustomDraftBackbone(detectCustomResidueBackbone(rdkit, smiles, anchors, customDraftAmidated) ?? {});
            setCustomDraftAutoStatus('idle');
          } else if (manualOverrideBackboneRef.current) {
            // user picked atoms: validate them, never auto-detect over them
            const kept = validateBackboneSlots(rdkit, smiles, customDraftBackbone, customDraftAmidated);
            if (CUSTOM_BACKBONE_SLOTS.some((slot) => kept[slot] !== customDraftBackbone[slot])) {
              setCustomDraftBackbone(kept);
            }
          } else if (skipBackboneAutoDetectRef.current) {
            skipBackboneAutoDetectRef.current = false;
          } else if (valid) {
            // fresh auto-detection as a starting suggestion
            setCustomDraftBackbone(detectCustomResidueBackbone(rdkit, smiles, {}, customDraftAmidated) ?? {});
            setCustomDraftAutoStatus('idle');
          } else {
            setCustomDraftBackbone({});
            setCustomDraftAutoStatus('idle');
          }
        } catch {
          if (!cancelled) {
            setCustomDraftValid(false);
            setCustomDraftBackbone({});
          }
        }
      };
      void validate();
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customDraftSmiles, customDraftAmidated]);

  // recompute slot errors on change; complete-but-wrong blocks Save,
  // incomplete is fine (backend auto-detects)
  useEffect(() => {
    let cancelled = false;
    const recompute = async () => {
      const backboneComplete = CUSTOM_BACKBONE_SLOTS.every((slot) => customDraftBackbone[slot] !== undefined);
      if (!backboneComplete) {
        if (!cancelled) setCustomDraftSlotErrors({});
        return;
      }
      try {
        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        const errors = validateCustomResidueBackbone(rdkit, customDraftSmiles.trim(), customDraftBackbone as CustomResidueBackbone, customDraftAmidated);
        if (!cancelled) setCustomDraftSlotErrors(errors);
      } catch {
        // RDKit not warmed yet; keep the previous verdict
      }
    };
    const timer = window.setTimeout(() => { void recompute(); }, 150);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customDraftBackbone, customDraftSmiles, customDraftAmidated]);

  const openCustomResidueEditor = (entry?: CustomCcdMoleculeInput) => {
    const ccd = normalizeCustomResidueCode(entry?.ccd || '');
    setCustomEditingCcd(ccd);
    setCustomDraftName(entry?.label || 'Custom residue');
    setCustomDraftBaseResidue(String(entry?.baseResidue || 'A').trim().toUpperCase().slice(0, 1) || 'A');
    setCustomDraftSmiles(entry?.smiles || CUSTOM_RESIDUE_SCAFFOLD_SMILES);
    setCustomDraftAmidated(Boolean(entry?.cTerminalAmidated));
    const storedBackbone = entry?.backbone ?? null;
    if (storedBackbone) {
      setCustomDraftBackbone(storedBackbone);
      skipBackboneAutoDetectRef.current = true;
    } else {
      setCustomDraftBackbone({});
    }
    setArmedBackboneSlot(null);
    manualOverrideBackboneRef.current = Boolean(storedBackbone);
    setCustomEditorOpen(true);
  };

  const closeCustomResidueEditor = () => {
    setCustomEditorOpen(false);
    setCustomEditingCcd('');
    setCustomDraftBackbone({});
    setCustomDraftAmidated(false);
    setArmedBackboneSlot(null);
  };

  // arm a slot, then click an atom to assign; the next unfilled slot arms automatically
  const handleBackboneAtomClick = (atomIndex: number) => {
    if (!armedBackboneSlot) return;
    manualOverrideBackboneRef.current = true;
    const next: Partial<CustomResidueBackbone> = { ...customDraftBackbone };
    if (next[armedBackboneSlot] === atomIndex) {
      delete next[armedBackboneSlot];
    } else {
      for (const slot of CUSTOM_BACKBONE_SLOTS) {
        if (next[slot] === atomIndex) delete next[slot];
      }
      next[armedBackboneSlot] = atomIndex;
    }
    setCustomDraftBackbone(next);
    setCustomDraftAutoStatus('idle');
    const nextUnset = CUSTOM_BACKBONE_SLOTS.find((slot) => next[slot] === undefined);
    setArmedBackboneSlot(nextUnset ?? null);
  };

  const resetBackboneToAuto = async () => {
    manualOverrideBackboneRef.current = false;
    const rdkit = await loadRDKitModule();
    // re-detect with the current picks as anchors; only empty slots are filled
    const detected = detectCustomResidueBackbone(rdkit, customDraftSmiles.trim(), customDraftBackbone, customDraftAmidated);
    if (detected) {
      setCustomDraftBackbone(detected);
      setCustomDraftAutoStatus('idle');
    } else {
      // keep the manual picks and explain, rather than wiping them
      setCustomDraftAutoStatus('failed');
    }
    setArmedBackboneSlot(null);
  };

  // derived 2D display: highlight assigned atoms with their slot letters
  const assignedBackboneIndices = CUSTOM_BACKBONE_SLOTS
    .map((slot) => customDraftBackbone[slot])
    .filter((idx): idx is number => idx !== undefined);
  const slotLabel = (slot: (typeof CUSTOM_BACKBONE_SLOTS)[number]) =>
    slot === 'oxt' && customDraftAmidated ? 'NXT' : CUSTOM_BACKBONE_SLOT_LABELS[slot];

  const backboneAtomLabels: string[] | null = assignedBackboneIndices.length
    ? (() => {
        const maxIdx = Math.max(...assignedBackboneIndices);
        const labels: string[] = new Array(maxIdx + 1).fill('');
        for (const slot of CUSTOM_BACKBONE_SLOTS) {
          const idx = customDraftBackbone[slot];
          if (idx !== undefined) labels[idx] = slotLabel(slot);
        }
        return labels;
      })()
    : null;
  const errorBackboneIndices = (CUSTOM_BACKBONE_SLOTS as readonly (keyof CustomResidueBackbone)[])
    .filter((slot) => Boolean(customDraftSlotErrors[slot]))
    .map((slot) => customDraftBackbone[slot])
    .filter((idx): idx is number => typeof idx === 'number');
  const backboneHighlightColorOverride: Record<number, [number, number, number]> | null = errorBackboneIndices.length
    ? Object.fromEntries(errorBackboneIndices.map((idx) => [idx, [0.86, 0.18, 0.18]] as [number, [number, number, number]]))
    : null;

  const customResidues = useMemo<ResidueCatalogEntry[]>(() => {
    const poolSources = (peptideResiduePool || [])
      .filter((item) => item.kind === 'custom' && String(item.smiles || '').trim())
      .map((item) => ({
        ccd: item.code,
        smiles: String(item.smiles),
        baseResidue: item.baseResidue,
        label: item.label,
        backbone: item.backbone,
        cTerminalAmidated: item.cTerminalAmidated
      }));
    return buildCustomResidueCatalog([...poolSources, ...peptideCustomResidueLibrary]);
  }, [peptideResiduePool, peptideCustomResidueLibrary]);

  // custom pool entries carry their own CCD SMILES so the definition
  // persists with the selection and reaches the backends as a CCD
  const poolEntryFromCatalog = (
    entry: ResidueCatalogEntry,
    kind: PeptideResiduePoolSelection['kind']
  ): PeptideResiduePoolSelection => {
    if (kind === 'custom' && entry.smiles) {
      return {
        code: entry.ccd,
        kind: 'custom',
        smiles: entry.smiles,
        baseResidue: entry.baseResidue,
        label: entry.label,
        backbone: entry.backbone,
        cTerminalAmidated: entry.cTerminalAmidated
      };
    }
    return { code: entry.ccd, kind };
  };

  // backfill a drawn SMILES from the library onto pool entries lacking one,
  // so the submit path only ever reads the pool entry
  const onPeptideResiduePoolChangeRef = useRef(onPeptideResiduePoolChange);
  onPeptideResiduePoolChangeRef.current = onPeptideResiduePoolChange;
  useEffect(() => {
    if (!isPeptideDesignWorkflow) return;
    const libraryByCode = new Map<string, CustomCcdMoleculeInput>();
    for (const item of peptideCustomResidueLibrary) {
      const code = normalizeCustomResidueCode(item.ccd);
      if (code && String(item.smiles || '').trim()) libraryByCode.set(code, item);
    }
    if (libraryByCode.size === 0) return;
    let changed = false;
    const nextPool = peptideResiduePool.map((entry) => {
      if (entry.kind !== 'custom' || String(entry.smiles || '').trim()) return entry;
      const lib = libraryByCode.get(normalizeCustomResidueCode(entry.code));
      if (!lib) return entry;
      changed = true;
      return { ...entry, smiles: lib.smiles, baseResidue: lib.baseResidue, label: lib.label, backbone: lib.backbone, cTerminalAmidated: lib.cTerminalAmidated };
    });
    if (changed) onPeptideResiduePoolChangeRef.current(nextPool);
  }, [isPeptideDesignWorkflow, peptideResiduePool, peptideCustomResidueLibrary]);

  const residueCatalogSections = useMemo(
    () => [
      { key: 'natural', title: 'Natural amino acids', kind: 'natural' as const, entries: NATURAL_AMINO_ACID_RESIDUES },
      { key: 'preset', title: 'Preset non-natural residues', kind: 'preset' as const, entries: BUILT_IN_PROTEIN_MODIFICATIONS },
      { key: 'custom', title: 'Custom library', kind: 'custom' as const, entries: customResidues }
    ],
    [customResidues]
  );
  const residueCatalog = useMemo(
    () => residueCatalogSections.flatMap((section) => section.entries),
    [residueCatalogSections]
  );
  const selectedResidueKeySet = useMemo(() => {
    const selected = new Set<string>();
    if (Array.isArray(peptideResiduePool)) {
      peptideResiduePool.forEach((item) => selected.add(`${item.kind}:${item.code}`));
    }
    if (hasPeptideResiduePool && selected.size === 0) {
      NATURAL_AMINO_ACID_RESIDUES.forEach((item) => selected.add(`natural:${item.ccd}`));
    }
    return selected;
  }, [peptideResiduePool, hasPeptideResiduePool]);
  const selectedNonNaturalCount = useMemo(
    () =>
      residueCatalogSections
        .filter((section) => section.kind !== 'natural')
        .flatMap((section) => section.entries.map((entry) => `${section.kind}:${entry.ccd}`))
        .filter((key) => selectedResidueKeySet.has(key)).length,
    [residueCatalogSections, selectedResidueKeySet]
  );
  const selectedNaturalCount = useMemo(
    () => NATURAL_AMINO_ACID_RESIDUES.filter((entry) => selectedResidueKeySet.has(`natural:${entry.ccd}`)).length,
    [selectedResidueKeySet]
  );
  const residuePoolControlsDisabled = !isEditable;
  const protectedResiduePositions = useMemo(() => {
    const protectedSet = new Set<number>();
    maskChars.forEach((maskChar, idx) => {
      if (maskChar && maskChar !== 'X') protectedSet.add(idx + 1);
    });
    if (isBicyclicMode) {
      Object.values(cysSlotValueMap).forEach((pos) => {
        const normalized = Math.max(1, Math.min(effectiveDesignLength, Math.floor(Number(pos) || 1)));
        protectedSet.add(normalized);
      });
    }
    return protectedSet;
  }, [maskChars, isBicyclicMode, cysSlotValueMap, effectiveDesignLength]);
  const residuePlacementStatusByKey = useMemo(() => {
    const status = new Map<string, { selectable: boolean; allowedPositions: number[]; reason: string; placement: string }>();
    residueCatalogSections.forEach((section) => {
      section.entries.forEach((entry) => {
        const key = `${section.kind}:${entry.ccd}`;
        const rule = residuePlacementRule(entry);
        let candidatePositions = positions;
        if (rule === 'n_term') candidatePositions = positions.filter((position) => position === 1);
        if (rule === 'c_term') candidatePositions = positions.filter((position) => position === effectiveDesignLength);
        if (rule === 'terminal') candidatePositions = positions.filter((position) => position === 1 || position === effectiveDesignLength);
        if (isBicyclicMode && section.kind === 'natural' && entry.ccd === 'CYS') {
          status.set(key, {
            selectable: false,
            allowedPositions: [],
            reason: 'Cys positions are controlled by bicyclic linker settings in this mode.',
            placement: 'Bicyclic linker controlled'
          });
          return;
        }
        const allowedPositions = candidatePositions.filter((position) => !protectedResiduePositions.has(position));
        const placement = entry.placementLabel || placementLabel(rule);
        let reason = allowedPositions.length > 0 ? `${placement}; ${allowedPositions.length} editable position${allowedPositions.length === 1 ? '' : 's'} available.` : '';
        if (allowedPositions.length === 0) {
          reason = `${placement}; no editable position is available with the current mask and design mode.`;
          if (rule === 'n_term' && protectedResiduePositions.has(1)) reason = `${placement}; position 1 is fixed by the sequence mask.`;
          if (rule === 'c_term' && protectedResiduePositions.has(effectiveDesignLength)) reason = `${placement}; the C-terminal position is fixed by the sequence mask.`;
          if (rule === 'terminal') reason = `${placement}; both terminal positions are fixed or protected.`;
        }
        status.set(key, {
          selectable: allowedPositions.length > 0,
          allowedPositions,
          reason,
          placement
        });
      });
    });
    return status;
  }, [residueCatalogSections, positions, effectiveDesignLength, protectedResiduePositions, isBicyclicMode]);
  const clampNonNaturalLimit = (value: number) => Math.max(0, Math.min(effectiveDesignLength, Math.floor(Number(value) || 0)));
  const toggleResiduePoolEntry = (entry: ResidueCatalogEntry) => {
    if (residuePoolControlsDisabled) return;
    const kind = normalizePoolEntryKind(entry);
    const key = `${kind}:${entry.ccd}`;
    const next = new Set(selectedResidueKeySet);
    if (next.has(key)) {
      next.delete(key);
    } else if (residuePlacementStatusByKey.get(key)?.selectable !== false) {
      next.add(key);
    } else {
      return;
    }
    const ordered = residueCatalog
      .map((item) => poolEntryFromCatalog(item, normalizePoolEntryKind(item)))
      .filter((item) => next.has(`${item.kind}:${item.code}`));
    onPeptideResiduePoolChange(ordered);
  };

  const setResidueSectionSelection = (sectionKind: PeptideResiduePoolSelection['kind'], entries: ResidueCatalogEntry[], selected: boolean) => {
    if (residuePoolControlsDisabled) return;
    const next = new Set(selectedResidueKeySet);
    entries.forEach((entry) => {
      const key = `${sectionKind}:${entry.ccd}`;
      if (selected) {
        if (residuePlacementStatusByKey.get(key)?.selectable !== false) next.add(key);
      } else {
        next.delete(key);
      }
    });
    const ordered = residueCatalogSections
      .flatMap((section) =>
        section.entries.map((entry) => poolEntryFromCatalog(entry, section.kind))
      )
      .filter((item) => next.has(`${item.kind}:${item.code}`));
    onPeptideResiduePoolChange(ordered);
  };


  const saveCustomResidueDraft = () => {
    if (residuePoolControlsDisabled || !customDraftValid || firstBackboneSlotError(customDraftSlotErrors)) return;
    const smiles = customDraftSmiles.trim();
    if (!smiles) return;
    // existing residues keep their code; new ones get a deterministic generated code
    const ccd = customEditingCcd || normalizeCustomResidueCode(generateCustomResidueCode(currentUserId, smiles));
    if (!ccd) return;
    const baseResidue = customDraftBaseResidue.trim().toUpperCase().slice(0, 1) || undefined;
    const label = customDraftName.trim() || 'Custom residue';
    // save the backbone only when all 5 slots are set; else backend auto-detects
    const backbone: CustomResidueBackbone | undefined = CUSTOM_BACKBONE_SLOTS.every(
      (slot) => customDraftBackbone[slot] !== undefined
    )
      ? (customDraftBackbone as CustomResidueBackbone)
      : undefined;
    const nextEntry: CustomCcdMoleculeInput = { ccd, smiles, baseResidue, label, backbone, cTerminalAmidated: customDraftAmidated || undefined };
    const nextLibrary = [
      nextEntry,
      ...peptideCustomResidueLibrary.filter((item) => {
        const itemCcd = normalizeCustomResidueCode(item.ccd);
        return itemCcd !== ccd && itemCcd !== customEditingCcd;
      })
    ].slice(0, 80);
    onCustomResidueLibraryChange(nextLibrary);
    const selectedKeys = new Set(selectedResidueKeySet);
    selectedKeys.add(`custom:${ccd}`);
    // the fresh residue is the source of truth for its own SMILES
    const freshEntry: PeptideResiduePoolSelection = { code: ccd, kind: 'custom', smiles, baseResidue, label, backbone, cTerminalAmidated: customDraftAmidated || undefined };
    const ordered = residueCatalogSections
      .flatMap((section) =>
        section.entries.map((entry) => {
          if (section.kind !== 'custom') return { code: entry.ccd, kind: section.kind };
          return normalizeCustomResidueCode(entry.ccd) === ccd ? freshEntry : poolEntryFromCatalog(entry, 'custom');
        })
      )
      .filter((item) => selectedKeys.has(`${item.kind}:${item.code}`));
    if (!ordered.some((item) => item.kind === 'custom' && normalizeCustomResidueCode(item.code) === ccd)) {
      ordered.push(freshEntry);
    }
    onPeptideResiduePoolChange(ordered);
    closeCustomResidueEditor();
  };

  const deleteCustomResidue = (ccdRaw: string) => {
    if (residuePoolControlsDisabled) return;
    const ccd = normalizeCustomResidueCode(ccdRaw);
    onCustomResidueLibraryChange(peptideCustomResidueLibrary.filter((item) => normalizeCustomResidueCode(item.ccd) !== ccd));
    onPeptideResiduePoolChange(peptideResiduePool.filter((item) => !(item.kind === 'custom' && item.code === ccd)));
    if (customEditingCcd === ccd) closeCustomResidueEditor();
  };

  const assignCysPosition = (slot: CysSlot, position: number) => {
    if (!isEditable || cysPositionAuto) return;
    if (slot === 'cys1') {
      onPeptideBicyclicCys1PosChange(position);
      return;
    }
    if (slot === 'cys2') {
      onPeptideBicyclicCys2PosChange(position);
      return;
    }
    if (isPeptideBicyclicFixTerminalCys) return;
    onPeptideBicyclicCys3PosChange(position);
  };

  const toggleMaskPosition = (position: number) => {
    if (!isEditable) return;
    const index = position - 1;
    if (index < 0 || index >= maskChars.length) return;
    const sequenceChar = normalizedInitialSequence[index] || '';
    if (!sequenceChar || sequenceChar === 'X') return;
    const nextMask = [...maskChars];
    nextMask[index] = nextMask[index] === 'X' ? sequenceChar : 'X';
    onPeptideSequenceMaskChange(nextMask.join(''));
  };

  if (!isVisible) return null;

  return (
    <section className="panel subtle component-runtime-settings">
      <div className="component-runtime-settings-row">

          <Field
            label={<>Backend <span className="required-mark">*</span></>}
            hint="Structure prediction engine. Protenix2Dock supports every peptide design mode, including cyclic/bicyclic covalent-bond constraints."
          >
            <select
              required
              value={displayedBackend}
              onChange={(e) =>
                isPeptideDesignWorkflow
                  ? handlePeptideBackendSelect(e.target.value)
                  : onBackendChange(e.target.value)
              }
              disabled={!canEditRuntimeIdentity}
            >
              {(isAffinityWorkflow
                ? [
                    { value: 'boltz', label: 'Boltz-2' }
                  ]
                : isPeptideDesignWorkflow
                  ? (peptideDesignMode !== 'linear'
                      ? [
                          { value: 'protenix2dock', label: 'Protenix2Dock' }
                        ]
                      : [
                          { value: 'protenix2dock', label: 'Protenix2Dock' },
                          { value: 'boltz2dock', label: 'Boltz2Dock' },
                          { value: 'alphafold3', label: 'AlphaFold3' }
                        ])
                  : [
                      { value: 'protenix', label: 'Protenix' },
                      { value: 'boltz', label: 'Boltz-2' },
                      { value: 'alphafold3', label: 'AlphaFold3' }
                    ]
              ).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>


        {(isPredictionWorkflow || isPeptideDesignWorkflow) && (
                      <Field
            label="Seed (optional)"
            hint="Reproducibility seed. Protenix and Boltz2Dock default to 42; AlphaFold3 draws a random seed when unset."
          >
            <input
            type="number"
            min={0}
            value={seed ?? ''}
            onChange={(e) => {
              const value = e.target.value;
              const nextSeed = value === '' ? null : Math.max(0, Math.floor(Number(value) || 0));
              onSeedChange(nextSeed);
            }}
            disabled={!canEditRuntimeIdentity}
            placeholder="42 (Protenix default)"
            />
            </Field>
        )}

        {(isPredictionWorkflow || isPeptideDesignWorkflow) && normalizedBackend !== 'alphafold3' && normalizedBackend !== 'nesso' && (
          <label className="switch-field runtime-device-toggle">
            <input
              type="checkbox"
              checked={isLowVram}
              onChange={(e) => onLowVramChange(e.target.checked)}
              disabled={!canEditRuntimeIdentity}
            />
            <span>Low VRAM</span>
          </label>
        )}

        {isPeptideDesignWorkflow && (
          <div className="peptide-runtime-layout">
            <section className="peptide-runtime-group">
              <div className="peptide-runtime-group-head">General</div>
              <div className="peptide-runtime-grid">
                                  <Field label="Peptide Design Mode">
                  <select
                  value={peptideDesignMode}
                  onChange={(e) =>
                    onPeptideDesignModeChange((e.target.value as 'linear' | 'cyclic' | 'bicyclic') || 'linear')
                  }
                  disabled={!isEditable}
                  >
                  <option value="linear">Linear</option>
                  <option value="cyclic" disabled={normalizedBackend === 'alphafold3'}>
                    Cyclic{normalizedBackend === 'alphafold3' ? ' (Protenix2Dock only)' : ''}
                  </option>
                  <option value="bicyclic" disabled={normalizedBackend === 'alphafold3'}>
                    Bicyclic{normalizedBackend === 'alphafold3' ? ' (Protenix2Dock only)' : ''}
                  </option>
                  </select>
                  </Field>
                                  <Field
                  label="Peptide Chirality"
                  hint="D-peptides run the mirror workflow: the target is mirrored, design happens in D-space, results are flipped back. Available on Boltz2Dock/Protenix2Dock."
                >
                  <select
                  value={peptideChirality}
                  onChange={(e) =>
                    onPeptideChiralityChange((e.target.value as 'l' | 'd') || 'l')
                  }
                  disabled={!isEditable}
                  >
                  <option value="l">L-peptide (standard)</option>
                  <option value="d" disabled={normalizedBackend !== 'boltz2dock' && normalizedBackend !== 'protenix2dock'}>
                    D-peptide
                  </option>
                  </select>
                  </Field>
                  <Field
                  label="Structure Mode"
                  hint="Secondary-structure bias for de-novo proposals: helix favours amphipathic helices, hairpin favours beta-hairpins, strand-loop mixes an extended strand with a turn. Auto leaves the choice to the model."
                >
                  <select
                  value={peptideStructureMode}
                  onChange={(e) =>
                    onPeptideStructureModeChange(
                      (e.target.value as 'auto' | 'helix' | 'hairpin' | 'strand_loop') || 'auto'
                    )
                  }
                  disabled={!isEditable}
                  >
                  <option value="auto">Auto (no bias)</option>
                  <option value="helix">Helix</option>
                  <option value="hairpin">Hairpin</option>
                  <option value="strand_loop">Strand-loop</option>
                  </select>
                  </Field>
                <div className="peptide-structure-seed-row">
                  <Field label="Initial peptide structure (optional)">
                  <div className="peptide-structure-upload">
                  <input
                    type="file"
                    accept=".pdb,.cif,.mmcif"
                    disabled={!isEditable || peptideChirality !== 'd'}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      e.target.value = '';
                      if (!file) return;
                      const format = file.name.toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif';
                      setStructureUploadError(null);
                      file.text().then(
                        (content) => {
                          onPeptideStructureUploadChange({
                            fileName: file.name, format, content, chainId: '',
                          });
                        },
                        (err) => {
                          setStructureUploadError(
                            `Failed to read ${file.name}: ${err instanceof Error ? err.message : 'unknown error'}`
                          );
                        }
                      );
                    }}
                  />
                  {structureUploadError ? (
                    <p className="muted small">{structureUploadError}</p>
                  ) : null}
                  {peptideStructureUpload ? (
                    <div className="peptide-structure-upload-meta">
                      <span title={peptideStructureUpload.fileName}>
                        {peptideStructureUpload.fileName}
                      </span>
                      <button
                        type="button"
                        className="ghost small"
                        disabled={!isEditable}
                        onClick={() => onPeptideStructureUploadChange(null)}
                      >
                        Remove
                      </button>
                    </div>
                  ) : null}
                  </div>
                  </Field>
                  <label className="switch-field peptide-runtime-switch peptide-initial-seq-toggle">
                    <input
                      type="checkbox"
                      checked={isPeptideUseInitialSequence}
                      onChange={(e) => onPeptideUseInitialSequenceChange(e.target.checked)}
                      disabled={!isEditable}
                    />
                    <span>Seed from reference</span>
                    {/* a plain span inside the label would toggle the checkbox */}
                    <span className="peptide-seed-info" onClick={(e) => e.preventDefault()}>
                      <InfoTip text="Use the reference sequence as the starting point for generation 1." align="start" />
                    </span>
                  </label>
                </div>
                <label className="field peptide-length-range">
                  <span>
                    Peptide Length
                    <InfoTip text="Design length window. Set min = max for a fixed length; an open range lets each generation adapt candidate lengths within it." align="start" />
                    {lengthLocked ? ' (fixed)' : ' (min–max)'}
                    {!lengthLocked && peptideLengthMin !== undefined ? (
                      <span className="muted" style={{ marginLeft: 6, fontSize: '0.9em' }}>
                        adaptive {peptideLengthMin}–{peptideLengthMax} aa
                      </span>
                    ) : null}
                  </span>
                  <div className="peptide-length-range-inputs">
                    <CommitNumberInput
                      min={peptideDesignMode === 'bicyclic' ? 8 : 5}
                      max={peptideLengthMax}
                      value={peptideLengthMin}
                      onCommit={(v) => onPeptideLengthRange(v, peptideLengthMax)}
                      disabled={!isEditable}
                    />
                    <span className="peptide-length-range-dash">–</span>
                    <CommitNumberInput
                      min={peptideLengthMin}
                      max={80}
                      value={peptideLengthMax}
                      onCommit={(v) => onPeptideLengthRange(peptideLengthMin, v)}
                      disabled={!isEditable}
                    />
                  </div>
                </label>
                <div className={`peptide-residue-config${residuePoolOpen ? " residue-pool-open" : ""}`}>
                  <button
                    type="button"
                    className="peptide-residue-config-head"
                    onClick={() => setResiduePoolOpen(!residuePoolOpen)}
                    aria-expanded={residuePoolOpen}
                  >
                    <span className="peptide-residue-config-chevron">
                      {residuePoolOpen ? "▾" : "▸"}
                    </span>
                    <strong>Residues used for design</strong>
                    <span className="peptide-residue-selection-summary">
                      {selectedNaturalCount} natural / {selectedNonNaturalCount} non-natural selected
                    </span>
                  </button>
                  <div className="peptide-residue-usage">
                    <div className="peptide-residue-usage-copy">
                      <strong>
                        Non-natural residue count
                        <InfoTip text="Caps how many selected non-natural residues each designed peptide may contain." align="start" />
                      </strong>
                    </div>
                    <div className="peptide-residue-usage-controls">
                      <label className="field peptide-residue-usage-field">
                        <span>At least</span>
                        <CommitNumberInput
                          min={0}
                          max={effectiveDesignLength}
                          value={peptideNonNaturalMin}
                          onCommit={(value) => {
                            const nextMin = clampNonNaturalLimit(value);
                            onPeptideNonNaturalRangeChange(nextMin, Math.max(nextMin, peptideNonNaturalMax));
                          }}
                          disabled={residuePoolControlsDisabled || selectedNonNaturalCount === 0}
                        />
                      </label>
                      <label className="field peptide-residue-usage-field">
                        <span>At most</span>
                        <CommitNumberInput
                          min={peptideNonNaturalMin}
                          max={effectiveDesignLength}
                          value={peptideNonNaturalMax}
                          onCommit={(value) => {
                            const nextMax = clampNonNaturalLimit(value);
                            onPeptideNonNaturalRangeChange(Math.min(peptideNonNaturalMin, nextMax), nextMax);
                          }}
                          disabled={residuePoolControlsDisabled || selectedNonNaturalCount === 0}
                        />
                      </label>
                    </div>
                  </div>
                  {!hasPeptideResiduePool ? (
                    <div className="muted small peptide-runtime-backend-hint">
                      Edits apply to the next submission.
                    </div>
                  ) : null}
                  {residuePoolOpen && (
                  <div className="peptide-residue-pool" aria-label="Design residues">
                    {residueCatalogSections.map((section) => {
                      const sectionSelectedCount = section.entries.filter((entry) =>
                        selectedResidueKeySet.has(`${section.kind}:${entry.ccd}`)
                      ).length;
                      return (
                        <section className="peptide-residue-section" key={section.key}>
                          <div className="peptide-residue-section-head">
                            <strong>{section.title}</strong>
                            <span>{sectionSelectedCount}/{section.entries.length} selected</span>
                            <div className="peptide-residue-section-actions">
                            {section.kind === 'custom' ? (
                              <button
                                type="button"
                                className="btn btn-primary btn-compact"
                                onClick={() => openCustomResidueEditor()}
                                disabled={residuePoolControlsDisabled}
                              >
                                Add
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="btn btn-ghost btn-compact"
                              onClick={() => setResidueSectionSelection(section.kind, section.entries, true)}
                              disabled={residuePoolControlsDisabled || section.entries.length === 0}
                            >
                              Select all
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost btn-compact"
                              onClick={() => setResidueSectionSelection(section.kind, section.entries, false)}
                              disabled={residuePoolControlsDisabled || section.entries.length === 0}
                            >
                              Select none
                            </button>
                          </div>
                        </div>
                        {section.entries.length > 0 ? (
                          <div className="peptide-residue-section-grid" role="list">
                            {section.entries.map((entry) => {
                              const kind: PeptideResiduePoolSelection['kind'] = section.kind;
                              const active = selectedResidueKeySet.has(`${kind}:${entry.ccd}`);
                              const placementStatus = residuePlacementStatusByKey.get(`${kind}:${entry.ccd}`);
                              const unavailable = placementStatus?.selectable === false;
                              const cardDisabled = residuePoolControlsDisabled;
                              const helpText = [entry.backboneLabel, placementStatus?.reason].filter(Boolean).join(' · ');
                              return (
                                <button
                                  key={`${kind}-${entry.ccd}`}
                                  type="button"
                                  role="listitem"
                                  className={`peptide-residue-card ${active ? 'active' : ''} ${kind === 'natural' ? 'natural' : ''} ${unavailable ? 'unavailable' : ''}`}
                                  onClick={() => toggleResiduePoolEntry(entry)}
                                  disabled={cardDisabled}
                                  aria-pressed={active}
                                  title={[entry.label, entry.ccd, entry.backboneLabel, placementStatus?.placement].filter(Boolean).join(' · ')}
                                >
                                  {helpText ? (
                                    <span className="peptide-residue-help" aria-label={helpText} onClick={(event) => event.stopPropagation()}>
                                      ?
                                      <span className="peptide-residue-tooltip">{helpText}</span>
                                    </span>
                                  ) : null}
                                  <div className="peptide-residue-preview" aria-hidden="true">
                                    {entry.smiles ? (
                                      <MemoLigand2DPreview smiles={entry.smiles} width={132} height={94} highlightQuery={entry.backboneHighlightQuery || AMINO_ACID_BACKBONE_SMARTS} />
                                    ) : (
                                      <div className="peptide-residue-preview-fallback">{entry.baseResidue}</div>
                                    )}
                                  </div>
                                  <div className="peptide-residue-meta">
                                    <span className="peptide-residue-code">{entry.baseResidue}</span>
                                    <span className="peptide-residue-name">{entry.label}</span>
                                    <span className="peptide-residue-ccd">{entry.ccd}</span>
                                  </div>
                                  {kind === 'custom' ? (
                                    <span className="peptide-residue-card-actions" onClick={(event) => event.stopPropagation()}>
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-compact"
                                        disabled={residuePoolControlsDisabled}
                                        onClick={() => {
                                          const libraryEntry = peptideCustomResidueLibrary.find((item) => normalizeCustomResidueCode(item.ccd) === entry.ccd);
                                          openCustomResidueEditor(libraryEntry);
                                        }}
                                      >
                                        Edit
                                      </button>
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-compact danger"
                                        disabled={residuePoolControlsDisabled}
                                        onClick={() => deleteCustomResidue(entry.ccd)}
                                      >
                                        Delete
                                      </button>
                                    </span>
                                  ) : null}
                                </button>
                              );
                            })}
                          </div>
                        ) : (
                          <div className="peptide-residue-section-empty">
                            {section.kind === 'custom' ? 'Add a custom residue to show it here.' : 'No saved residues.'}
                          </div>
                        )}
                        </section>
                      );
                    })}
                  </div>
                  )}
                </div>
                <CustomResidueEditorModal
                  isOpen={customEditorOpen}
                  userId={currentUserId ?? 'anon'}
                  editingCcd={customEditingCcd}
                  isDisabled={residuePoolControlsDisabled}
                  draftSmiles={customDraftSmiles}
                  draftName={customDraftName}
                  draftBaseResidue={customDraftBaseResidue}
                  draftBackbone={customDraftBackbone}
                  isDraftAmidated={customDraftAmidated}
                  isDraftValid={customDraftValid}
                  autoStatus={customDraftAutoStatus}
                  slotErrors={customDraftSlotErrors}
                  armedSlot={armedBackboneSlot}
                  assignedIndices={assignedBackboneIndices}
                  backboneAtomLabels={backboneAtomLabels}
                  onAtomClick={handleBackboneAtomClick}
                  onResetBackbone={resetBackboneToAuto}
                  highlightColors={backboneHighlightColorOverride}
                  onSmilesChange={setCustomDraftSmiles}
                  onNameChange={setCustomDraftName}
                  onBaseResidueChange={setCustomDraftBaseResidue}
                  onAmidatedChange={setCustomDraftAmidated}
                  onArmSlot={(v) => setArmedBackboneSlot(v as typeof armedBackboneSlot)}
                  onSave={saveCustomResidueDraft}
                  onClose={closeCustomResidueEditor}
                />
                <div className="peptide-preset-row" role="group" aria-label="Sampling effort presets">
                  {PEPTIDE_EFFORT_PRESETS.map((preset) => {
                    const isActive =
                      peptideIterations === preset.iterations
                      && peptidePopulationSize === preset.populationSize
                      && peptideEliteSize === preset.eliteSize;
                    return (
                      <button
                        key={preset.id}
                        type="button"
                        className={`peptide-preset-chip${isActive ? ' active' : ''}`}
                        disabled={!isEditable}
                        title={`${preset.iterations} generations × ${preset.populationSize} candidates (elite ${preset.eliteSize}) ≈ ${preset.iterations * preset.populationSize} predictions`}
                        onClick={() => {
                          onPeptideIterationsChange(preset.iterations);
                          onPeptidePopulationSizeChange(preset.populationSize);
                          onPeptideEliteSizeChange(preset.eliteSize);
                        }}
                      >
                        {preset.label}
                      </button>
                    );
                  })}
                </div>
                                  <Field
                  label="Iterations"
                  hint="Evolution generations. Each generation evaluates one population of candidates and breeds the next from the elites."
                >
                  <CommitNumberInput
                  min={2}
                  max={100}
                  value={peptideIterations}
                  onCommit={onPeptideIterationsChange}
                  isDisabled={!isEditable}
                  />
                  </Field>
                                  <Field
                  label="Population Size"
                  hint="Candidates evaluated per generation. Larger populations explore more sequence space per generation."
                >
                  <CommitNumberInput
                  min={2}
                  max={100}
                  value={peptidePopulationSize}
                  onCommit={onPeptidePopulationSizeChange}
                  isDisabled={!isEditable}
                  />
                  </Field>
                                  <Field
                  label="Elite Size"
                  hint="Top candidates carried into the next generation's breeding pool."
                >
                  <CommitNumberInput
                  min={1}
                  max={Math.max(1, peptidePopulationSize - 1)}
                  value={peptideEliteSize}
                  onCommit={onPeptideEliteSizeChange}
                  isDisabled={!isEditable}
                  />
                  </Field>
                <p className="muted small">
                  ≈ {peptideIterations * peptidePopulationSize} candidate predictions total
                  (generations × population).
                </p>
                <label className="field peptide-mask-field">
                  <span>Fixed positions</span>
                  <input
                    className="peptide-fixed-reference-input"
                    type="text"
                    value={normalizedInitialSequence}
                    onChange={(e) => onPeptideInitialSequenceChange(e.target.value)}
                    disabled={!isEditable}
                    placeholder={lengthLocked ? `Reference sequence, length ${effectiveDesignLength}` : `Reference sequence, up to ${effectiveDesignLength} aa (design range ${peptideLengthMin}–${peptideLengthMax})`}
                    spellCheck={false}
                  />
                  <div className="peptide-mask-rail" role="list" aria-label="Sequence mask positions">
                    {positions.map((position) => {
                      const residue = maskChars[position - 1] || 'X';
                      const fixed = residue !== 'X';
                      const referenceResidue = normalizedInitialSequence[position - 1] || '';
                      const canFixPosition = Boolean(referenceResidue && referenceResidue !== 'X');
                      return (
                        <button
                          key={`peptide-mask-${position}`}
                          type="button"
                          role="listitem"
                          className={`peptide-mask-dot ${fixed ? 'fixed' : ''} ${!canFixPosition ? 'empty' : ''}`}
                          onClick={() => toggleMaskPosition(position)}
                          disabled={!isEditable || !canFixPosition}
                          title={
                            fixed
                              ? `Position ${position} fixed at ${residue}`
                              : canFixPosition
                                ? `Click to fix position ${position} at ${referenceResidue}`
                                : `Add residue ${position} in the reference sequence before fixing it`
                          }
                        >
                          <span>{position}</span>
                          <strong>{fixed ? residue : canFixPosition ? referenceResidue : '·'}</strong>
                        </button>
                      );
                    })}
                  </div>
                </label>
              </div>
              {normalizedInitialSequence.length !== effectiveDesignLength && (
                <p className="muted small">
                  Reference sequence length is {normalizedInitialSequence.length}. Expected {effectiveDesignLength} to fix every desired position.
                </p>
              )}
            </section>

            {isBicyclicMode && (
              <section className="peptide-runtime-group peptide-runtime-group-bicyclic">
                <div className="peptide-runtime-group-headline">
                  <div className="peptide-runtime-group-head">
                    Bicyclic Specific
                    <InfoTip text="Linker and cysteine topology for bicyclic designs." align="start" />
                  </div>
                  <span className="peptide-runtime-chip">Bicyclic</span>
                </div>
                <div className="peptide-bicyclic-layout">
                  <div className="field peptide-linker-field peptide-linker-field-compact">
                    <span>Linker Type</span>
                    <div className="peptide-linker-gallery peptide-linker-gallery-compact">
                      {BICYCLIC_LINKERS.map((linker) => (
                        <button
                          key={linker.type}
                          type="button"
                          className={`peptide-linker-card ${peptideBicyclicLinkerCcd === linker.type ? 'active' : ''}`}
                          onClick={() => onPeptideBicyclicLinkerCcdChange(linker.type)}
                          disabled={!isEditable}
                          aria-pressed={peptideBicyclicLinkerCcd === linker.type}
                          aria-label={`Select ${linker.type} linker`}
                          title={`${linker.type} · ${linker.smiles}`}
                        >
                          <div className="peptide-linker-card-preview" aria-hidden="true">
                            <MemoLigand2DPreview smiles={linker.smiles} width={208} height={158} />
                          </div>
                          <div className="peptide-linker-card-name">{linker.name}</div>
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="peptide-bicyclic-divider" aria-hidden="true" />

                  <div className="peptide-bicyclic-main">
                    <div className="peptide-bicyclic-top-control">
                      <div className="field">
                        <span>
                          Cys Layout
                          <InfoTip
                            text="How the three Cys anchors follow the peptide length range."
                            align="start"
                          />
                        </span>
                        <div className="peptide-cys-mode-seg" role="group" aria-label="Cys layout mode">
                          {([
                            { key: 'auto' as CysLayoutMode, label: 'Auto', title: 'Engine places Cys at first / middle / last of every candidate.' },
                            { key: 'ring' as CysLayoutMode, label: 'Ring sizes', title: 'Pin the two ring sizes - the anchor block rides the C-terminus; the N-flank absorbs the length range.' },
                            { key: 'ratio' as CysLayoutMode, label: 'Ratio', title: 'Anchors scale with each candidate length; ring sizes flex.' },
                            { key: 'absolute' as CysLayoutMode, label: 'Absolute', title: 'Literal positions - requires a fixed length (min = max).' }
                          ]).map((mode) => (
                            <button
                              key={mode.key}
                              type="button"
                              className={`peptide-cys-mode-btn ${peptideBicyclicCysLayout === mode.key ? 'active' : ''}`}
                              onClick={() => onPeptideBicyclicCysLayoutChange(mode.key)}
                              disabled={!isEditable}
                              aria-pressed={peptideBicyclicCysLayout === mode.key}
                              title={mode.title}
                            >
                              {mode.label}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>
                    <div className="peptide-runtime-grid peptide-runtime-grid-controls">
                      <label className="switch-field peptide-runtime-switch">
                        <input
                          type="checkbox"
                          checked={isPeptideBicyclicFixTerminalCys}
                          onChange={(e) => onPeptideBicyclicFixTerminalCysChange(e.target.checked)}
                          disabled={!isEditable || cysPositionAuto || peptideBicyclicCysLayout === 'ring'}
                        />
                        <span>Fix Terminal Cys</span>
                      </label>
                      <label className="switch-field peptide-runtime-switch">
                        <input
                          type="checkbox"
                          checked={isPeptideBicyclicIncludeExtraCys}
                          onChange={(e) => onPeptideBicyclicIncludeExtraCysChange(e.target.checked)}
                          disabled={!isEditable}
                        />
                        <span>Allow Extra Cys</span>
                      </label>
                    </div>

                    {peptideBicyclicCysLayout === 'ring' && (
                      <div className="peptide-cys-params">
                        <label className="field peptide-cys-param">
                          <span>Ring 1 <em>residues between Cys 1-2</em></span>
                          <CommitNumberInput
                            min={1}
                            max={40}
                            value={peptideBicyclicRing1}
                            onCommit={(v) => onPeptideBicyclicRingChange(v, peptideBicyclicRing2)}
                            disabled={!isEditable}
                          />
                        </label>
                        <label className="field peptide-cys-param">
                          <span>Ring 2 <em>residues between Cys 2-3</em></span>
                          <CommitNumberInput
                            min={1}
                            max={40}
                            value={peptideBicyclicRing2}
                            onCommit={(v) => onPeptideBicyclicRingChange(peptideBicyclicRing1, v)}
                            disabled={!isEditable}
                          />
                        </label>
                      </div>
                    )}

                    {peptideBicyclicCysLayout === 'ratio' && (
                      <div className="peptide-cys-params peptide-cys-params-ratio">
                        {([
                          { key: 1, label: 'Cys 1', value: peptideBicyclicRatio1, pct3: undefined },
                          { key: 2, label: 'Cys 2', value: peptideBicyclicRatio2, pct3: undefined },
                          ...(isPeptideBicyclicFixTerminalCys
                            ? []
                            : [{ key: 3, label: 'Cys 3', value: peptideBicyclicRatio3, pct3: peptideBicyclicRatio3 }])
                        ]).map((slider) => (
                          <label className="field peptide-cys-param peptide-ratio-slider-field" key={slider.key}>
                            <span>{slider.label} <em>at % of peptide length</em></span>
                            <div className="peptide-ratio-slider-row">
                              <input
                                type="range"
                                min={0}
                                max={100}
                                value={slider.value}
                                onChange={(e) => {
                                  const v = Math.floor(Number(e.target.value) || 0);
                                  if (slider.key === 1) onPeptideBicyclicRatioChange(v, peptideBicyclicRatio2, slider.pct3);
                                  else if (slider.key === 2) onPeptideBicyclicRatioChange(peptideBicyclicRatio1, v, slider.pct3);
                                  else onPeptideBicyclicRatioChange(peptideBicyclicRatio1, peptideBicyclicRatio2, v);
                                }}
                                disabled={!isEditable}
                              />
                              <strong>{slider.value}%</strong>
                            </div>
                          </label>
                        ))}
                      </div>
                    )}

                    {/* the spectrum only adds information across a multi-length range */}
                    {!cysPositionAuto && peptideLengthMin !== peptideLengthMax && (
                      <PeptideCysSpectrum
                        lengthMin={Math.min(peptideLengthMin, peptideLengthMax)}
                        lengthMax={Math.max(peptideLengthMin, peptideLengthMax)}
                        resolve={spectrumResolver}
                        referenceLength={cysReferenceLength}
                      />
                    )}

                    <div className={`peptide-runtime-grid peptide-runtime-grid-cys ${cysPositionAuto ? 'is-disabled' : ''}`}>
                      <div className="field peptide-cys-picker-field">
                        <span>
                          {peptideBicyclicCysLayout === 'absolute'
                            ? 'Cys Positions'
                            : `Cys Positions - preview at ${cysReferenceLength} aa`}
                        </span>
                        <div className="peptide-cys-slot-tabs" role="tablist" aria-label="Cysteine slots">
                          {([
                            { key: 'cys1' as CysSlot, label: 'Cys 1' },
                            { key: 'cys2' as CysSlot, label: 'Cys 2' },
                            { key: 'cys3' as CysSlot, label: 'Cys 3' }
                          ]).map((slot) => {
                            const disabled = peptideBicyclicCysLayout === 'absolute'
                              && slot.key === 'cys3' && isPeptideBicyclicFixTerminalCys;
                            const assigned = cysSlotValueMap[slot.key];
                            return (
                              <button
                                key={slot.key}
                                type="button"
                                className={`peptide-cys-slot-tab ${
                                  activeCysSlot === slot.key ? 'active' : ''
                                } ${slot.key}`}
                                onClick={() => setActiveCysSlot(slot.key)}
                                disabled={disabled}
                                title={disabled ? 'Cys 3 is fixed to terminal residue.' : ''}
                              >
                                <span>{slot.label}</span>
                                <strong>{assigned}</strong>
                              </button>
                            );
                          })}
                        </div>
                        <div className="peptide-position-rail" role="list" aria-label="Peptide positions">
                          {positions.map((position) => {
                            const marks: CysSlot[] = [];
                            if (cysSlotValueMap.cys1 === position) marks.push('cys1');
                            if (cysSlotValueMap.cys2 === position) marks.push('cys2');
                            if (cysSlotValueMap.cys3 === position) marks.push('cys3');
                            const markClass = marks.length > 0 ? marks[0] : '';
                            const disabledByRange = position > cysSlotMaxMap[activeCysSlot];
                            const disabledByFixedCys3 = activeCysSlot === 'cys3' && isPeptideBicyclicFixTerminalCys;
                            const previewOnly = peptideBicyclicCysLayout !== 'absolute';
                            const disabled = !isEditable || cysPositionAuto || previewOnly || disabledByRange || disabledByFixedCys3;
                            return (
                              <button
                                key={`peptide-position-${position}`}
                                type="button"
                                role="listitem"
                                className={`peptide-position-dot ${markClass} ${
                                  marks.includes(activeCysSlot) ? 'active-slot' : ''
                                }`}
                                onClick={() => assignCysPosition(activeCysSlot, position)}
                                disabled={disabled}
                                title={marks.length > 0 ? `Assigned: ${marks.join(', ')}` : `Position ${position}`}
                              >
                                {position}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                    {cysLayoutError && (
                      <p className="peptide-cys-feedback is-error" role="alert">
                        {cysLayoutError}
                        {peptideBicyclicCysLayout === 'absolute' && rangeIsOpen && (
                          <button
                            type="button"
                            className="btn btn-ghost btn-compact peptide-cys-pin-length"
                            onClick={() => onPeptideLengthRange(cysReferenceLength, cysReferenceLength)}
                            disabled={!isEditable}
                          >
                            Pin length to {cysReferenceLength} aa
                          </button>
                        )}
                      </p>
                    )}
                    {!cysLayoutError && cysLayoutNotice && (
                      <p className="peptide-cys-feedback is-notice">{cysLayoutNotice}</p>
                    )}
                    {cysPositionAuto && (
                      <p className="muted small">Auto mode will optimize Cys positions during design.</p>
                    )}
                    {peptideBicyclicCysLayout === 'ring' && (
                      <p className="muted small">
                        Ring sizes stay fixed at every candidate length; the N-terminal flank absorbs the range.
                      </p>
                    )}
                    {peptideBicyclicCysLayout === 'ratio' && (
                      <p className="muted small">
                        Cys positions scale with each candidate length; ring sizes flex between candidates.
                      </p>
                    )}
                    {!cysPositionAuto && isPeptideBicyclicFixTerminalCys && peptideBicyclicCysLayout !== 'ring' && (
                      <p className="muted small">Cys 3 is anchored to terminal residue.</p>
                    )}
                  </div>
                </div>
              </section>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
