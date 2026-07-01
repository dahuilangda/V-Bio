import { useEffect, useMemo, useState, type ChangeEvent, type FocusEvent } from 'react';
import { MemoLigand2DPreview } from '../../components/project/Ligand2DPreview';
import { JSMEEditor } from '../../components/project/JSMEEditor';
import { buildCustomResidueCatalog, BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES, type ResidueCatalogEntry } from '../../components/project/residueCatalog';
import { AMINO_ACID_BACKBONE_SMARTS, rdkitMolHasAminoAcidBackbone } from '../../utils/inputValidation';
import { loadRDKitModule } from '../../utils/rdkit';
import type { CustomCcdMoleculeInput, PeptideResiduePoolSelection } from '../../types/models';
import { normalizePredictionBackend } from './projectDraftUtils';

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
  const [activeCysSlot, setActiveCysSlot] = useState<CysSlot>('cys1');
  const [customEditorOpen, setCustomEditorOpen] = useState(false);
  const [customEditingCcd, setCustomEditingCcd] = useState('');
  const [customDraftCcd, setCustomDraftCcd] = useState('');
  const [customDraftName, setCustomDraftName] = useState('Custom residue');
  const [customDraftBaseResidue, setCustomDraftBaseResidue] = useState('A');
  const [customDraftSmiles, setCustomDraftSmiles] = useState(CUSTOM_RESIDUE_SCAFFOLD_SMILES);
  const [customDraftValid, setCustomDraftValid] = useState(false);
  const showFullFields = displayMode === 'full';
  const normalizedBackend = isAffinityWorkflow ? 'boltz' : normalizePredictionBackend(backend);
  const canEditRuntimeIdentity = canEdit || isPredictionWorkflow || isPeptideDesignWorkflow || isAffinityWorkflow;
  const isLinearOnlyPeptideBackend =
    isPeptideDesignWorkflow && (normalizedBackend === 'alphafold3' || normalizedBackend === 'protenix');
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
    const validate = async () => {
      const smiles = customDraftSmiles.trim();
      if (!smiles) {
        setCustomDraftValid(false);
        return;
      }
      try {
        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        setCustomDraftValid(rdkitMolHasAminoAcidBackbone(rdkit, smiles, true));
      } catch {
        if (!cancelled) setCustomDraftValid(false);
      }
    };
    void validate();
    return () => {
      cancelled = true;
    };
  }, [customDraftSmiles]);

  const openCustomResidueEditor = (entry?: CustomCcdMoleculeInput) => {
    const ccd = normalizeCustomResidueCode(entry?.ccd || '');
    setCustomEditingCcd(ccd);
    setCustomDraftCcd(ccd || `UAA${Math.max(1, peptideCustomResidueLibrary.length + 1)}`.slice(0, 12));
    setCustomDraftName(entry?.label || 'Custom residue');
    setCustomDraftBaseResidue(String(entry?.baseResidue || 'A').trim().toUpperCase().slice(0, 1) || 'A');
    setCustomDraftSmiles(entry?.smiles || CUSTOM_RESIDUE_SCAFFOLD_SMILES);
    setCustomEditorOpen(true);
  };

  const closeCustomResidueEditor = () => {
    setCustomEditorOpen(false);
    setCustomEditingCcd('');
  };

  const customResidues = useMemo(() => buildCustomResidueCatalog(peptideCustomResidueLibrary), [peptideCustomResidueLibrary]);
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
      .map((item) => {
        const itemKind = normalizePoolEntryKind(item);
        return { code: item.ccd, kind: itemKind };
      })
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
        section.entries.map((entry) => ({ code: entry.ccd, kind: section.kind }))
      )
      .filter((item) => next.has(`${item.kind}:${item.code}`));
    onPeptideResiduePoolChange(ordered);
  };


  const saveCustomResidueDraft = () => {
    if (residuePoolControlsDisabled || !customDraftValid) return;
    const ccd = normalizeCustomResidueCode(customDraftCcd);
    const smiles = customDraftSmiles.trim();
    if (!ccd || !smiles) return;
    const nextEntry: CustomCcdMoleculeInput = {
      ccd,
      smiles,
      baseResidue: customDraftBaseResidue.trim().toUpperCase().slice(0, 1) || undefined,
      label: customDraftName.trim() || 'Custom residue'
    };
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
    const ordered = residueCatalogSections
      .flatMap((section) => section.entries.map((entry) => ({ code: entry.ccd, kind: section.kind })))
      .filter((item) => selectedKeys.has(`${item.kind}:${item.code}`));
    if (!ordered.some((item) => item.kind === 'custom' && item.code === ccd)) {
      ordered.push({ code: ccd, kind: 'custom' });
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
                    { value: 'alphafold3', label: 'AlphaFold3', disabled: isPeptideDesignWorkflow && peptideDesignMode !== 'linear' },
                    { value: 'protenix', label: 'Protenix', disabled: isPeptideDesignWorkflow && peptideDesignMode !== 'linear' }
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
                    <option value="cyclic" disabled={isLinearOnlyPeptideBackend}>
                      Cyclic
                    </option>
                    <option value="bicyclic" disabled={isLinearOnlyPeptideBackend}>
                      Bicyclic
                    </option>
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
                  AlphaFold3 and Protenix support linear peptides only. Cyclic and bicyclic designs run on Boltz-2.
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
                          value={customDraftCcd}
                          disabled={residuePoolControlsDisabled}
                          onChange={(event) => setCustomDraftCcd(normalizeCustomResidueCode(event.target.value))}
                          placeholder="UAA1"
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
                          highlightQuery={AMINO_ACID_BACKBONE_SMARTS}
                        />
                        <span className={customDraftValid ? 'peptide-custom-valid' : 'peptide-custom-invalid'}>
                          {customDraftValid ? 'Amino-acid backbone detected.' : 'Backbone N-CA-C(=O) is required.'}
                        </span>
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
                    <div className="peptide-custom-editor-actions">
                      <button
                        type="button"
                        className="btn btn-primary btn-compact"
                        disabled={residuePoolControlsDisabled || !normalizeCustomResidueCode(customDraftCcd) || !customDraftSmiles.trim() || !customDraftValid}
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
