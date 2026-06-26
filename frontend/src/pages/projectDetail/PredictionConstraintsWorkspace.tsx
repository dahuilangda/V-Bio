import { useMemo, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';
import { ArrowLeft } from 'lucide-react';
import { ConstraintEditor } from '../../components/project/ConstraintEditor';
import { Ligand2DPreview } from '../../components/project/Ligand2DPreview';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import type { InputComponent, PredictionConstraint, PredictionConstraintType, PredictionProperties, ProteinModification } from '../../types/models';
import { buildChainInfos } from '../../utils/chainAssignments';
import { BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES } from '../../components/project/residueCatalog';
import { buildComponentAtomOptionsByChain } from '../../utils/constraintAtomOptions';
import { extractStructureResidueAtomOptions, type StructureAtomOptionsByChain } from '../../utils/structureParser';

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
  if (mod?.inputMethod === 'jsme') return [];

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

function selectedResidueNumberForChain(params: {
  chainId: string;
  pickedResidue: { chainId: string; residue: number; atomName?: string } | null;
  activeResidue: { chainId: string; residue: number } | null;
}) {
  const { chainId, pickedResidue, activeResidue } = params;
  if (pickedResidue?.chainId === chainId) return pickedResidue.residue;
  if (activeResidue?.chainId === chainId) return activeResidue.residue;
  return null;
}

function ConstraintSequencePicker({
  components,
  atomOptionsByChain,
  pickedResidue,
  selectedAtomRefs,
  highlightResidues,
  activeResidue,
  disabled,
  onPick
}: {
  components: InputComponent[];
  atomOptionsByChain: StructureAtomOptionsByChain;
  pickedResidue: { chainId: string; residue: number; atomName?: string } | null;
  selectedAtomRefs: Array<{ chainId: string; residue: number; atomName: string }>;
  highlightResidues: Array<{ chainId: string; residue: number }>;
  activeResidue: { chainId: string; residue: number } | null;
  disabled: boolean;
  onPick: (pick: MolstarResiduePick) => void;
}) {
  const activeComponents = components.filter((item) => cleanSequence(item.sequence));
  const chainInfos = buildChainInfos(activeComponents);
  const componentById = new Map(activeComponents.map((item) => [item.id, item] as const));
  const highlightKeys = new Set(highlightResidues.map((item) => `${item.chainId}:${item.residue}`));
  const selectedAtomKeys = new Set(selectedAtomRefs.map((item) => `${item.chainId}:${item.residue}:${String(item.atomName || '').trim().toUpperCase()}`));
  const activeKey = activeResidue ? `${activeResidue.chainId}:${activeResidue.residue}` : '';
  const pickedKey = pickedResidue ? `${pickedResidue.chainId}:${pickedResidue.residue}` : '';
  const selectedDetail = (() => {
    const selectedChains = [pickedResidue?.chainId, activeResidue?.chainId].filter(Boolean) as string[];
    for (const selectedChainId of selectedChains) {
      const chain = chainInfos.find((item) => item.id === selectedChainId);
      if (!chain || chain.type !== 'protein') continue;
      const component = componentById.get(chain.componentId);
      const rows = atomOptionsByChain[chain.id] || [];
      const residue = selectedResidueNumberForChain({ chainId: chain.id, pickedResidue, activeResidue });
      const row = residue ? rows.find((item) => item.residue === residue) || null : null;
      const smiles = residue2DSmiles(component, row || undefined);
      const atomLabels = residue2DAtomLabels(component, row || undefined);
      if (component && row) return { chain, component, row, smiles, atomLabels };
    }
    return null;
  })();

  if (chainInfos.length === 0) {
    return (
      <div className="constraint-viewer-empty muted small">
        Add Components to enable local picking for constraints.
      </div>
    );
  }

  return (
    <div className={`constraint-sequence-picker ${selectedDetail ? 'has-selection-detail' : ''}`} aria-label="Constraint sequence picker">
      <div className="constraint-sequence-list">
        {chainInfos.map((chain) => {
          const component = componentById.get(chain.componentId);
          const rows = atomOptionsByChain[chain.id] || [];
          const sequence = cleanSequence(component?.sequence || '');
          if (!sequence || rows.length === 0) return null;
          const title = `${chain.id} · ${chain.type}${chain.copyIndex > 0 ? ` copy ${chain.copyIndex + 1}` : ''}`;
          const isLigand = chain.type === 'ligand';
          return (
          <section key={`${chain.componentId}-${chain.id}`} className="constraint-sequence-chain">
            <div className="constraint-sequence-chain-head">
              <strong>{title}</strong>
              <span className="muted small">{isLigand ? `${rows[0]?.atoms.length || 0} atoms` : `${rows.length} residues`}</span>
            </div>
            {isLigand ? (
              <div className="constraint-ligand-picker-body">
                {(rows[0]?.atoms || []).length > 0 && component?.inputMethod !== 'ccd' && (
                  <div className="constraint-2d-panel constraint-ligand-2d-panel">
                    <Ligand2DPreview
                      smiles={component?.sequence || ''}
                      width={250}
                      height={170}
                      atomLabels={(rows[0]?.atoms || []).map((_atom, index) => String(index + 1))}
                      highlightAtomIndices={(rows[0]?.atoms || []).reduce<number[]>((acc, atom, index) => {
                        const key = `${chain.id}:1`;
                        const atomKey = `${chain.id}:1:${atom}`;
                        if ((key === pickedKey && pickedResidue?.atomName === atom) || selectedAtomKeys.has(atomKey)) acc.push(index);
                        return acc;
                      }, [])}
                      onAtomClick={(atomIndex) => {
                        const atom = (rows[0]?.atoms || [])[atomIndex];
                        if (!atom) return;
                        onPick({
                          chainId: chain.id,
                          residue: 1,
                          atomName: atom,
                          label: `${chain.id}:1:${atom}`
                        });
                      }}
                    />
                  </div>
                )}
                <div className="constraint-ligand-atom-grid">
                  {(rows[0]?.atoms || []).length > 0 ? (
                    (rows[0]?.atoms || []).map((atom, index) => {
                      const key = `${chain.id}:1`;
                      const atomKey = `${chain.id}:1:${atom}`;
                      const picked = (key === pickedKey && pickedResidue?.atomName === atom) || selectedAtomKeys.has(atomKey);
                      return (
                        <button
                          key={`${chain.id}:1:${atom}`}
                          type="button"
                          className={`constraint-ligand-atom ${picked ? 'picked' : ''}`}
                          disabled={disabled}
                          onClick={() =>
                            onPick({
                              chainId: chain.id,
                              residue: 1,
                              atomName: atom,
                              label: `${chain.id}:1:${atom}`
                            })
                          }
                          title={`${chain.id}:1:${atom}`}
                        >
                          <span className="constraint-atom-index">{index + 1}</span>
                          <span className="constraint-atom-name">{atom}</span>
                        </button>
                      );
                    })
                  ) : (
                    <div className="constraint-ligand-atoms-empty muted small">
                      No deterministic atom names are available for this ligand input.
                    </div>
                  )}
                </div>
              </div>
            ) : (
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
            )}
          </section>
          );
        })}
      </div>
      {selectedDetail && (
        <aside className="constraint-selection-detail-panel" aria-label="Selected residue atoms">
          <div className="constraint-residue-2d-meta">
            <strong>{selectedDetail.row.residueName}</strong>
            <span className="muted small">{selectedDetail.chain.id}:{selectedDetail.row.residue}</span>
          </div>
          {selectedDetail.smiles && (
            <Ligand2DPreview
              smiles={selectedDetail.smiles}
              width={250}
              height={170}
              atomLabels={selectedDetail.atomLabels}
              highlightAtomIndices={selectedDetail.atomLabels.reduce<number[]>((acc, atomName, index) => {
                const atom = String(atomName || '').trim().toUpperCase();
                if (!atom) return acc;
                const atomKey = `${selectedDetail.chain.id}:${selectedDetail.row.residue}:${atom}`;
                if (
                  (pickedResidue?.chainId === selectedDetail.chain.id &&
                    pickedResidue.residue === selectedDetail.row.residue &&
                    String(pickedResidue.atomName || '').trim().toUpperCase() === atom) ||
                  selectedAtomKeys.has(atomKey)
                ) {
                  acc.push(index);
                }
                return acc;
              }, [])}
              onAtomClick={(atomIndex) => {
                const atomName = String(selectedDetail.atomLabels[atomIndex] || '').trim().toUpperCase();
                if (!atomName || !selectedDetail.row.atoms.includes(atomName)) return;
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
        </aside>
      )}
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
  constraintPickModeEnabled: boolean;
  onToggleConstraintPickMode: () => void;
  canEdit: boolean;
  onBackToComponents: () => void;
  onNavigateConstraint: (delta: -1 | 1) => void;
  pickedResidue: { chainId: string; residue: number; atomName?: string } | null;
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
  constraintPickModeEnabled,
  onToggleConstraintPickMode,
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
  components,
  constraints,
  properties,
  activeConstraintId,
  selectedConstraintIds,
  onSelectedConstraintIdChange,
  onConstraintClick,
  allowedConstraintTypes,
  isBondOnlyBackend,
  onConstraintsChange,
  onPropertiesChange,
  disabled
}: PredictionConstraintsWorkspaceProps) {
  const sequenceAtomOptionsByChain = useMemo(() => buildComponentAtomOptionsByChain(components), [components]);
  const structureAtomOptionsByChain = useMemo(() => {
    if (!hasConstraintStructure || !constraintStructureText.trim()) return sequenceAtomOptionsByChain;
    return extractStructureResidueAtomOptions(constraintStructureText, constraintStructureFormat);
  }, [hasConstraintStructure, constraintStructureText, constraintStructureFormat, sequenceAtomOptionsByChain]);

  if (!visible) return null;
  const pickModeHint = constraintPickModeEnabled
    ? 'Pick Mode is on: left click in Mol* to auto-fill the selected constraint.'
    : 'Enable Pick Mode to start selecting residues from Mol*.';

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
              <span className="muted small constraint-nav-pick-hint">{pickModeHint}</span>
              {pickedResidue && <span className="muted small constraint-nav-picked">Picked: {pickedResidue.chainId}:{pickedResidue.residue}</span>}
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
              <button
                type="button"
                className={`btn btn-compact ${constraintPickModeEnabled ? 'btn-primary' : 'btn-ghost'}`}
                onClick={onToggleConstraintPickMode}
                disabled={!canEdit}
              >
                {constraintPickModeEnabled ? 'Pick: On' : 'Pick: Off'}
              </button>
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
            lockView={constraintPickModeEnabled}
            suppressAutoFocus={constraintPickModeEnabled}
            onResiduePick={(pick: MolstarResiduePick) => {
              onApplyPickToSelectedConstraint(pick);
            }}
          />
        ) : (
          <ConstraintSequencePicker
            components={components}
            atomOptionsByChain={structureAtomOptionsByChain}
            pickedResidue={pickedResidue}
            selectedAtomRefs={constraintSelectedAtomRefs}
            highlightResidues={constraintViewerHighlightResidues}
            activeResidue={constraintViewerActiveResidue}
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
          compatibilityHint={isBondOnlyBackend ? 'Current backend currently supports Bond constraints only.' : undefined}
          onConstraintsChange={onConstraintsChange}
          onPropertiesChange={onPropertiesChange}
          onPickSlotFocus={onConstraintPickSlotFocus}
          disabled={disabled}
        />
      </section>
    </div>
  );
}
