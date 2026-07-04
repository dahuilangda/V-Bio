import { useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';
import { ArrowLeft, Boxes } from 'lucide-react';
import { ConstraintEditor } from '../../components/project/ConstraintEditor';
import { Ligand2DPreview } from '../../components/project/Ligand2DPreview';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import type { InputComponent, PredictionConstraint, PredictionConstraintType, PredictionProperties, ProteinModification } from '../../types/models';
import { buildChainInfos } from '../../utils/chainAssignments';
import { BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES } from '../../components/project/residueCatalog';
import { buildComponentAtomOptionsByChain } from '../../utils/constraintAtomOptions';
import { extractStructureResidueAtomOptions, type StructureAtomOptionsByChain } from '../../utils/structureParser';
import { loadRDKitModule, type RDKitModule } from '../../utils/rdkit';

export interface ConstraintTemplateOption {
  componentId: string;
  label: string;
  fileName: string;
  chainId: string;
}

export interface SelectedTemplatePreview {
  componentId: string;
  chainId: string;
}

function cleanSequence(value: string): string {
  return String(value || '').replace(/\s+/g, '').toUpperCase();
}

const NATURAL_RESIDUE_BY_ONE = new Map(NATURAL_AMINO_ACID_RESIDUES.map((entry) => [entry.baseResidue, entry] as const));
const RESIDUE_BY_CCD = new Map([...NATURAL_AMINO_ACID_RESIDUES, ...BUILT_IN_PROTEIN_MODIFICATIONS].map((entry) => [entry.ccd, entry] as const));

function modificationByPosition(modifications: ProteinModification[] | undefined): Map<number, ProteinModification> {
  const byPosition = new Map<number, ProteinModification>();
  for (const mod of modifications || []) {
    const position = Math.max(1, Math.floor(Number(mod.position || 0)));
    if (Number.isFinite(position) && position > 0 && !byPosition.has(position)) byPosition.set(position, mod);
  }
  return byPosition;
}

function residue2DSmiles(component: InputComponent | undefined, row: { residue: number; residueName: string } | undefined): string {
  if (!component || !row) return '';
  const mods = modificationByPosition(component.modifications);
  const mod = mods.get(row.residue);
  if (mod?.smiles?.trim()) return mod.smiles.trim();
  const ccd = String(mod?.ccd || row.residueName || '').trim().toUpperCase();
  const catalogEntry = RESIDUE_BY_CCD.get(ccd);
  if (catalogEntry?.smiles) return catalogEntry.smiles;
  const sequenceResidue = cleanSequence(component.sequence)[row.residue - 1] || '';
  return NATURAL_RESIDUE_BY_ONE.get(sequenceResidue)?.smiles || '';
}

const RESIDUE_RDKit_ATOM_LABELS_BY_CCD: Record<string, string[]> = {
  PRO: ['N', 'CD', 'CG', 'CB', 'CA', 'C', 'O'],
  HYP: ['O', 'C', '', 'CA', 'CB', 'CG', 'OD1', 'CD', 'N'],
  PCA: ['O', 'C', '', 'CA', 'CB', 'CG', 'CD', 'OE', 'N']
};

function residue2DAtomLabels(component: InputComponent | undefined, row: { residue: number; residueName: string; atoms: string[] } | undefined): string[] {
  if (!component || !row) return [];
  const mods = modificationByPosition(component.modifications);
  const mod = mods.get(row.residue);
  // JSME custom residues: row.atoms is already in RDKit depiction (atom-index) order
  // (customResidueAtomNamesFromSmiles), and residue2DSmiles uses the same SMILES, so
  // row.atoms[i] is exactly the name of the i-th depicted atom — use directly so the
  // 2D labels, highlight, and onAtomClick all line up with the atom grid.
  if (mod?.inputMethod === 'jsme') return row.atoms;

  const ccd = String(mod?.ccd || row.residueName || '').trim().toUpperCase();
  const sequenceResidue = cleanSequence(component.sequence)[row.residue - 1] || '';
  const naturalCcd = NATURAL_RESIDUE_BY_ONE.get(sequenceResidue)?.ccd || '';
  const key = ccd || naturalCcd;
  const explicit = RESIDUE_RDKit_ATOM_LABELS_BY_CCD[key];
  if (explicit) return explicit;

  const atoms = row.atoms.map((atom) => String(atom || '').trim().toUpperCase()).filter(Boolean);
  if (atoms.length === 0) return [];
  const has = (atom: string) => atoms.includes(atom);
  const sidechain = atoms.filter((atom) => !['N', 'CA', 'C', 'O', 'OXT'].includes(atom));
  const labels = [has('N') ? 'N' : '', has('CA') ? 'CA' : '', ...sidechain, has('C') ? 'C' : '', has('O') ? 'O' : ''];
  if (has('OXT')) labels.push('OXT');
  return labels;
}

export interface BondEndpointSummary {
  chain: string;
  residue: number;
  atom: string;
}

export interface ActiveBondEndpoints {
  id: string;
  atom1: BondEndpointSummary;
  atom2: BondEndpointSummary;
}

interface PickedResidueLike {
  chainId: string;
  residue: number;
  atomName?: string;
}

// Left pane: all chains listed as a vertical stack of sections. Each protein/dna/rna chain
// shows a residue grid; a ligand chain shows an atom-name grid. Clicking any residue/atom
// button aims the active endpoint at it and picks it. The active endpoint slot's residue
// (targetResidue) drives the `.active` marker, so the left mirrors the right's Atom 1/Atom 2
// tab; the right-side 2D (top of the constraint card) is the primary atom picker.
function ConstraintChainPicker({
  components,
  atomOptionsByChain,
  targetResidue,
  pickedResidue,
  highlightResidues,
  selectedAtomRefs,
  disabled,
  onPick
}: {
  components: InputComponent[];
  atomOptionsByChain: StructureAtomOptionsByChain;
  targetResidue: { chainId: string; residue: number } | null;
  pickedResidue: PickedResidueLike | null;
  highlightResidues: Array<{ chainId: string; residue: number }>;
  selectedAtomRefs: Array<{ chainId: string; residue: number; atomName: string }>;
  disabled: boolean;
  onPick: (pick: MolstarResiduePick) => void;
}) {
  const activeComponents = components.filter((item) => cleanSequence(item.sequence));
  const chainInfos = buildChainInfos(activeComponents);
  const highlightKeys = new Set(highlightResidues.map((item) => `${item.chainId}:${item.residue}`));
  // The active residue marker follows the active endpoint slot (targetResidue), so the left
  // grid highlights the exact residue of the selected Atom 1/Atom 2 (or token1/token2) tab.
  const activeKey = targetResidue ? `${targetResidue.chainId}:${targetResidue.residue}` : '';
  const pickedKey = pickedResidue ? `${pickedResidue.chainId}:${pickedResidue.residue}` : '';
  const selectedAtomKeys = new Set(
    selectedAtomRefs.map((item) => `${item.chainId}:${item.residue}:${String(item.atomName || '').trim().toUpperCase()}`)
  );

  if (chainInfos.length === 0) {
    return (
      <div className="constraint-viewer-empty">
        <Boxes size={18} />
        <span className="muted small">No components</span>
      </div>
    );
  }

  const typeLabel = (type: string) => type.charAt(0).toUpperCase() + type.slice(1);

  return (
    <div className="constraint-sequence-picker" aria-label="Constraint chain picker">
      <div className="constraint-sequence-list">
        {chainInfos.map((chain) => {
          const rows = atomOptionsByChain[chain.id] || [];
          const isLigand = chain.type === 'ligand';
          const count = isLigand ? rows[0]?.atoms.length || 0 : rows.length;
          const title = `${chain.id} · ${typeLabel(chain.type)}${chain.copyIndex > 0 ? ` copy ${chain.copyIndex + 1}` : ''}`;
          const isPickedChain = targetResidue?.chainId === chain.id;
          return (
            <section
              key={`${chain.componentId}-${chain.id}`}
              className={`constraint-sequence-chain${isPickedChain ? ' is-picked-chain' : ''}`}
            >
              <div className="constraint-sequence-chain-head">
                <strong>{title}</strong>
                <span className="muted small">{isLigand ? `${count} atoms` : `${count} residues`}</span>
              </div>
              {isLigand ? (
                count > 0 ? (
                  <div className="constraint-sequence-grid">
                    {rows[0].atoms.map((atom, atomIndex) => {
                      const atomName = String(atom || '').trim().toUpperCase();
                      const atomKey = `${chain.id}:1:${atomName}`;
                      const picked =
                        selectedAtomKeys.has(atomKey) ||
                        (pickedResidue?.chainId === chain.id &&
                          pickedResidue.residue === 1 &&
                          String(pickedResidue.atomName || '').trim().toUpperCase() === atomName);
                      return (
                        <button
                          key={atomKey}
                          type="button"
                          className={`constraint-sequence-residue is-atom ${picked ? 'picked' : ''}`}
                          disabled={disabled}
                          onClick={() => onPick({ chainId: chain.id, residue: 1, atomName, label: atomKey })}
                          title={atomKey}
                        >
                          <span className="constraint-sequence-residue-index">{atomIndex + 1}</span>
                          <span className="constraint-sequence-residue-letter">{atom}</span>
                        </button>
                      );
                    })}
                  </div>
                ) : (
                  <div className="muted small">No atom names</div>
                )
              ) : rows.length > 0 ? (
                <div className="constraint-sequence-grid">
                  {rows.map((row) => {
                    const position = row.residue;
                    const key = `${chain.id}:${position}`;
                    const active = key === activeKey;
                    const picked = key === pickedKey;
                    const highlighted = highlightKeys.has(key);
                    const atom = row.atoms[0] || '';
                    return (
                      <button
                        key={key}
                        type="button"
                        className={`constraint-sequence-residue ${highlighted ? 'highlighted' : ''} ${active ? 'active' : ''} ${picked ? 'picked' : ''}`}
                        disabled={disabled || !atom}
                        onClick={() =>
                          atom &&
                          onPick({
                            chainId: chain.id,
                            residue: position,
                            atomName: atom,
                            label: `${chain.id}:${position}:${atom}`
                          })
                        }
                        title={`${chain.id}:${position}:${row.residueName}${atom ? `:${atom}` : ''}`}
                      >
                        <span className="constraint-sequence-residue-index">{position}</span>
                        <span className="constraint-sequence-residue-letter">{row.residueName}</span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div className="muted small">No residues</div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

// Right pane: show the picked residue's 2D depiction + atom grid, plus bond endpoint
// chips when a bond constraint is active. pickedResidue drives what is shown, so the 2D
// follows the user's selection (protein residue or ligand) and never gets stuck.
function ResidueAtomPicker({
  components,
  atomOptionsByChain,
  targetResidue,
  pickedResidue,
  selectedAtomRefs,
  onPick,
  disabled
}: {
  components: InputComponent[];
  atomOptionsByChain: StructureAtomOptionsByChain;
  targetResidue: { chainId: string; residue: number } | null;
  pickedResidue: PickedResidueLike | null;
  selectedAtomRefs: Array<{ chainId: string; residue: number; atomName: string }>;
  onPick: (pick: MolstarResiduePick) => void;
  disabled: boolean;
}) {
  const activeComponents = components.filter((item) => cleanSequence(item.sequence));
  const chainInfos = buildChainInfos(activeComponents);
  const componentById = new Map(activeComponents.map((item) => [item.id, item] as const));

  const selectedAtomKeys = new Set(
    selectedAtomRefs.map((item) => `${item.chainId}:${item.residue}:${String(item.atomName || '').trim().toUpperCase()}`)
  );

  // The 2D detail view is driven by `targetResidue` — the residue of the constraint's
  // currently-active endpoint (derived from constraint data + active slot), NOT by
  // pickedResidue. pickedResidue is only the viewer-click highlight signal. This keeps
  // the 2D always in sync with the endpoint being edited, with no manual state syncing.
  const selectedDetail = (() => {
    const selectedChainId = targetResidue?.chainId;
    if (!selectedChainId) return null;
    const chain = chainInfos.find((item) => item.id === selectedChainId);
    if (!chain) return null;
    const component = componentById.get(chain.componentId);
    if (!component) return null;
    const rows = atomOptionsByChain[chain.id] || [];

    if (chain.type === 'ligand') {
      const row = rows[0] || null;
      if (!row) return null;
      const smiles = component.inputMethod === 'ccd' ? '' : component.sequence || '';
      const atomLabels = (row.atoms || []).map((_atom, index) => String(index + 1));
      return { chain, component, row, smiles, atomLabels, isLigand: true };
    }

    if (chain.type !== 'protein') return null;
    const residue = targetResidue.residue;
    const row = residue ? rows.find((item) => item.residue === residue) || null : null;
    if (!row) return null;
    const smiles = residue2DSmiles(component, row);
    const atomLabels = residue2DAtomLabels(component, row);
    return { chain, component, row, smiles, atomLabels, isLigand: false };
  })();

  return (
    <aside className="constraint-selection-detail-panel" aria-label="Selected residue atoms">
      {selectedDetail ? (
        <>
          {selectedDetail.smiles && (
            <Ligand2DPreview
              smiles={selectedDetail.smiles}
              width={250}
              height={170}
              atomLabels={selectedDetail.atomLabels}
              highlightAtomIndices={selectedDetail.atomLabels.reduce<number[]>((acc, label, index) => {
                // Ligand: label is a 1-based index, atom name lives at atoms[index].
                // Protein: label is the atom name itself.
                const atomName = selectedDetail.isLigand
                  ? selectedDetail.row.atoms[index] || ''
                  : String(label || '').trim().toUpperCase();
                if (!atomName) return acc;
                const atomKey = `${selectedDetail.chain.id}:${selectedDetail.row.residue}:${atomName}`;
                if (
                  (pickedResidue?.chainId === selectedDetail.chain.id &&
                    pickedResidue.residue === selectedDetail.row.residue &&
                    String(pickedResidue.atomName || '').trim().toUpperCase() === atomName) ||
                  selectedAtomKeys.has(atomKey)
                ) {
                  acc.push(index);
                }
                return acc;
              }, [])}
              onAtomClick={(atomIndex) => {
                const atomName = selectedDetail.isLigand
                  ? selectedDetail.row.atoms[atomIndex] || ''
                  : String(selectedDetail.atomLabels[atomIndex] || '').trim().toUpperCase();
                if (!atomName) return;
                if (!selectedDetail.isLigand && !selectedDetail.row.atoms.includes(atomName)) return;
                onPick({
                  chainId: selectedDetail.chain.id,
                  residue: selectedDetail.row.residue,
                  atomName,
                  label: `${selectedDetail.chain.id}:${selectedDetail.row.residue}:${atomName}`
                });
              }}
            />
          )}
          {selectedDetail.row.atoms.length > 0 && (
            <div className="constraint-residue-atom-grid" aria-label={`${selectedDetail.chain.id}:${selectedDetail.row.residue} atoms`}>
              {selectedDetail.row.atoms.map((atom) => {
                const atomKey = `${selectedDetail.chain.id}:${selectedDetail.row.residue}:${atom}`;
                const picked =
                  (pickedResidue?.chainId === selectedDetail.chain.id &&
                    pickedResidue.residue === selectedDetail.row.residue &&
                    pickedResidue.atomName === atom) ||
                  selectedAtomKeys.has(atomKey);
                return (
                  <button
                    key={atomKey}
                    type="button"
                    className={`constraint-residue-atom ${picked ? 'picked' : ''}`}
                    disabled={disabled}
                    onClick={() =>
                      onPick({
                        chainId: selectedDetail.chain.id,
                        residue: selectedDetail.row.residue,
                        atomName: atom,
                        label: atomKey
                      })
                    }
                    title={atomKey}
                  >
                    {atom}
                  </button>
                );
              })}
            </div>
          )}
        </>
      ) : null}
    </aside>
  );
}

// Bond endpoint chips (Atom 1 / Atom 2): the primary entry for choosing which endpoint the
// next pick fills. Rendered at the top of the active bond constraint card
// (ConstraintEditor.endpointTargets), above Constraint Type, so both endpoints and the active
// slot stay visible while editing — pick a chip to aim, then click a residue/atom on the left.
function BondEndpointTargets({
  activeBondEndpoints,
  activeConstraintPickSlot,
  onEndpointActivate
}: {
  activeBondEndpoints: ActiveBondEndpoints | null;
  activeConstraintPickSlot: 'first' | 'second';
  onEndpointActivate: (slot: 'first' | 'second') => void;
}) {
  if (!activeBondEndpoints) return null;
  return (
    <div className="constraint-endpoint-targets" role="group" aria-label="Bond endpoint targets">
      {(
        [
          { slot: 'first', label: 'Atom 1', endpoint: activeBondEndpoints.atom1 },
          { slot: 'second', label: 'Atom 2', endpoint: activeBondEndpoints.atom2 }
        ] as const
      ).map(({ slot, label, endpoint }) => {
        const isActive = activeConstraintPickSlot === slot;
        const isEmpty = !endpoint.chain;
        const value = endpoint.chain
          ? `${endpoint.chain}:${endpoint.residue}:${endpoint.atom || '—'}`
          : 'not set';
        return (
          <button
            key={slot}
            type="button"
            className={`constraint-endpoint-target ${isActive ? 'active' : ''} ${isEmpty ? 'is-empty' : ''}`}
            onClick={() => onEndpointActivate(slot)}
            aria-pressed={isActive}
            title={`${label} · ${value}`}
          >
            <span className="constraint-endpoint-target-label">{label}</span>
            <span className="constraint-endpoint-target-value">{value}</span>
          </button>
        );
      })}
    </div>
  );
}

export interface PredictionConstraintsWorkspaceProps {
  visible: boolean;
  constraintsWorkspaceRef: RefObject<HTMLDivElement | null>;
  isConstraintsResizing: boolean;
  constraintsGridStyle: CSSProperties;
  constraintCount: number;
  activeConstraintIndex: number;
  constraintTemplateOptions: ConstraintTemplateOption[];
  selectedTemplatePreview: SelectedTemplatePreview | null;
  onSelectedConstraintTemplateComponentIdChange: (componentId: string | null) => void;
  canEdit: boolean;
  onBackToComponents: () => void;
  onNavigateConstraint: (delta: -1 | 1) => void;
  pickedResidue: PickedResidueLike | null;
  hasConstraintStructure: boolean;
  constraintStructureText: string;
  constraintStructureFormat: 'cif' | 'pdb';
  constraintViewerHighlightResidues: Array<{ chainId: string; residue: number }>;
  constraintViewerActiveResidue: { chainId: string; residue: number } | null;
  constraintSelectedAtomRefs: Array<{ chainId: string; residue: number; atomName: string }>;
  onApplyPickToSelectedConstraint: (pick: MolstarResiduePick) => void;
  onConstraintsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onConstraintsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  onClearConstraintSelection: () => void;
  onConstraintPickSlotFocus: (constraintId: string, slot: 'first' | 'second') => void;
  activeConstraintPickSlot: 'first' | 'second';
  components: InputComponent[];
  constraints: PredictionConstraint[];
  properties: PredictionProperties;
  activeConstraintId: string | null;
  selectedConstraintIds: string[];
  onSelectedConstraintIdChange: (id: string | null) => void;
  onConstraintClick: (id: string, options?: { toggle?: boolean; range?: boolean }) => void;
  allowedConstraintTypes: PredictionConstraintType[];
  isBondOnlyBackend: boolean;
  onConstraintsChange: (constraints: PredictionConstraint[]) => void;
  onPropertiesChange: (properties: PredictionProperties) => void;
  disabled: boolean;
}

export function PredictionConstraintsWorkspace({
  visible,
  constraintsWorkspaceRef,
  isConstraintsResizing,
  constraintsGridStyle,
  constraintCount,
  activeConstraintIndex,
  constraintTemplateOptions,
  selectedTemplatePreview,
  onSelectedConstraintTemplateComponentIdChange,
  canEdit,
  onBackToComponents,
  onNavigateConstraint,
  pickedResidue,
  hasConstraintStructure,
  constraintStructureText,
  constraintStructureFormat,
  constraintViewerHighlightResidues,
  constraintViewerActiveResidue,
  constraintSelectedAtomRefs,
  onApplyPickToSelectedConstraint,
  onConstraintsResizerPointerDown,
  onConstraintsResizerKeyDown,
  onClearConstraintSelection,
  onConstraintPickSlotFocus,
  activeConstraintPickSlot,
  components,
  constraints,
  properties,
  activeConstraintId,
  selectedConstraintIds,
  onSelectedConstraintIdChange,
  onConstraintClick,
  allowedConstraintTypes,
  onConstraintsChange,
  onPropertiesChange,
  disabled
}: PredictionConstraintsWorkspaceProps) {
  // RDKit resolves custom-residue atom names from the drawn SMILES (mirroring the
  // backend CCD builder). loadRDKitModule() is a cached promise already started by
  // Ligand2DPreview in this view, so no extra network cost.
  const [rdkit, setRdkit] = useState<RDKitModule | null>(null);
  useEffect(() => {
    let alive = true;
    loadRDKitModule()
      .then((module) => {
        if (alive) setRdkit(module);
      })
      .catch(() => {
        /* RDKit unavailable: custom residues expose no atom names until it loads */
      });
    return () => {
      alive = false;
    };
  }, []);
  const sequenceAtomOptionsByChain = useMemo(() => buildComponentAtomOptionsByChain(components, rdkit), [components, rdkit]);
  const structureAtomOptionsByChain = useMemo(() => {
    if (!hasConstraintStructure || !constraintStructureText.trim()) return sequenceAtomOptionsByChain;
    return extractStructureResidueAtomOptions(constraintStructureText, constraintStructureFormat);
  }, [hasConstraintStructure, constraintStructureText, constraintStructureFormat, sequenceAtomOptionsByChain]);

  const activeBondEndpoints = useMemo<ActiveBondEndpoints | null>(() => {
    if (!activeConstraintId) return null;
    const constraint = constraints.find((item) => item.id === activeConstraintId);
    if (!constraint || constraint.type !== 'bond') return null;
    return {
      id: constraint.id,
      atom1: { chain: constraint.atom1_chain, residue: constraint.atom1_residue, atom: constraint.atom1_atom },
      atom2: { chain: constraint.atom2_chain, residue: constraint.atom2_residue, atom: constraint.atom2_atom }
    };
  }, [constraints, activeConstraintId]);

  // 2D display source, derived directly from the active constraint's data + active slot
  // (bond → atom1/atom2, contact → token1/token2, pocket → binder). Always defined once a
  // constraint is selected — the 2D never falls back to pickedResidue, which is only the
  // viewer-click highlight signal. This removes the manual pickedResidue syncing that
  // previously caused the 2D to go blank.
  const activeEndpointTarget = useMemo<{ chainId: string; residue: number } | null>(() => {
    if (!activeConstraintId) return null;
    const constraint = constraints.find((item) => item.id === activeConstraintId);
    if (!constraint) return null;
    if (constraint.type === 'bond') {
      return activeConstraintPickSlot === 'second'
        ? { chainId: constraint.atom2_chain, residue: constraint.atom2_residue }
        : { chainId: constraint.atom1_chain, residue: constraint.atom1_residue };
    }
    if (constraint.type === 'contact') {
      return activeConstraintPickSlot === 'second'
        ? { chainId: constraint.token2_chain, residue: constraint.token2_residue }
        : { chainId: constraint.token1_chain, residue: constraint.token1_residue };
    }
    return { chainId: constraint.binder, residue: 1 };
  }, [constraints, activeConstraintId, activeConstraintPickSlot]);

  // Activating a target chip only switches the active endpoint slot; the 2D panel follows
  // automatically via activeEndpointTarget, so no pickedResidue sync is needed here.
  const handleEndpointActivate = (slot: 'first' | 'second') => {
    if (!activeBondEndpoints) return;
    onConstraintPickSlotFocus(activeBondEndpoints.id, slot);
  };

  // Rendered inside the active constraint card: endpoint chips (Atom 1/2) at the top, above
  // Constraint Type, as the primary endpoint selector; the 2D + atom grid below the fields.
  const bondEndpointTargets = activeBondEndpoints ? (
    <BondEndpointTargets
      activeBondEndpoints={activeBondEndpoints}
      activeConstraintPickSlot={activeConstraintPickSlot}
      onEndpointActivate={handleEndpointActivate}
    />
  ) : null;

  const residueAtomPicker = (
    <ResidueAtomPicker
      components={components}
      atomOptionsByChain={structureAtomOptionsByChain}
      targetResidue={activeEndpointTarget}
      pickedResidue={pickedResidue}
      selectedAtomRefs={constraintSelectedAtomRefs}
      onPick={onApplyPickToSelectedConstraint}
      disabled={!canEdit}
    />
  );

  if (!visible) return null;

  return (
    <div
      ref={constraintsWorkspaceRef as RefObject<HTMLDivElement>}
      className={`constraint-workspace resizable ${isConstraintsResizing ? 'is-resizing' : ''}`}
      style={constraintsGridStyle}
    >
      <section className="constraint-viewer-panel">
        <div className="constraint-nav-bar">
          <div className="constraint-nav-title-group">
            <div className="constraint-nav-title-row constraint-nav-title-row-inline">
              <h3>Constraint Picker</h3>
              <span className="muted small constraint-nav-counter">
                {constraintCount === 0 ? 'No constraints' : `${activeConstraintIndex >= 0 ? activeConstraintIndex + 1 : 0}/${constraintCount}`}
              </span>
            </div>
          </div>
          <div className="constraint-nav-controls">
            {constraintTemplateOptions && constraintTemplateOptions.length > 0 && (
              <label className="constraint-template-switch">
                <select
                  aria-label="Select protein template for constraint viewer"
                  value={selectedTemplatePreview?.componentId || ''}
                  onChange={(e) => onSelectedConstraintTemplateComponentIdChange(e.target.value || null)}
                >
                  {constraintTemplateOptions.map((item) => (
                    <option key={`constraint-template-${item.componentId}`} value={item.componentId}>
                      {item.label} - {item.fileName} (chain {item.chainId})
                    </option>
                  ))}
                </select>
              </label>
            )}
            <div className="constraint-nav-actions">
              <button type="button" className="btn btn-ghost btn-compact" onClick={onBackToComponents}>
                <ArrowLeft size={14} />
                Components
              </button>
              <button type="button" className="btn btn-ghost btn-compact" onClick={() => onNavigateConstraint(-1)} disabled={constraintCount <= 1}>
                Prev
              </button>
              <button type="button" className="btn btn-ghost btn-compact" onClick={() => onNavigateConstraint(1)} disabled={constraintCount <= 1}>
                Next
              </button>
            </div>
          </div>
        </div>
        {hasConstraintStructure ? (
          <MolstarViewer
            key={`constraint-viewer-${selectedTemplatePreview?.componentId || 'none'}-${selectedTemplatePreview?.chainId || 'none'}`}
            structureText={constraintStructureText}
            format={constraintStructureFormat}
            colorMode="default"
            pickMode="click"
            highlightResidues={constraintViewerHighlightResidues}
            activeResidue={constraintViewerActiveResidue}
            lockView
            suppressAutoFocus
            onResiduePick={(pick: MolstarResiduePick) => {
              onApplyPickToSelectedConstraint(pick);
            }}
          />
        ) : (
          <ConstraintChainPicker
            components={components}
            atomOptionsByChain={structureAtomOptionsByChain}
            targetResidue={activeEndpointTarget}
            pickedResidue={pickedResidue}
            highlightResidues={constraintViewerHighlightResidues}
            selectedAtomRefs={constraintSelectedAtomRefs}
            disabled={!canEdit}
            onPick={onApplyPickToSelectedConstraint}
          />
        )}
      </section>

      <div
        className={`panel-resizer ${isConstraintsResizing ? 'dragging' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize constraint picker and constraints panels"
        tabIndex={0}
        onPointerDown={onConstraintsResizerPointerDown}
        onKeyDown={onConstraintsResizerKeyDown}
      />

      <section
        className="constraint-editor-panel"
        onClick={(event) => {
          if (event.target === event.currentTarget) {
            onClearConstraintSelection();
          }
        }}
      >
        <ConstraintEditor
          components={components}
          constraints={constraints}
          properties={properties}
          pickedResidue={pickedResidue}
          structureAtomOptionsByChain={structureAtomOptionsByChain}
          selectedConstraintId={activeConstraintId}
          selectedConstraintIds={selectedConstraintIds}
          onSelectedConstraintIdChange={onSelectedConstraintIdChange}
          onConstraintClick={onConstraintClick}
          onClearSelection={onClearConstraintSelection}
          showAffinitySection={false}
          allowedConstraintTypes={allowedConstraintTypes}
          onConstraintsChange={onConstraintsChange}
          onPropertiesChange={onPropertiesChange}
          onPickSlotFocus={onConstraintPickSlotFocus}
          endpointTargets={bondEndpointTargets}
          activeResiduePicker={residueAtomPicker}
          disabled={disabled}
        />
      </section>
    </div>
  );
}
