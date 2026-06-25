import { useMemo, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';
import { ArrowLeft } from 'lucide-react';
import { ConstraintEditor } from '../../components/project/ConstraintEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import type { InputComponent, PredictionConstraint, PredictionConstraintType, PredictionProperties } from '../../types/models';
import { buildChainInfos } from '../../utils/chainAssignments';
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

function ConstraintSequencePicker({
  components,
  atomOptionsByChain,
  pickedResidue,
  highlightResidues,
  activeResidue,
  disabled,
  onPick
}: {
  components: InputComponent[];
  atomOptionsByChain: StructureAtomOptionsByChain;
  pickedResidue: { chainId: string; residue: number; atomName?: string } | null;
  highlightResidues: Array<{ chainId: string; residue: number }>;
  activeResidue: { chainId: string; residue: number } | null;
  disabled: boolean;
  onPick: (pick: MolstarResiduePick) => void;
}) {
  const activeComponents = components.filter((item) => cleanSequence(item.sequence));
  const chainInfos = buildChainInfos(activeComponents);
  const componentById = new Map(activeComponents.map((item) => [item.id, item] as const));
  const highlightKeys = new Set(highlightResidues.map((item) => `${item.chainId}:${item.residue}`));
  const activeKey = activeResidue ? `${activeResidue.chainId}:${activeResidue.residue}` : '';
  const pickedKey = pickedResidue ? `${pickedResidue.chainId}:${pickedResidue.residue}` : '';

  if (chainInfos.length === 0) {
    return (
      <div className="constraint-viewer-empty muted small">
        Add Components to enable local picking for constraints.
      </div>
    );
  }

  return (
    <div className="constraint-sequence-picker" aria-label="Constraint sequence picker">
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
              <div className="constraint-ligand-atom-grid">
                {(rows[0]?.atoms || []).length > 0 ? (
                  (rows[0]?.atoms || []).map((atom) => {
                    const key = `${chain.id}:1`;
                    const picked = key === pickedKey && pickedResidue?.atomName === atom;
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
                        {atom}
                      </button>
                    );
                  })
                ) : (
                  <div className="constraint-ligand-atoms-empty muted small">
                    Atom names require a structure-derived CCD or a backend with deterministic SMILES atom naming.
                  </div>
                )}
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
  onApplyPickToSelectedConstraint: (pick: MolstarResiduePick) => void;
  onConstraintsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onConstraintsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  onClearConstraintSelection: () => void;
  components: InputComponent[];
  backend: string;
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
  onApplyPickToSelectedConstraint,
  onConstraintsResizerPointerDown,
  onConstraintsResizerKeyDown,
  onClearConstraintSelection,
  components,
  backend,
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
  const sequenceAtomOptionsByChain = useMemo(() => buildComponentAtomOptionsByChain(components, backend), [components, backend]);
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
          disabled={disabled}
        />
      </section>
    </div>
  );
}
