import { LigandFragmentSketcher, type LigandFragmentItem } from '../LigandFragmentSketcher';
import type {
  LeadOptDirection,
  LeadOptQueryProperty,
} from './hooks/useLeadOptMmpQueryForm';

interface LeadOptFragmentPanelProps {
  sectionId?: string;
  effectiveLigandSmiles: string;
  fragments: LigandFragmentItem[];
  selectedFragmentIds: string[];
  activeFragmentId: string;
  onAtomClick: (atomIndex: number, options?: { additive?: boolean; preferredFragmentId?: string }) => void;
  onToggleFragmentSelection: (fragmentId: string, options?: { additive?: boolean }) => void;
  onClearFragmentSelection: () => void;
  direction: LeadOptDirection;
  queryProperty: LeadOptQueryProperty;
  selectedDatabaseId: string;
  databaseOptions: Array<{ id: string; label: string }>;
  propertyOptions: Array<{ value: string; label: string }>;
  envRadius: number;
  minPairs: number;
  onDirectionChange: (value: LeadOptDirection) => void;
  onQueryPropertyChange: (value: LeadOptQueryProperty) => void;
  onDatabaseIdChange: (value: string) => void;
  onEnvRadiusChange: (value: number) => void;
  onMinPairsChange: (value: number) => void;
}

export function LeadOptFragmentPanel({
  sectionId,
  effectiveLigandSmiles,
  fragments,
  selectedFragmentIds,
  activeFragmentId,
  onAtomClick,
  onToggleFragmentSelection,
  onClearFragmentSelection,
  direction,
  queryProperty,
  selectedDatabaseId,
  databaseOptions,
  propertyOptions,
  envRadius,
  minPairs,
  onDirectionChange,
  onQueryPropertyChange,
  onDatabaseIdChange,
  onEnvRadiusChange,
  onMinPairsChange
}: LeadOptFragmentPanelProps) {
  return (
    <section id={sectionId} className="panel subtle lead-opt-panel">
      <LigandFragmentSketcher
        smiles={effectiveLigandSmiles}
        fragments={fragments}
        selectedFragmentIds={selectedFragmentIds}
        activeFragmentId={activeFragmentId}
        onAtomClick={onAtomClick}
        onFragmentClick={onToggleFragmentSelection}
        onBackgroundClick={onClearFragmentSelection}
        height={220}
      />

      <div className="lead-opt-build-controls">
        <label className="field lead-opt-build-field lead-opt-build-field--property">
          <span>Property</span>
          <select value={queryProperty} onChange={(e) => onQueryPropertyChange(e.target.value as LeadOptQueryProperty)}>
            <option value="">-</option>
            {propertyOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field lead-opt-build-field lead-opt-build-field--db">
          <span>DB</span>
          <select
            value={selectedDatabaseId}
            onChange={(e) => onDatabaseIdChange(e.target.value)}
            disabled={databaseOptions.length <= 1}
          >
            {databaseOptions.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field lead-opt-build-field lead-opt-build-field--direction">
          <span>Direction</span>
          <select
            value={direction}
            onChange={(e) => onDirectionChange(e.target.value as LeadOptDirection)}
            disabled={!queryProperty}
          >
            <option value="">-</option>
            <option value="increase">Up</option>
            <option value="decrease">Down</option>
          </select>
        </label>
        <label className="field lead-opt-build-field lead-opt-build-field--pairs">
          <span>Min pairs</span>
          <input
            type="number"
            min={1}
            max={50}
            value={minPairs}
            onChange={(e) => onMinPairsChange(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
          />
        </label>
        <label className="field lead-opt-build-field lead-opt-build-field--radius">
          <span>Env radius</span>
          <input
            type="number"
            min={0}
            max={6}
            value={envRadius}
            onChange={(e) => onEnvRadiusChange(Math.max(0, Math.min(6, Number(e.target.value) || 0)))}
          />
        </label>
      </div>
    </section>
  );
}
