import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { ArrowLeft } from 'lucide-react';
import { ConstraintEditor } from '../../components/project/ConstraintEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import type { InputComponent, PredictionConstraint, PredictionConstraintType, PredictionProperties } from '../../types/models';

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
  pickedResidue: { chainId: string; residue: number } | null;
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
  if (!visible) return null;

  return (
    <div
      ref={constraintsWorkspaceRef as RefObject<HTMLDivElement>}
      className={`constraint-workspace resizable ${isConstraintsResizing ? 'is-resizing' : ''}`}
      style={constraintsGridStyle}
    >
      <section className="panel subtle constraint-viewer-panel">
        <div className="constraint-nav-bar">
          <div className="constraint-nav-title-row">
            <h3>Constraint Picker</h3>
            <span className="muted small constraint-nav-counter">
              {constraintCount === 0 ? 'No constraints' : `${activeConstraintIndex >= 0 ? activeConstraintIndex + 1 : 0}/${constraintCount}`}
            </span>
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
        <div className="row between">
          <span className="muted small">
            {constraintPickModeEnabled
              ? 'Pick Mode is on: left click in Mol* to auto-fill the selected constraint.'
              : 'Enable Pick Mode to start selecting residues from Mol*.'}
          </span>
          {pickedResidue && <span className="muted small">Picked: {pickedResidue.chainId}:{pickedResidue.residue}</span>}
        </div>
        {hasConstraintStructure ? (
          <MolstarViewer
            key={`constraint-viewer-${selectedTemplatePreview?.componentId || 'none'}-${selectedTemplatePreview?.chainId || 'none'}`}
            structureText={constraintStructureText}
            format={constraintStructureFormat}
            colorMode="white"
            pickMode="click"
            highlightResidues={constraintViewerHighlightResidues}
            activeResidue={constraintViewerActiveResidue}
            lockView={constraintPickModeEnabled}
            suppressAutoFocus={constraintPickModeEnabled}
            onResiduePick={
              constraintPickModeEnabled
                ? (pick: MolstarResiduePick) => {
                    onApplyPickToSelectedConstraint(pick);
                  }
                : undefined
            }
          />
        ) : (
          <div className="constraint-viewer-empty muted small">
            Upload a protein template in Components to enable Mol* picking for constraints.
          </div>
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
        className="panel subtle constraint-editor-panel"
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
