import { ChevronDown, ChevronRight, Dna, FlaskConical, Plus, Target } from 'lucide-react';
import { componentTypeLabel } from '../../utils/projectInputs';
import type { InputComponent, PredictionConstraint, PredictionConstraintType, PredictionProperties } from '../../types/models';

export interface ComponentBucketEntry {
  id: string;
  filled: boolean;
  typeLabel: string;
  typeOrder: number;
  globalOrder: number;
}

export interface ComponentCompletionSummary {
  incompleteCount: number;
}

export interface WorkspaceSelection {
  componentId: string | null;
}

export interface WorkspaceOption {
  componentId: string;
  label: string;
  isSmallMolecule?: boolean;
}

export interface PredictionComponentsSidebarProps {
  visible: boolean;
  canEdit: boolean;
  components: InputComponent[];
  hasIncompleteComponents: boolean;
  componentCompletion: ComponentCompletionSummary;
  sidebarTypeOrder: Array<'protein' | 'ligand' | 'dna' | 'rna'>;
  componentTypeBuckets: Record<'protein' | 'ligand' | 'dna' | 'rna', ComponentBucketEntry[]>;
  sidebarTypeOpen: Record<'protein' | 'ligand' | 'dna' | 'rna', boolean>;
  onSidebarTypeToggle: (type: 'protein' | 'ligand' | 'dna' | 'rna') => void;
  onAddComponent: (type: 'protein' | 'ligand' | 'dna' | 'rna') => void;
  activeComponentId: string | null;
  onJumpToComponent: (id: string) => void;
  sidebarConstraintsOpen: boolean;
  onSidebarConstraintsToggle: () => void;
  constraintCount: number;
  onAddConstraint: () => void;
  hasActiveChains: boolean;
  constraints: PredictionConstraint[];
  activeConstraintId: string | null;
  selectedContactConstraintIdSet: Set<string>;
  onJumpToConstraint: (id: string, options?: { toggle?: boolean; range?: boolean }) => void;
  constraintLabel: (type: PredictionConstraintType) => string;
  formatConstraintCombo: (constraint: PredictionConstraint) => string;
  formatConstraintDetail: (constraint: PredictionConstraint) => string;
  properties: PredictionProperties;
  canEnableAffinityFromWorkspace: boolean;
  onSetAffinityEnabledFromWorkspace: (enabled: boolean) => void;
  selectedWorkspaceTarget: WorkspaceSelection;
  selectedWorkspaceLigand: WorkspaceSelection;
  workspaceTargetOptions: WorkspaceOption[];
  workspaceLigandSelectableOptions: WorkspaceOption[];
  onSetAffinityComponentFromWorkspace: (role: 'target' | 'ligand', componentId: string | null) => void;
  affinityEnableDisabledReason: string;
}

export function PredictionComponentsSidebar({
  visible,
  canEdit,
  components,
  hasIncompleteComponents,
  componentCompletion,
  sidebarTypeOrder,
  componentTypeBuckets,
  sidebarTypeOpen,
  onSidebarTypeToggle,
  onAddComponent,
  activeComponentId,
  onJumpToComponent,
  sidebarConstraintsOpen,
  onSidebarConstraintsToggle,
  constraintCount,
  onAddConstraint,
  hasActiveChains,
  constraints,
  activeConstraintId,
  selectedContactConstraintIdSet,
  onJumpToConstraint,
  constraintLabel,
  formatConstraintCombo,
  formatConstraintDetail,
  properties,
  canEnableAffinityFromWorkspace,
  onSetAffinityEnabledFromWorkspace,
  selectedWorkspaceTarget,
  selectedWorkspaceLigand,
  workspaceTargetOptions,
  workspaceLigandSelectableOptions,
  onSetAffinityComponentFromWorkspace,
  affinityEnableDisabledReason
}: PredictionComponentsSidebarProps) {
  if (!visible) return null;

  return (
    <aside className="component-sidebar">
      <div className="component-sidebar-head">
        <div className="component-sidebar-head-meta">
          <span className="component-count-chip">{components.length} items</span>
          <span className={`component-readiness-chip ${hasIncompleteComponents ? 'incomplete' : 'complete'}`}>
            {hasIncompleteComponents ? `${componentCompletion.incompleteCount} missing` : 'All ready'}
          </span>
        </div>
      </div>
      {sidebarTypeOrder.map((type) => {
        const bucket = componentTypeBuckets[type];
        return (
          <section className="component-sidebar-section" key={`sidebar-type-${type}`}>
            <div className="component-tree-row">
              <button type="button" className="component-sidebar-toggle" onClick={() => onSidebarTypeToggle(type)}>
                <span className="component-tree-label">
                  {sidebarTypeOpen[type] ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  {type === 'protein' ? <Dna size={13} /> : type === 'ligand' ? <FlaskConical size={13} /> : <Dna size={13} />}
                  <strong>{componentTypeLabel(type)}</strong>
                </span>
                <span className="muted small">{bucket.length}</span>
              </button>
              <button
                type="button"
                className="icon-btn component-tree-add"
                onClick={() => onAddComponent(type)}
                disabled={!canEdit}
                title={`Add ${componentTypeLabel(type)}`}
              >
                <Plus size={14} />
              </button>
            </div>

            {sidebarTypeOpen[type] && (
              <div className="component-sidebar-list component-sidebar-list-components">
                {bucket.length === 0 ? (
                  <div className="component-sidebar-empty muted small">No {componentTypeLabel(type).toLowerCase()} yet.</div>
                ) : (
                  bucket.map((entry) => (
                    <button
                      key={entry.id}
                      type="button"
                      className={`component-sidebar-link ${activeComponentId === entry.id ? 'active' : ''} ${
                        entry.filled ? 'is-complete' : 'is-incomplete'
                      }`}
                      onClick={() => onJumpToComponent(entry.id)}
                    >
                      <span>
                        {entry.typeLabel} {entry.typeOrder}
                      </span>
                      <span
                        className={`small component-sidebar-link-meta component-sidebar-link-status ${
                          entry.filled ? 'is-complete' : 'is-incomplete'
                        }`}
                      >
                        Comp {entry.globalOrder} · {entry.filled ? 'ready' : 'incomplete'}
                      </span>
                    </button>
                  ))
                )}
              </div>
            )}
          </section>
        );
      })}

      <section className="component-sidebar-section">
        <div className="component-tree-row">
          <button type="button" className="component-sidebar-toggle" onClick={onSidebarConstraintsToggle}>
            <span className="component-tree-label">
              {sidebarConstraintsOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              <strong>Constraints</strong>
            </span>
            <span className="muted small">{constraintCount}</span>
          </button>
          <button
            type="button"
            className="icon-btn component-tree-add"
            onClick={onAddConstraint}
            disabled={!canEdit || !hasActiveChains}
            title="Add constraint"
          >
            <Plus size={14} />
          </button>
        </div>
        {sidebarConstraintsOpen && (
          <div className="component-sidebar-list component-sidebar-list-nested">
            {constraints.length === 0 ? (
              <div className="component-sidebar-empty muted small">No constraints yet.</div>
            ) : (
              constraints.map((constraint, index) => (
                <button
                  key={constraint.id}
                  type="button"
                  className={`component-sidebar-link component-sidebar-link-constraint ${
                    activeConstraintId === constraint.id || selectedContactConstraintIdSet.has(constraint.id) ? 'active' : ''
                  }`}
                  onClick={(event) =>
                    onJumpToConstraint(constraint.id, {
                      toggle: event.metaKey || event.ctrlKey,
                      range: event.shiftKey
                    })
                  }
                >
                  <span>{`${index + 1}. ${constraintLabel(constraint.type)} · ${formatConstraintCombo(constraint)}`}</span>
                  <span className="muted small">{formatConstraintDetail(constraint)}</span>
                </button>
              ))
            )}
          </div>
        )}
      </section>

      <section className="component-sidebar-section">
        <div className="component-sidebar-toggle component-sidebar-toggle-static">
          <span className="component-tree-label">
            <Target size={13} />
            <strong>Binding</strong>
          </span>
          <label className="affinity-enable-toggle">
            <input
              type="checkbox"
              checked={properties.affinity}
              disabled={!canEdit || !canEnableAffinityFromWorkspace}
              onChange={(event) => onSetAffinityEnabledFromWorkspace(event.target.checked)}
            />
            <span>Compute</span>
          </label>
        </div>
        <div className="component-sidebar-list component-sidebar-list-nested affinity-sidebar-list">
          <label className="field affinity-field">
            <span className="affinity-key">Target</span>
            <select
              value={selectedWorkspaceTarget.componentId || ''}
              disabled={!canEdit || workspaceTargetOptions.length === 0}
              onChange={(e) => onSetAffinityComponentFromWorkspace('target', e.target.value || null)}
            >
              {workspaceTargetOptions.map((item) => (
                <option key={`workspace-affinity-target-${item.componentId}`} value={item.componentId}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field affinity-field">
            <span className="affinity-key">Ligand</span>
            <select
              value={selectedWorkspaceLigand.componentId || ''}
              disabled={!canEdit || workspaceLigandSelectableOptions.length === 0}
              onChange={(e) => onSetAffinityComponentFromWorkspace('ligand', e.target.value || null)}
            >
              <option value="">-</option>
              {workspaceLigandSelectableOptions.map((item) => (
                <option key={`workspace-affinity-ligand-${item.componentId}`} value={item.componentId}>
                  {item.isSmallMolecule ? item.label : `${item.label} (affinity disabled)`}
                </option>
              ))}
            </select>
          </label>
        </div>
        {!canEnableAffinityFromWorkspace && affinityEnableDisabledReason.trim() && (
          <div className="component-sidebar-empty muted small">{affinityEnableDisabledReason}</div>
        )}
      </section>

      <div className="component-sidebar-note muted small">Click items to jump to the target editor block.</div>
    </aside>
  );
}
