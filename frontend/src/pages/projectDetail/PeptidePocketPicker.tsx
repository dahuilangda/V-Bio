import { useEffect, useMemo, useRef, useState } from 'react';
import { Box, ChevronDown, ChevronRight, RotateCcw } from 'lucide-react';
import { MolstarViewer, type MolstarResiduePick } from '../../components/project/MolstarViewer';
import { PocketBoxControls } from '../../components/project/PocketBoxControls';
import {
  aminoAcidOptionLabel,
  formatPlainPocketPositions,
  formatPocketResiduePicks,
  parsePocketResidueTokens,
  peptidePocketSummaryLabel,
  peptidePocketTargetChanged,
  peptidePocketTargetSignature,
  pocketSubmissionFieldsFromBox,
  togglePocketPosition
} from '../../utils/peptidePocket';
import type { AffinityDockPocket } from '../../types/models';
import { InfoTip } from '../../components/common/InfoTip';

/** Pocket state wired into the Binding target component (peptide design only). */
export interface PeptideTargetPocketContext {
  /** The protein component Binding's Target refers to; null = no target yet. */
  componentId: string | null;
  /** YAML chain id of the target (residue-token chain prefix). */
  chainId: string | null;
  /** Target component sequence (drives the sequence-only picker). */
  sequence: string;
  pocketCenter: string;
  pocketResidues: string;
  pocketBox: number;
  dockPocket: AffinityDockPocket | null;
  onPocketFieldChange: (
    field: 'peptidePocketCenter' | 'peptidePocketResidues' | 'peptidePocketBox',
    value: string | number | null
  ) => void;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
}

// Sequence-only target: the pocket is defined by naming amino acids on the
// target sequence, constraint-style. A dropdown adds positions (labelled
// "25 · GLU" like the constraint residue selects); chips remove them; the rail
// below keeps the spatial overview and stays clickable.
function PeptidePocketSequencePicker({
  sequence,
  selectedPositions,
  disabled,
  onToggle
}: {
  sequence: string;
  selectedPositions: number[];
  disabled: boolean;
  onToggle: (position: number) => void;
}) {
  const letters = String(sequence || '').replace(/\s+/g, '').toUpperCase().split('');

  if (letters.length === 0) {
    return <div className="muted small">Target sequence is empty — add a protein target first.</div>;
  }

  return (
    <div className="peptide-pocket-sequence-picker">
      <div className="peptide-pocket-sequence-add">
        <select
          value=""
          disabled={disabled}
          aria-label="Add pocket residue"
          title="Add a target-sequence amino acid to the pocket"
          onChange={(event) => {
            const position = Number.parseInt(event.target.value, 10);
            if (Number.isFinite(position) && !selectedPositions.includes(position)) onToggle(position);
          }}
        >
          <option value="">Add residue…</option>
          {letters.map((letter, index) => {
            const position = index + 1;
            return (
              <option key={`peptide-pocket-option-${position}`} value={position} disabled={selectedPositions.includes(position)}>
                {aminoAcidOptionLabel(position, letter)}
              </option>
            );
          })}
        </select>
      </div>
      {selectedPositions.length > 0 ? (
        <div className="peptide-pocket-chips" role="list" aria-label="Selected pocket residues">
          {selectedPositions.map((position) => (
            <button
              key={`peptide-pocket-chip-${position}`}
              type="button"
              role="listitem"
              className="peptide-pocket-chip"
              disabled={disabled}
              onClick={() => onToggle(position)}
              title={`Remove pocket residue ${position}`}
            >
              {aminoAcidOptionLabel(position, letters[position - 1] || '?')}
              <span aria-hidden="true">×</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="muted small">No pocket residues selected — empty = design over the whole target surface.</div>
      )}
      <div className="peptide-pocket-rail" role="list" aria-label="Target sequence pocket residues">
        {letters.map((letter, index) => {
          const position = index + 1;
          const selected = selectedPositions.includes(position);
          return (
            <button
              key={`peptide-pocket-pos-${position}`}
              type="button"
              role="listitem"
              className={`peptide-mask-dot ${selected ? 'fixed' : ''}`}
              onClick={() => onToggle(position)}
              disabled={disabled}
              aria-pressed={selected}
              title={selected ? `Remove pocket residue ${position} (${letter})` : `Add pocket residue ${position} (${letter})`}
            >
              <span>{position}</span>
              <strong>{letter}</strong>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Peptide-design binding-pocket picker, mounted inside the target component's
// editor block (the component Binding's Target refers to). With an uploaded
// structure it mirrors the docking module: the component's inline MolStar
// viewer shows the box wireframe, clicking residues picks the pocket, and the
// Box panel adjusts center/size — all remembered with the draft/task. A
// sequence-only target instead names amino acids on the sequence
// (constraint-style); the engines fold the target (Protenix / Boltz2 /
// AlphaFold3) and the pocket constraint is applied to those positions, so the
// box is built around the user's residues after folding.
export function PeptidePocketPicker({
  canEdit,
  targetComponentId,
  targetTemplate,
  targetChainId,
  targetSequence,
  pocketCenter,
  pocketResidues,
  pocketBox,
  dockPocket,
  onPocketFieldChange,
  onDockPocketChange
}: {
  canEdit: boolean;
  targetComponentId: string | null;
  targetTemplate: {
    fileName: string;
    format: 'pdb' | 'cif';
    content: string;
    chainId: string;
  } | null;
  targetChainId: string | null;
  targetSequence: string;
  pocketCenter: string;
  pocketResidues: string;
  pocketBox: number;
  dockPocket: AffinityDockPocket | null;
  onPocketFieldChange: (field: 'peptidePocketCenter' | 'peptidePocketResidues' | 'peptidePocketBox', value: string | number | null) => void;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
}) {
  const hasStructure = Boolean(targetTemplate && targetTemplate.content.trim());
  const [picks, setPicks] = useState<MolstarResiduePick[]>([]);
  const [boxWireframe, setBoxWireframe] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Collapsed by default: the pocket stays a one-line summary until clicked.
  const [open, setOpen] = useState(false);
  const lastTargetSignatureRef = useRef('');

  // Identity of what the pocket is defined against (see
  // peptidePocketTargetSignature). A real change invalidates picks/box/fields
  // defined against the old coordinates — except the same component gaining
  // its uploaded structure, which is how restored tasks hydrate their
  // templates and must not wipe the persisted pocket.
  const targetSignature = peptidePocketTargetSignature({
    componentId: targetComponentId,
    hasStructure,
    fileName: targetTemplate?.fileName,
    contentLength: targetTemplate?.content.length,
    sequence: targetSequence
  });

  useEffect(() => {
    const previous = lastTargetSignatureRef.current;
    lastTargetSignatureRef.current = targetSignature;
    if (!peptidePocketTargetChanged(previous, targetSignature)) return;
    setPicks([]);
    setBoxWireframe('');
    onDockPocketChange(null);
    onPocketFieldChange('peptidePocketResidues', null);
    onPocketFieldChange('peptidePocketCenter', null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetSignature]);


  const handleResiduePick = (pick: MolstarResiduePick) => {
    if (!canEdit || !hasStructure) return;
    const exists = picks.some((p) => p.chainId === pick.chainId && p.residue === pick.residue);
    const next = exists
      ? picks.filter((p) => !(p.chainId === pick.chainId && p.residue === pick.residue))
      : [...picks, pick];
    setPicks(next);
    if (next.length === 0) {
      onDockPocketChange(null);
      onPocketFieldChange('peptidePocketResidues', null);
      return;
    }
    // Picks carry the template's author numbering; the token chain prefix is
    // the target's YAML chain, which the backend resolves through the
    // template alignment.
    onPocketFieldChange('peptidePocketResidues', formatPocketResiduePicks(targetChainId, next));
    onPocketFieldChange('peptidePocketCenter', null);
  };

  const handleDockPocketChange = (pocket: AffinityDockPocket | null) => {
    onDockPocketChange(pocket);
    const fields = pocketSubmissionFieldsFromBox(targetChainId, pocket, picks);
    onPocketFieldChange('peptidePocketCenter', fields.peptidePocketCenter);
    onPocketFieldChange('peptidePocketResidues', fields.peptidePocketResidues);
    if (fields.peptidePocketBox !== null) {
      onPocketFieldChange('peptidePocketBox', fields.peptidePocketBox);
    }
  };

  const handleClearPocket = () => {
    setDrawerOpen(false);
    setPicks([]);
    setBoxWireframe('');
    onDockPocketChange(null);
    onPocketFieldChange('peptidePocketResidues', null);
    onPocketFieldChange('peptidePocketCenter', null);
  };

  const pocketHighlights = useMemo(() => {
    if (picks.length > 0) {
      return picks.map((p) => ({ chainId: p.chainId, residue: p.residue }));
    }
    // Restored task: surface persisted pocket tokens on the template chain so
    // the selection is visible before the first re-pick.
    const templateChain = String(targetTemplate?.chainId || '').trim();
    if (!templateChain) return undefined;
    const contacts = parsePocketResidueTokens(pocketResidues).chainContacts;
    if (contacts.length === 0) return undefined;
    return contacts.map((c) => ({ chainId: templateChain, residue: c.residue }));
  }, [picks, targetTemplate?.chainId, pocketResidues]);

  const plainPositions = useMemo(
    () => parsePocketResidueTokens(pocketResidues).plainPositions,
    [pocketResidues]
  );

  const toggleSequencePosition = (position: number) => {
    const next = togglePocketPosition(plainPositions, position);
    onPocketFieldChange('peptidePocketResidues', next.length > 0 ? formatPlainPocketPositions(next) : null);
  };

  const pocketSummary = peptidePocketSummaryLabel(pocketCenter, pocketResidues);

  return (
    <div className="peptide-pocket-field peptide-pocket-field-component">
      <div className={`peptide-pocket-head ${open ? 'open' : 'collapsed'}`}>
        <button
          type="button"
          className="peptide-pocket-toggle"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span className="peptide-pocket-title">Binding pocket</span>
          {!open ? <span className="peptide-pocket-summary">{pocketSummary}</span> : null}
        </button>
        <InfoTip
          text={hasStructure
            ? 'Optional — leave empty to design over the whole target surface. Defined on the uploaded target structure.'
            : 'Optional — leave empty to design over the whole target surface. Defined on the target sequence.'}
          align="end"
        />
      </div>
      {open && hasStructure && targetTemplate ? (
        <div className="peptide-pocket-structure">
          <div className="peptide-pocket-toolbar">
            <button
              type="button"
              className={`btn pocket-box-btn ${drawerOpen ? 'active' : ''}`}
              onClick={() => setDrawerOpen((v) => !v)}
              disabled={!canEdit}
              title="Show the box controls (center/size sliders, ligand presets)"
            >
              <Box size={12} />
              {drawerOpen ? 'Hide box' : 'Box'}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-compact"
              onClick={handleClearPocket}
              disabled={!canEdit}
              title="Remove the pocket — design over the whole target surface"
            >
              <RotateCcw size={11} />
              Clear
            </button>
          </div>
          <div className="peptide-pocket-viewer">
            <MolstarViewer
              key={`peptide-pocket-viewer-${targetSignature}`}
              structureText={targetTemplate.content}
              format={targetTemplate.format}
              overlayStructureText={boxWireframe || undefined}
              overlayFormat="pdb"
              colorMode="default"
              pickMode="click"
              highlightResidues={pocketHighlights}
              onResiduePick={handleResiduePick}
            />
            {drawerOpen ? (
              <PocketBoxControls
                pocket={dockPocket}
                onPocketChange={handleDockPocketChange}
                proteinStructureText={targetTemplate.content}
                proteinStructureFormat={targetTemplate.format}
                pickedResidues={picks}
                onBoxWireframeChange={setBoxWireframe}
                onCollapse={() => setDrawerOpen(false)}
                canEdit={canEdit}
                submitting={false}
              />
            ) : null}
          </div>
        </div>
      ) : open ? (
        <div className="peptide-pocket-sequence">
          <PeptidePocketSequencePicker
            sequence={targetSequence}
            selectedPositions={plainPositions}
            disabled={!canEdit}
            onToggle={toggleSequencePosition}
          />
        </div>
      ) : null}
      {open ? (
      <details className="pocket-advanced">
        <summary className="muted small">Advanced</summary>
        <div className="pocket-box-fields">
          <input
            type="text"
            value={pocketCenter || ''}
            placeholder="center x,y,z"
            aria-label="Pocket center coordinates"
            title="Pocket center 'x,y,z' in the uploaded structure's frame; selects residues within the radius below"
            onChange={(e) => onPocketFieldChange('peptidePocketCenter', e.target.value)}
            disabled={!canEdit || !hasStructure}
          />
          <input
            type="text"
            value={pocketResidues || ''}
            placeholder={hasStructure ? 'residues A:152,A:153,…' : 'positions 25,26,27…'}
            aria-label="Pocket residues"
            title={
              hasStructure
                ? "Pocket residues in the uploaded structure's author numbering ('A:152,A:153')"
                : 'Target sequence positions acting as the pocket (1-based)'
            }
            onChange={(e) => onPocketFieldChange('peptidePocketResidues', e.target.value)}
            disabled={!canEdit}
          />
          <input
            type="number"
            min={4}
            max={40}
            value={pocketBox ?? 6}
            aria-label="Pocket box radius"
            title="Pocket radius in Å around the center (box size / 2)"
            onChange={(e) => {
              const next = Number(e.target.value);
              if (Number.isFinite(next) && e.target.value.trim() !== '') {
                onPocketFieldChange('peptidePocketBox', Math.max(4, Math.min(40, Math.round(next))));
              }
            }}
            disabled={!canEdit}
          />
        </div>
      </details>
      ) : null}
    </div>
  );
}
