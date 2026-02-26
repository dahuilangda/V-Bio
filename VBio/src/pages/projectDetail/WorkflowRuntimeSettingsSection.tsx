import { useMemo, useState } from 'react';
import { MemoLigand2DPreview } from '../../components/project/Ligand2DPreview';

type CysSlot = 'cys1' | 'cys2' | 'cys3';
type BicyclicLinkerType = 'SEZ' | '29N' | 'BS3';

// Canonical CCD SMILES used for RDKit previews.
const BICYCLIC_LINKERS: Array<{ type: BicyclicLinkerType; name: string; smiles: string }> = [
  { type: 'SEZ', name: '1,3,5-Trimethylbenzene', smiles: 'Cc1cc(C)cc(C)c1' },
  { type: '29N', name: 'Triazinane linker', smiles: 'CC(=O)CCN1CN(CC(=O)CC)CN(CC(=O)CC)C1' },
  { type: 'BS3', name: 'Bi(III) center', smiles: '[Bi+3]' }
];

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
  onPeptideBicyclicLinkerCcdChange,
  onPeptideBicyclicCysPositionModeChange,
  onPeptideBicyclicFixTerminalCysChange,
  onPeptideBicyclicIncludeExtraCysChange,
  onPeptideBicyclicCys1PosChange,
  onPeptideBicyclicCys2PosChange,
  onPeptideBicyclicCys3PosChange
}: WorkflowRuntimeSettingsSectionProps) {
  const [activeCysSlot, setActiveCysSlot] = useState<CysSlot>('cys1');
  if (!visible) return null;
  const showFullFields = displayMode === 'full';
  const normalizedBackend = isAffinityWorkflow ? 'boltz' : backend;
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
  const canToggleMask = canEdit && peptideUseInitialSequence;

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
    const sequenceChar = normalizedInitialSequence[index] || 'X';
    if (!sequenceChar || sequenceChar === 'X') return;
    const nextMask = [...maskChars];
    nextMask[index] = nextMask[index] === 'X' ? sequenceChar : 'X';
    onPeptideSequenceMaskChange(nextMask.join(''));
  };

  return (
    <section className="panel subtle component-runtime-settings">
      <div className="component-runtime-settings-row">
        {showFullFields && (
          <label className="field">
            <span>
              Backend <span className="required-mark">*</span>
            </span>
            <select required value={normalizedBackend} onChange={(e) => onBackendChange(e.target.value)} disabled={!canEdit}>
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
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        )}

        {showFullFields && isPredictionWorkflow && (
          <label className="field">
            <span>Random Seed (optional)</span>
            <input
              type="number"
              min={0}
              value={seed ?? ''}
              onChange={(e) => {
                const value = e.target.value;
                const nextSeed = value === '' ? null : Math.max(0, Math.floor(Number(value) || 0));
                onSeedChange(nextSeed);
              }}
              disabled={!canEdit}
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
                      onPeptideDesignModeChange((e.target.value as 'linear' | 'cyclic' | 'bicyclic') || 'cyclic')
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
                  <input
                    type="number"
                    min={peptideDesignMode === 'bicyclic' ? 8 : 5}
                    max={80}
                    value={peptideBinderLength}
                    onChange={(e) => onPeptideBinderLengthChange(Math.floor(Number(e.target.value) || peptideBinderLength))}
                    disabled={!canEdit}
                  />
                </label>
                <label className="switch-field peptide-runtime-switch peptide-initial-seq-toggle">
                  <input
                    type="checkbox"
                    checked={peptideUseInitialSequence}
                    onChange={(e) => onPeptideUseInitialSequenceChange(e.target.checked)}
                    disabled={!canEdit}
                  />
                  <span>Use Initial Sequence</span>
                </label>
                {peptideUseInitialSequence && (
                  <label className="field peptide-initial-seq-field">
                    <span>Initial Sequence</span>
                    <input
                      type="text"
                      value={normalizedInitialSequence}
                      onChange={(e) => onPeptideInitialSequenceChange(e.target.value)}
                      disabled={!canEdit}
                      placeholder={`Length ${peptideBinderLength}, e.g. ACDEFG...`}
                      spellCheck={false}
                    />
                  </label>
                )}
                <label className="field">
                  <span>Iterations</span>
                  <input
                    type="number"
                    min={2}
                    max={100}
                    value={peptideIterations}
                    onChange={(e) => onPeptideIterationsChange(Math.floor(Number(e.target.value) || peptideIterations))}
                    disabled={!canEdit}
                  />
                </label>
                <label className="field">
                  <span>Population Size</span>
                  <input
                    type="number"
                    min={2}
                    max={100}
                    value={peptidePopulationSize}
                    onChange={(e) =>
                      onPeptidePopulationSizeChange(Math.floor(Number(e.target.value) || peptidePopulationSize))
                    }
                    disabled={!canEdit}
                  />
                </label>
                <label className="field">
                  <span>Elite Size</span>
                  <input
                    type="number"
                    min={1}
                    max={Math.max(1, peptidePopulationSize - 1)}
                    value={peptideEliteSize}
                    onChange={(e) => onPeptideEliteSizeChange(Math.floor(Number(e.target.value) || peptideEliteSize))}
                    disabled={!canEdit}
                  />
                </label>
                <label className="field">
                  <span>Mutation Rate</span>
                  <input
                    type="number"
                    min={0.01}
                    max={1}
                    step={0.01}
                    value={peptideMutationRate}
                    onChange={(e) => onPeptideMutationRateChange(Number(e.target.value) || peptideMutationRate)}
                    disabled={!canEdit}
                  />
                </label>
                {peptideUseInitialSequence && (
                  <label className="field peptide-mask-field">
                    <span>Mask Positions (click to toggle)</span>
                    <div className="peptide-mask-rail" role="list" aria-label="Sequence mask positions">
                      {positions.map((position) => {
                        const residue = maskChars[position - 1] || 'X';
                        const fixed = residue !== 'X';
                        return (
                          <button
                            key={`peptide-mask-${position}`}
                            type="button"
                            role="listitem"
                            className={`peptide-mask-dot ${fixed ? 'fixed' : ''}`}
                            onClick={() => toggleMaskPosition(position)}
                            disabled={!canToggleMask}
                            title={fixed ? `Fixed at ${residue}` : `Position ${position} is mutable`}
                          >
                            <span>{position}</span>
                            <strong>{fixed ? residue : '·'}</strong>
                          </button>
                        );
                      })}
                    </div>
                  </label>
                )}
              </div>
              {peptideUseInitialSequence && normalizedInitialSequence.length !== peptideBinderLength && (
                <p className="muted small">
                  Initial sequence length is {normalizedInitialSequence.length}. Expected {peptideBinderLength}.
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
