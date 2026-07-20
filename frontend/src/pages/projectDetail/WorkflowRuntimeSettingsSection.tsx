import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FocusEvent } from 'react';
import { MemoLigand2DPreview } from '../../components/project/Ligand2DPreview';
import { JSMEEditor } from '../../components/project/JSMEEditor';
import { buildCustomResidueCatalog, BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES, type ResidueCatalogEntry } from '../../components/project/residueCatalog';
import { AMINO_ACID_BACKBONE_SMARTS, rdkitMolHasAminoAcidBackbone } from '../../utils/inputValidation';
import { loadRDKitModule } from '../../utils/rdkit';
import type { CustomCcdMoleculeInput, CustomResidueBackbone, PeptideResiduePoolSelection } from '../../types/models';
import { normalizePredictionBackend } from './projectDraftUtils';
import { detectCustomResidueBackbone, firstBackboneSlotError, validateBackboneSlots, validateCustomResidueBackbone, type BackboneSlotErrors } from '../../utils/constraintAtomOptions';
import { toggleTerminalAmide } from '../../utils/smilesTransform';
import { useAuth } from '../../hooks/useAuth';

type CysSlot = 'cys1' | 'cys2' | 'cys3';
type BicyclicLinkerType = 'SEZ' | '29N' | 'BS3';

// Canonical CCD SMILES used for RDKit previews.
const BICYCLIC_LINKERS: Array<{ type: BicyclicLinkerType; name: string; smiles: string }> = [
  { type: 'SEZ', name: '1,3,5-Trimethylbenzene', smiles: 'Cc1cc(C)cc(C)c1' },
  { type: '29N', name: 'Triazinane linker', smiles: 'CC(=O)CCN1CN(CC(=O)CC)CN(CC(=O)CC)C1' },
  { type: 'BS3', name: 'Bi(III) center', smiles: '[Bi+3]' }
];

const CUSTOM_RESIDUE_SCAFFOLD_SMILES = 'N[C@H](C(=O)O)c1ccccc1';

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

// System-generated CCD code for a NEW custom residue: deterministic per (user, SMILES) so the
// same residue has one stable identity under its owner and never collides across users in a
// shared project. Users cannot author it. Runtime user CCDs override built-ins, so the code
// only needs to be unique + stable, not avoid the real-CCD namespace. Existing residues keep
// their original codes — no migration.
function generateCustomResidueCode(userId: string | null | undefined, smiles: string): string {
  const input = `${String(userId || 'anon').trim()}${String(smiles || '').trim()}`;
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const fnv1a = (seed: number) => {
    let hash = seed >>> 0;
    for (let index = 0; index < input.length; index += 1) {
      hash ^= input.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return hash >>> 0;
  };
  const left = fnv1a(0x811c9dc5);
  const right = fnv1a(0x9e3779b9);
  let code = 'U';
  let a = left;
  let b = right;
  for (let index = 0; index < 3; index += 1) {
    code += alphabet[a % 36];
    a = Math.floor(a / 36);
    code += alphabet[b % 36];
    b = Math.floor(b / 36);
  }
  return code; // U + 6 alphanumeric chars
}

const CUSTOM_BACKBONE_SLOTS = ['n', 'ca', 'c', 'o', 'oxt'] as const;
const CUSTOM_BACKBONE_SLOT_LABELS: Record<(typeof CUSTOM_BACKBONE_SLOTS)[number], string> = {
  n: 'N',
  ca: 'CA',
  c: 'C',
  o: 'O',
  oxt: 'OXT'
};

function clampCommittedNumber(value: number, minValue: number, maxValue: number, fallback: number, step?: number): number {
  const parsed = Number.isFinite(value) ? value : fallback;
  const clamped = Math.max(minValue, Math.min(maxValue, parsed));
  if (step && step > 0) return Number((Math.round(clamped / step) * step).toFixed(6));
  return Math.floor(clamped);
}

function CommitNumberInput({
  value,
  min,
  max,
  step,
  disabled,
  onCommit
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  disabled?: boolean;
  onCommit: (value: number) => void;
}) {
  const [draftValue, setDraftValue] = useState(String(value));

  useEffect(() => {
    setDraftValue(String(value));
  }, [value]);

  const commit = (rawValue: string) => {
    const next = clampCommittedNumber(Number(rawValue), min, max, value, step);
    setDraftValue(String(next));
    if (next !== value) onCommit(next);
  };

  return (
    <input
      type="number"
      min={min}
      max={max}
      step={step}
      value={draftValue}
      onChange={(event: ChangeEvent<HTMLInputElement>) => setDraftValue(event.target.value)}
      onBlur={(event: FocusEvent<HTMLInputElement>) => commit(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== 'Enter') return;
        commit(event.currentTarget.value);
        event.currentTarget.blur();
      }}
      disabled={disabled}
    />
  );
}


export interface WorkflowRuntimeSettingsSectionProps {
  visible: boolean;
  displayMode?: 'full' | 'peptide_mode_only';
  canEdit: boolean;
  isPredictionWorkflow: boolean;
  isPeptideDesignWorkflow: boolean;
  isAffinityWorkflow: boolean;
  backend: string;
  seed: number | null;
  lowVram: boolean;
  peptideDesignMode: 'linear' | 'cyclic' | 'bicyclic';
  peptideBinderLength: number;
  peptideUseInitialSequence: boolean;
  peptideInitialSequence: string;
  peptideSequenceMask: string;
  peptideIterations: number;
  peptidePopulationSize: number;
  peptideEliteSize: number;
  peptideMutationRate: number;
  peptideResiduePool: PeptideResiduePoolSelection[];
  peptideResiduePoolAvailable?: boolean;
  peptideNonNaturalMin: number;
  peptideNonNaturalMax: number;
  peptideCustomResidueLibrary: CustomCcdMoleculeInput[];
  onCustomResidueLibraryChange: (library: CustomCcdMoleculeInput[]) => void;
  peptideBicyclicLinkerCcd: BicyclicLinkerType;
  peptideBicyclicCysPositionMode: 'auto' | 'manual';
  peptideBicyclicFixTerminalCys: boolean;
  peptideBicyclicIncludeExtraCys: boolean;
  peptideBicyclicCys1Pos: number;
  peptideBicyclicCys2Pos: number;
  peptideBicyclicCys3Pos: number;
  onBackendChange: (backend: string) => void;
  onSeedChange: (seed: number | null) => void;
  onLowVramChange: (lowVram: boolean) => void;
  onPeptideDesignModeChange: (mode: 'linear' | 'cyclic' | 'bicyclic') => void;
  onPeptideBinderLengthChange: (value: number) => void;
  onPeptideUseInitialSequenceChange: (value: boolean) => void;
  onPeptideInitialSequenceChange: (value: string) => void;
  onPeptideSequenceMaskChange: (value: string) => void;
  onPeptideIterationsChange: (value: number) => void;
  onPeptidePopulationSizeChange: (value: number) => void;
  onPeptideEliteSizeChange: (value: number) => void;
  onPeptideMutationRateChange: (value: number) => void;
  onPeptideResiduePoolChange: (value: PeptideResiduePoolSelection[]) => void;
  onPeptideNonNaturalRangeChange: (min: number, max: number) => void;
  onPeptideBicyclicLinkerCcdChange: (value: BicyclicLinkerType) => void;
  onPeptideBicyclicCysPositionModeChange: (value: 'auto' | 'manual') => void;
  onPeptideBicyclicFixTerminalCysChange: (value: boolean) => void;
  onPeptideBicyclicIncludeExtraCysChange: (value: boolean) => void;
  onPeptideBicyclicCys1PosChange: (value: number) => void;
  onPeptideBicyclicCys2PosChange: (value: number) => void;
  onPeptideBicyclicCys3PosChange: (value: number) => void;
}

export function WorkflowRuntimeSettingsSection({
  visible,
  displayMode = 'full',
  canEdit,
  isPredictionWorkflow,
  isPeptideDesignWorkflow,
  isAffinityWorkflow,
  backend,
  seed,
  lowVram,
  peptideDesignMode,
  peptideBinderLength,
  peptideUseInitialSequence,
  peptideInitialSequence,
  peptideSequenceMask,
  peptideIterations,
  peptidePopulationSize,
  peptideEliteSize,
  peptideMutationRate,
  peptideResiduePool,
  peptideResiduePoolAvailable = true,
  peptideNonNaturalMin,
  peptideNonNaturalMax,
  peptideCustomResidueLibrary,
  onCustomResidueLibraryChange,
  peptideBicyclicLinkerCcd,
  peptideBicyclicCysPositionMode,
  peptideBicyclicFixTerminalCys,
  peptideBicyclicIncludeExtraCys,
  peptideBicyclicCys1Pos,
  peptideBicyclicCys2Pos,
  peptideBicyclicCys3Pos,
  onBackendChange,
  onSeedChange,
  onLowVramChange,
  onPeptideDesignModeChange,
  onPeptideBinderLengthChange,
  onPeptideUseInitialSequenceChange,
  onPeptideInitialSequenceChange,
  onPeptideSequenceMaskChange,
  onPeptideIterationsChange,
  onPeptidePopulationSizeChange,
  onPeptideEliteSizeChange,
  onPeptideMutationRateChange,
  onPeptideResiduePoolChange,
  onPeptideNonNaturalRangeChange,
  onPeptideBicyclicLinkerCcdChange,
  onPeptideBicyclicCysPositionModeChange,
  onPeptideBicyclicFixTerminalCysChange,
  onPeptideBicyclicIncludeExtraCysChange,
  onPeptideBicyclicCys1PosChange,
  onPeptideBicyclicCys2PosChange,
  onPeptideBicyclicCys3PosChange
}: WorkflowRuntimeSettingsSectionProps) {
  const { session } = useAuth();
  const currentUserId = session?.userId ?? null;
  const [activeCysSlot, setActiveCysSlot] = useState<CysSlot>('cys1');
  const [customEditorOpen, setCustomEditorOpen] = useState(false);
  const [customEditingCcd, setCustomEditingCcd] = useState('');
  const [customDraftName, setCustomDraftName] = useState('Custom residue');
  const [customDraftBaseResidue, setCustomDraftBaseResidue] = useState('A');
  const [customDraftSmiles, setCustomDraftSmiles] = useState(CUSTOM_RESIDUE_SCAFFOLD_SMILES);
  const [customDraftValid, setCustomDraftValid] = useState(false);
  // Manual backbone atom slots (0-based heavy-atom indices). Auto-prefilled from RDKit when the
  // SMILES validates; the user corrects by clicking atoms in the 2D. Saved on the residue as its
  // `backbone` and used by the backend as-is.
  const [customDraftBackbone, setCustomDraftBackbone] = useState<Partial<CustomResidueBackbone>>({});
  const [customDraftAmidated, setCustomDraftAmidated] = useState(false);
  // 'failed' surfaces why an explicit Auto run found no backbone (e.g. a C-terminal amide without
  // the amidation flag): the manual picks are kept and the reason is shown instead of a silent wipe.
  const [customDraftAutoStatus, setCustomDraftAutoStatus] = useState<'idle' | 'failed'>('idle');
  // Per-slot errors for the manual backbone override (empty = valid). Mirrors the protein editor
  // in ComponentInputEditor; blocks Save and surfaces inline. Never silently passes a wrong set.
  const [customDraftSlotErrors, setCustomDraftSlotErrors] = useState<BackboneSlotErrors>({});
  const [armedBackboneSlot, setArmedBackboneSlot] = useState<(typeof CUSTOM_BACKBONE_SLOTS)[number] | null>(null);
  const skipBackboneAutoDetectRef = useRef(false);
  const prevCustomDraftAmidatedRef = useRef(customDraftAmidated);
  // True once the user has clicked any atom. While set, SMILES edits validate the picks against
  // the new structure instead of auto-detecting over them. Cleared by Auto and on opening a residue.
  const manualOverrideBackboneRef = useRef(false);
  const showFullFields = displayMode === 'full';
  const normalizedBackend = isAffinityWorkflow ? 'boltz' : normalizePredictionBackend(backend);
  const canEditRuntimeIdentity = canEdit || isPredictionWorkflow || isPeptideDesignWorkflow || isAffinityWorkflow;
  const isBicyclicMode = isPeptideDesignWorkflow && peptideDesignMode === 'bicyclic';
  const cys2Max = peptideBicyclicFixTerminalCys
    ? Math.max(1, peptideBinderLength - 2)
    : Math.max(1, peptideBinderLength - 1);
  const cysPositionAuto = peptideBicyclicCysPositionMode === 'auto';
  const hasDuplicatedCysPositions =
    isBicyclicMode &&
    !cysPositionAuto &&
    new Set([peptideBicyclicCys1Pos, peptideBicyclicCys2Pos, peptideBicyclicCys3Pos]).size < 3;
  const cysSlotValueMap = useMemo(
    () => ({
      cys1: peptideBicyclicCys1Pos,
      cys2: peptideBicyclicCys2Pos,
      cys3: peptideBicyclicFixTerminalCys ? peptideBinderLength : peptideBicyclicCys3Pos
    }),
    [
      peptideBicyclicCys1Pos,
      peptideBicyclicCys2Pos,
      peptideBicyclicCys3Pos,
      peptideBicyclicFixTerminalCys,
      peptideBinderLength
    ]
  );
  const cysSlotMaxMap = useMemo(
    () => ({
      cys1: Math.max(1, peptideBinderLength - 2),
      cys2: cys2Max,
      cys3: peptideBinderLength
    }),
    [peptideBinderLength, cys2Max]
  );
  const positions = useMemo(
    () => Array.from({ length: Math.max(1, peptideBinderLength) }, (_, idx) => idx + 1),
    [peptideBinderLength]
  );
  const normalizedInitialSequence = useMemo(
    () =>
      String(peptideInitialSequence || '')
        .replace(/[\s_-]/g, '')
        .toUpperCase()
        .slice(0, peptideBinderLength),
    [peptideInitialSequence, peptideBinderLength]
  );
  const normalizedSequenceMask = useMemo(() => {
    const normalized = String(peptideSequenceMask || '')
      .replace(/[\s_-]/g, '')
      .toUpperCase()
      .replace(/[^ARNDCQEGHILKMFPSTWYVX]/g, '')
      .slice(0, peptideBinderLength);
    if (!normalized) return 'X'.repeat(Math.max(1, peptideBinderLength));
    return normalized.padEnd(Math.max(1, peptideBinderLength), 'X');
  }, [peptideSequenceMask, peptideBinderLength]);
  const maskChars = useMemo(() => normalizedSequenceMask.split(''), [normalizedSequenceMask]);
  const canToggleMask = canEdit;

  useEffect(() => {
    let cancelled = false;
    const amidatedChanged = prevCustomDraftAmidatedRef.current !== customDraftAmidated;
    prevCustomDraftAmidatedRef.current = customDraftAmidated;
    // Debounce so drawing in JSME (many SMILES changes in a row) doesn't flicker the picks.
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
          // Amidation toggle re-canonicalizes the SMILES and flips the terminal element; re-detect
          // keeping the user's N/CA/C/O picks.
          if (amidatedChanged) {
            skipBackboneAutoDetectRef.current = false;
            const anchors = manualOverrideBackboneRef.current ? customDraftBackbone : {};
            setCustomDraftBackbone(detectCustomResidueBackbone(rdkit, smiles, anchors, customDraftAmidated) ?? {});
            setCustomDraftAutoStatus('idle');
          } else if (manualOverrideBackboneRef.current) {
            // The user picked atoms: validate them against the edited structure; never auto-detect
            // over them. Keep picks still on the right element, drop those whose atom shifted.
            const kept = validateBackboneSlots(rdkit, smiles, customDraftBackbone, customDraftAmidated);
            if (CUSTOM_BACKBONE_SLOTS.some((slot) => kept[slot] !== customDraftBackbone[slot])) {
              setCustomDraftBackbone(kept);
            }
          } else if (skipBackboneAutoDetectRef.current) {
            skipBackboneAutoDetectRef.current = false;
          } else if (valid) {
            // Fresh auto-detection as a starting suggestion (user hasn't intervened).
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

  // Recompute backbone slot errors whenever picks/structure/amidation change. A complete-but-wrong
  // override is reported and blocks Save; an incomplete set yields no error (backend auto-detects).
  // Never silently passes a wrong assignment.
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
        // RDKit not warmed yet; leave the previous verdict rather than silently passing.
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

  // Arm a slot, then click an atom in the 2D to assign it. Clicking the atom already in the
  // armed slot clears it; an atom already used by another slot is moved. After assigning, the
  // next unfilled slot is armed automatically (or null once all five are set).
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
    // Re-detect with the current picks as anchors: atoms the user already set stay; only the
    // empty slots are filled.
    const detected = detectCustomResidueBackbone(rdkit, customDraftSmiles.trim(), customDraftBackbone, customDraftAmidated);
    if (detected) {
      setCustomDraftBackbone(detected);
      setCustomDraftAutoStatus('idle');
    } else {
      // Keep the manual picks and explain, rather than silently wiping them.
      setCustomDraftAutoStatus('failed');
    }
    setArmedBackboneSlot(null);
  };

  // Derived display values for the 2D: highlight the assigned backbone atoms and label them
  // with their slot letter so the user sees exactly which atom is N/CA/C/O/OXT.
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

  // A custom pool entry carries its own CCD SMILES (read straight off the catalog entry,
  // which is the single merged source) so the definition persists with the selection in
  // the config and reaches the backends as a CCD.
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

  // Persist the drawn SMILES onto the selection itself: if a custom pool entry lacks a
  // SMILES but the residue library (where it was drawn) has one, write it into the pool
  // entry once. After this the pool entry is self-contained and survives reload; the
  // submit path reads only the pool entry (no runtime fallback).
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
    if (peptideResiduePoolAvailable && selected.size === 0) {
      NATURAL_AMINO_ACID_RESIDUES.forEach((item) => selected.add(`natural:${item.ccd}`));
    }
    return selected;
  }, [peptideResiduePool, peptideResiduePoolAvailable]);
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
  const residuePoolControlsDisabled = !canEdit;
  const protectedResiduePositions = useMemo(() => {
    const protectedSet = new Set<number>();
    maskChars.forEach((maskChar, idx) => {
      if (maskChar && maskChar !== 'X') protectedSet.add(idx + 1);
    });
    if (isBicyclicMode) {
      Object.values(cysSlotValueMap).forEach((pos) => {
        const normalized = Math.max(1, Math.min(peptideBinderLength, Math.floor(Number(pos) || 1)));
        protectedSet.add(normalized);
      });
    }
    return protectedSet;
  }, [maskChars, isBicyclicMode, cysSlotValueMap, peptideBinderLength]);
  const residuePlacementStatusByKey = useMemo(() => {
    const status = new Map<string, { selectable: boolean; allowedPositions: number[]; reason: string; placement: string }>();
    residueCatalogSections.forEach((section) => {
      section.entries.forEach((entry) => {
        const key = `${section.kind}:${entry.ccd}`;
        const rule = residuePlacementRule(entry);
        let candidatePositions = positions;
        if (rule === 'n_term') candidatePositions = positions.filter((position) => position === 1);
        if (rule === 'c_term') candidatePositions = positions.filter((position) => position === peptideBinderLength);
        if (rule === 'terminal') candidatePositions = positions.filter((position) => position === 1 || position === peptideBinderLength);
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
          if (rule === 'c_term' && protectedResiduePositions.has(peptideBinderLength)) reason = `${placement}; the C-terminal position is fixed by the sequence mask.`;
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
  }, [residueCatalogSections, positions, peptideBinderLength, protectedResiduePositions, isBicyclicMode]);
  const clampNonNaturalLimit = (value: number) => Math.max(0, Math.min(peptideBinderLength, Math.floor(Number(value) || 0)));
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
    // Existing residues keep their frozen code; new residues get a system-generated code
    // (deterministic per user + SMILES). Users never author the CCD code.
    const ccd = customEditingCcd || normalizeCustomResidueCode(generateCustomResidueCode(currentUserId, smiles));
    if (!ccd) return;
    const baseResidue = customDraftBaseResidue.trim().toUpperCase().slice(0, 1) || undefined;
    const label = customDraftName.trim() || 'Custom residue';
    // The backbone is saved only when all 5 slots are set; otherwise it is omitted and the
    // backend auto-detects.
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
    // The freshly drawn residue is the source of truth for its own SMILES; every other
    // custom residue keeps the SMILES already on its catalog entry.
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
    if (!canEdit || cysPositionAuto) return;
    if (slot === 'cys1') {
      onPeptideBicyclicCys1PosChange(position);
      return;
    }
    if (slot === 'cys2') {
      onPeptideBicyclicCys2PosChange(position);
      return;
    }
    if (peptideBicyclicFixTerminalCys) return;
    onPeptideBicyclicCys3PosChange(position);
  };

  const toggleMaskPosition = (position: number) => {
    if (!canToggleMask) return;
    const index = position - 1;
    if (index < 0 || index >= maskChars.length) return;
    const sequenceChar = normalizedInitialSequence[index] || '';
    if (!sequenceChar || sequenceChar === 'X') return;
    const nextMask = [...maskChars];
    nextMask[index] = nextMask[index] === 'X' ? sequenceChar : 'X';
    onPeptideSequenceMaskChange(nextMask.join(''));
  };

  if (!visible) return null;

  return (
    <section className="panel subtle component-runtime-settings">
      <div className="component-runtime-settings-row">
        {showFullFields && (
          <label className="field">
            <span>
              Backend <span className="required-mark">*</span>
            </span>
            <select
              required
              value={normalizedBackend}
              onChange={(e) => onBackendChange(e.target.value)}
              disabled={!canEditRuntimeIdentity}
            >
              {(isAffinityWorkflow
                ? [
                    { value: 'boltz', label: 'Boltz-2' }
                  ]
                : [
                    { value: 'boltz', label: 'Boltz-2' },
                    { value: 'alphafold3', label: 'AlphaFold3' },
                    { value: 'protenix', label: 'Protenix' }
                  ]
              ).map((option) => (
                <option key={option.value} value={option.value} disabled={Boolean((option as { disabled?: boolean }).disabled)}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        )}

        {showFullFields && (isPredictionWorkflow || isPeptideDesignWorkflow) && (
          <label className="field">
            <span>Seed (optional)</span>
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
              placeholder="Default: 42"
            />
          </label>
        )}

        {showFullFields && (isPredictionWorkflow || isPeptideDesignWorkflow) && normalizedBackend !== 'alphafold3' && (
          <label className="switch-field runtime-device-toggle">
            <input
              type="checkbox"
              checked={lowVram}
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
                <label className="field">
                  <span>Peptide Design Mode</span>
                  <select
                    value={peptideDesignMode}
                    onChange={(e) =>
                      onPeptideDesignModeChange((e.target.value as 'linear' | 'cyclic' | 'bicyclic') || 'linear')
                    }
                    disabled={!canEdit}
                  >
                    <option value="linear">Linear</option>
                    <option value="cyclic">Cyclic</option>
                    <option value="bicyclic">Bicyclic</option>
                  </select>
                </label>
                <label className="field">
                  <span>Peptide Length</span>
                  <CommitNumberInput
                    min={peptideDesignMode === 'bicyclic' ? 8 : 5}
                    max={80}
                    value={peptideBinderLength}
                    onCommit={onPeptideBinderLengthChange}
                    disabled={!canEdit}
                  />
                </label>
                <div className="muted small peptide-runtime-backend-hint">
                  Cyclic uses a head-to-tail bond; bicyclic uses 3 Cys + a linker (SEZ/29N/BS3). All three backends support every mode.
                </div>
                <label className="switch-field peptide-runtime-switch peptide-initial-seq-toggle">
                  <input
                    type="checkbox"
                    checked={peptideUseInitialSequence}
                    onChange={(e) => onPeptideUseInitialSequenceChange(e.target.checked)}
                    disabled={!canEdit}
                  />
                  <span>Seed first generation from reference sequence</span>
                </label>
                <div className="peptide-residue-config">
                  <div className="peptide-residue-config-head">
                    <div className="peptide-residue-config-title">
                      <strong>Residues used for design</strong>
                      <span>Select the residues available for the next peptide generation.</span>
                    </div>
                    <span className="peptide-residue-selection-summary">
                      {selectedNaturalCount} natural / {selectedNonNaturalCount} non-natural selected
                    </span>
                  </div>
                  <div className="peptide-residue-usage">
                    <div className="peptide-residue-usage-copy">
                      <strong>Non-natural residue count</strong>
                      <span>
                        {selectedNonNaturalCount === 0
                          ? 'Select non-natural residues to enable this constraint.'
                          : 'Set how many selected non-natural residues each designed peptide may contain.'}
                      </span>
                    </div>
                    <div className="peptide-residue-usage-controls">
                      <label className="field peptide-residue-usage-field">
                        <span>At least</span>
                        <CommitNumberInput
                          min={0}
                          max={peptideBinderLength}
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
                          max={peptideBinderLength}
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
                  {!peptideResiduePoolAvailable ? (
                    <div className="muted small peptide-runtime-backend-hint">
                      The completed task does not contain the original residue-pool snapshot; edits here configure the next submitted task.
                    </div>
                  ) : null}
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
                </div>
                {customEditorOpen && (
                  <div className="peptide-custom-editor">
                    <div className="peptide-custom-editor-head">
                      <strong>{customEditingCcd ? 'Edit custom residue' : 'Add custom residue'}</strong>
                      <button type="button" className="btn btn-ghost btn-compact" onClick={closeCustomResidueEditor}>
                        Close
                      </button>
                    </div>
                    <div className="peptide-custom-editor-grid">
                      <label className="field">
                        <span>CCD</span>
                        <input
                          value={customEditingCcd || generateCustomResidueCode(currentUserId, customDraftSmiles.trim())}
                          readOnly
                          title="Auto-generated per user + residue (not editable). User CCDs override built-ins, so this only needs to be unique."
                        />
                      </label>
                      <label className="field">
                        <span>Name</span>
                        <input
                          value={customDraftName}
                          disabled={residuePoolControlsDisabled}
                          onChange={(event) => setCustomDraftName(event.target.value)}
                          placeholder="Custom residue"
                        />
                      </label>
                      <label className="field">
                        <span>Base residue</span>
                        <select
                          value={customDraftBaseResidue}
                          disabled={residuePoolControlsDisabled}
                          onChange={(event) => setCustomDraftBaseResidue(event.target.value)}
                        >
                          {'ARNDCQEGHILKMFPSTWYV'.split('').map((aa) => (
                            <option key={aa} value={aa}>
                              {aa}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <div className="peptide-custom-editor-main">
                      <div className="jsme-editor-container component-jsme-shell peptide-custom-jsme">
                        <JSMEEditor smiles={customDraftSmiles} height={360} onSmilesChange={setCustomDraftSmiles} />
                      </div>
                      <div className="peptide-custom-preview">
                        <MemoLigand2DPreview
                          smiles={customDraftSmiles}
                          width={240}
                          height={160}
                          highlightAtomIndices={assignedBackboneIndices.length ? assignedBackboneIndices : undefined}
                          highlightAtomColorsOverride={backboneHighlightColorOverride}
                          atomLabels={backboneAtomLabels}
                          onAtomClick={armedBackboneSlot ? handleBackboneAtomClick : undefined}
                        />
                        <div className="peptide-custom-backbone-slots" role="group" aria-label="Backbone atom slots">
                          {CUSTOM_BACKBONE_SLOTS.map((slot) => {
                            const idx = customDraftBackbone[slot];
                            const armed = armedBackboneSlot === slot;
                            return (
                              <button
                                key={slot}
                                type="button"
                                className={`peptide-custom-backbone-slot${armed ? ' armed' : ''}${idx === undefined ? ' empty' : ''}${customDraftSlotErrors[slot] ? ' error' : ''}`}
                                onClick={() => setArmedBackboneSlot((prev) => (prev === slot ? null : slot))}
                                title={
                                  armed
                                    ? `Click an atom in the 2D to assign ${slotLabel(slot)}`
                                    : `Set ${slotLabel(slot)}${idx === undefined ? '' : ` (atom #${idx + 1})`}`
                                }
                              >
                                <span className="peptide-custom-backbone-slot-label">{slotLabel(slot)}</span>
                                <span className="peptide-custom-backbone-slot-value">{idx === undefined ? '—' : `#${idx + 1}`}</span>
                              </button>
                            );
                          })}
                        </div>
                        {firstBackboneSlotError(customDraftSlotErrors) ? (
                          <span className="peptide-custom-invalid">{firstBackboneSlotError(customDraftSlotErrors)}</span>
                        ) : null}
                        <div className="peptide-custom-backbone-foot">
                          <button
                            type="button"
                            className="peptide-custom-backbone-reset"
                            onClick={() => void resetBackboneToAuto()}
                            title="Re-run auto backbone detection"
                          >
                            Auto
                          </button>
                          {!customDraftValid ? (
                            <span className="peptide-custom-invalid">Backbone N-CA-C(=O) is required.</span>
                          ) : null}
                          {customDraftAutoStatus === 'failed' ? (
                            <span className="peptide-custom-invalid">
                              Auto could not identify the full backbone. Click atoms to set N/CA/C/O/OXT manually — for a C-terminal amide, enable amidation first.
                            </span>
                          ) : null}
                        </div>
                      </div>
                    </div>
                    <label className="field peptide-custom-smiles">
                      <span>Custom Residue SMILES</span>
                      <input
                        value={customDraftSmiles}
                        disabled={residuePoolControlsDisabled}
                        onChange={(event) => setCustomDraftSmiles(event.target.value)}
                      />
                    </label>
                    <label className="switch-field peptide-custom-amidation">
                      <input
                        type="checkbox"
                        checked={customDraftAmidated}
                        disabled={residuePoolControlsDisabled}
                        onChange={async (event) => {
                          const nextAmidated = event.target.checked;
                          const currentSmiles = String(customDraftSmiles || '').trim() || CUSTOM_RESIDUE_SCAFFOLD_SMILES;
                          // Flip the backbone's terminal atom (OXT <-> NXT), honoring the user's OXT pick.
                          // Atomic: if the terminal can't be resolved, leave flag and SMILES unchanged.
                          const transformed = await toggleTerminalAmide(currentSmiles, customDraftBackbone, nextAmidated);
                          if (!transformed || transformed === currentSmiles) return;
                          setCustomDraftAmidated(nextAmidated);
                          setCustomDraftSmiles(transformed);
                        }}
                      />
                      <span>C-terminal amidation</span>
                    </label>
                    <div className="peptide-custom-editor-actions">
                      <button
                        type="button"
                        className="btn btn-primary btn-compact"
                        disabled={residuePoolControlsDisabled || !customDraftSmiles.trim() || !customDraftValid || Boolean(firstBackboneSlotError(customDraftSlotErrors))}
                        onClick={saveCustomResidueDraft}
                      >
                        Save residue
                      </button>
                    </div>
                  </div>
                )}
                <label className="field">
                  <span>Iterations</span>
                  <CommitNumberInput
                    min={2}
                    max={100}
                    value={peptideIterations}
                    onCommit={onPeptideIterationsChange}
                    disabled={!canEdit}
                  />
                </label>
                <label className="field">
                  <span>Population Size</span>
                  <CommitNumberInput
                    min={2}
                    max={100}
                    value={peptidePopulationSize}
                    onCommit={onPeptidePopulationSizeChange}
                    disabled={!canEdit}
                  />
                </label>
                <label className="field">
                  <span>Elite Size</span>
                  <CommitNumberInput
                    min={1}
                    max={Math.max(1, peptidePopulationSize - 1)}
                    value={peptideEliteSize}
                    onCommit={onPeptideEliteSizeChange}
                    disabled={!canEdit}
                  />
                </label>
                <label className="field">
                  <span>Mutation Rate</span>
                  <CommitNumberInput
                    min={0.01}
                    max={1}
                    step={0.01}
                    value={peptideMutationRate}
                    onCommit={onPeptideMutationRateChange}
                    disabled={!canEdit}
                  />
                </label>
                <label className="field peptide-mask-field">
                  <span>Fixed positions</span>
                  <input
                    className="peptide-fixed-reference-input"
                    type="text"
                    value={normalizedInitialSequence}
                    onChange={(e) => onPeptideInitialSequenceChange(e.target.value)}
                    disabled={!canEdit}
                    placeholder={`Reference sequence, length ${peptideBinderLength}`}
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
                          disabled={!canToggleMask || !canFixPosition}
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
              {normalizedInitialSequence.length !== peptideBinderLength && (
                <p className="muted small">
                  Reference sequence length is {normalizedInitialSequence.length}. Expected {peptideBinderLength} to fix every desired position.
                </p>
              )}
            </section>

            {isBicyclicMode && (
              <section className="peptide-runtime-group peptide-runtime-group-bicyclic">
                <div className="peptide-runtime-group-headline">
                  <div className="peptide-runtime-group-head">Bicyclic Specific</div>
                  <span className="peptide-runtime-chip">Bicyclic</span>
                </div>
                <p className="muted small peptide-runtime-group-desc">
                  Configure linker and cysteine topology for bicyclic peptide generation.
                </p>
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
                          disabled={!canEdit}
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
                      <label className="field">
                        <span>Cys Position Mode</span>
                        <select
                          value={peptideBicyclicCysPositionMode}
                          onChange={(e) =>
                            onPeptideBicyclicCysPositionModeChange((e.target.value as 'auto' | 'manual') || 'auto')
                          }
                          disabled={!canEdit}
                        >
                          <option value="auto">Auto</option>
                          <option value="manual">Manual</option>
                        </select>
                      </label>
                    </div>
                    <div className="peptide-runtime-grid peptide-runtime-grid-controls">
                      <label className="switch-field peptide-runtime-switch">
                        <input
                          type="checkbox"
                          checked={peptideBicyclicFixTerminalCys}
                          onChange={(e) => onPeptideBicyclicFixTerminalCysChange(e.target.checked)}
                          disabled={!canEdit || cysPositionAuto}
                        />
                        <span>Fix Terminal Cys</span>
                      </label>
                      <label className="switch-field peptide-runtime-switch">
                        <input
                          type="checkbox"
                          checked={peptideBicyclicIncludeExtraCys}
                          onChange={(e) => onPeptideBicyclicIncludeExtraCysChange(e.target.checked)}
                          disabled={!canEdit}
                        />
                        <span>Allow Extra Cys</span>
                      </label>
                    </div>

                    <div className={`peptide-runtime-grid peptide-runtime-grid-cys ${cysPositionAuto ? 'is-disabled' : ''}`}>
                      <div className="field peptide-cys-picker-field">
                        <span>Cys Positions</span>
                        <div className="peptide-cys-slot-tabs" role="tablist" aria-label="Cysteine slots">
                          {([
                            { key: 'cys1' as CysSlot, label: 'Cys 1' },
                            { key: 'cys2' as CysSlot, label: 'Cys 2' },
                            { key: 'cys3' as CysSlot, label: 'Cys 3' }
                          ]).map((slot) => {
                            const disabled = slot.key === 'cys3' && peptideBicyclicFixTerminalCys;
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
                            const disabledByFixedCys3 = activeCysSlot === 'cys3' && peptideBicyclicFixTerminalCys;
                            const disabled = !canEdit || cysPositionAuto || disabledByRange || disabledByFixedCys3;
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
                    {cysPositionAuto && (
                      <p className="muted small">Auto mode will optimize Cys positions during design.</p>
                    )}
                    {!cysPositionAuto && peptideBicyclicFixTerminalCys && (
                      <p className="muted small">Cys 3 is anchored to terminal residue.</p>
                    )}
                    {hasDuplicatedCysPositions && (
                      <p className="muted small">Manual Cys positions should be different to form two valid rings.</p>
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
