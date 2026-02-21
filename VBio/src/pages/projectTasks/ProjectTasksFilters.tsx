import { Activity, Filter, RefreshCcw, Search, SlidersHorizontal } from 'lucide-react';
import { JSMEEditor } from '../../components/project/JSMEEditor';
import type { ProjectTask } from '../../types/models';
import { getWorkflowDefinition } from '../../utils/workflows';
import type {
  SeedFilterOption,
  StructureSearchMode,
  SubmittedWithinDaysOption,
  TaskWorkflowFilter
} from './taskListTypes';
import { backendLabel } from './taskPresentation';

interface ProjectTasksFiltersProps {
  taskSearch: string;
  onTaskSearchChange: (value: string) => void;
  stateFilter: 'all' | ProjectTask['task_state'];
  onStateFilterChange: (value: 'all' | ProjectTask['task_state']) => void;
  workflowFilter: TaskWorkflowFilter;
  onWorkflowFilterChange: (value: TaskWorkflowFilter) => void;
  workflowOptions: TaskWorkflowFilter[];
  backendFilter: 'all' | string;
  onBackendFilterChange: (value: string) => void;
  backendOptions: string[];
  leadOptOnlyView: boolean;
  filteredMatchedCount: number;
  showAdvancedFilters: boolean;
  onToggleAdvancedFilters: () => void;
  advancedFilterCount: number;
  submittedWithinDays: SubmittedWithinDaysOption;
  onSubmittedWithinDaysChange: (value: SubmittedWithinDaysOption) => void;
  seedFilter: SeedFilterOption;
  onSeedFilterChange: (value: SeedFilterOption) => void;
  minPlddt: string;
  onMinPlddtChange: (value: string) => void;
  minIptm: string;
  onMinIptmChange: (value: string) => void;
  maxPae: string;
  onMaxPaeChange: (value: string) => void;
  failureOnly: boolean;
  onFailureOnlyChange: (checked: boolean) => void;
  structureSearchMode: StructureSearchMode;
  onStructureSearchModeChange: (value: StructureSearchMode) => void;
  structureSearchQuery: string;
  onStructureSearchQueryChange: (value: string) => void;
  structureSearchLoading: boolean;
  structureSearchError: string | null;
  structureSearchMatches: Record<string, boolean>;
  onClearAdvancedFilters: () => void;
}

export function ProjectTasksFilters({
  taskSearch,
  onTaskSearchChange,
  stateFilter,
  onStateFilterChange,
  workflowFilter,
  onWorkflowFilterChange,
  workflowOptions,
  backendFilter,
  onBackendFilterChange,
  backendOptions,
  leadOptOnlyView,
  filteredMatchedCount,
  showAdvancedFilters,
  onToggleAdvancedFilters,
  advancedFilterCount,
  submittedWithinDays,
  onSubmittedWithinDaysChange,
  seedFilter,
  onSeedFilterChange,
  minPlddt,
  onMinPlddtChange,
  minIptm,
  onMinIptmChange,
  maxPae,
  onMaxPaeChange,
  failureOnly,
  onFailureOnlyChange,
  structureSearchMode,
  onStructureSearchModeChange,
  structureSearchQuery,
  onStructureSearchQueryChange,
  structureSearchLoading,
  structureSearchError,
  structureSearchMatches,
  onClearAdvancedFilters
}: ProjectTasksFiltersProps) {
  return (
    <>
      <div className="toolbar project-toolbar">
        <div className="project-toolbar-filters">
          <div className="project-filter-field project-filter-field-search">
            <div className="input-wrap search-input">
              <Search size={16} />
              <input
                value={taskSearch}
                onChange={(e) => onTaskSearchChange(e.target.value)}
                placeholder={leadOptOnlyView ? 'Search status/MMP/fragments...' : 'Search status/backend/metrics...'}
                aria-label="Search tasks"
              />
            </div>
          </div>
          <label className="project-filter-field">
            <Activity size={14} />
            <select
              className="project-filter-select"
              value={stateFilter}
              onChange={(e) => onStateFilterChange(e.target.value as 'all' | ProjectTask['task_state'])}
              aria-label="Filter tasks by state"
            >
              <option value="all">All States</option>
              <option value="DRAFT">Draft</option>
              <option value="QUEUED">Queued</option>
              <option value="RUNNING">Running</option>
              <option value="SUCCESS">Success</option>
              <option value="FAILURE">Failed</option>
              <option value="REVOKED">Revoked</option>
            </select>
          </label>
          <label className="project-filter-field">
            <Filter size={14} />
            <select
              className="project-filter-select"
              value={workflowFilter}
              onChange={(e) => onWorkflowFilterChange(e.target.value as TaskWorkflowFilter)}
              aria-label="Filter tasks by workflow"
            >
              <option value="all">All Workflows</option>
              {workflowOptions.map((value) => (
                <option key={`task-workflow-filter-${value}`} value={value}>
                  {getWorkflowDefinition(value).shortTitle}
                </option>
              ))}
            </select>
          </label>
          {!leadOptOnlyView && (
            <label className="project-filter-field">
              <Filter size={14} />
              <select
                className="project-filter-select"
                value={backendFilter}
                onChange={(e) => onBackendFilterChange(e.target.value)}
                aria-label="Filter tasks by backend"
              >
                <option value="all">All Backends</option>
                {backendOptions.map((value) => (
                  <option key={`task-backend-filter-${value}`} value={value}>
                    {backendLabel(value)}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <div className="project-toolbar-meta project-toolbar-meta-rich">
          <span className="muted small">{filteredMatchedCount} matched</span>
          <button
            type="button"
            className={`btn btn-ghost btn-compact advanced-filter-toggle ${showAdvancedFilters ? 'active' : ''}`}
            onClick={onToggleAdvancedFilters}
            title="Toggle advanced filters"
            aria-label="Toggle advanced filters"
          >
            <SlidersHorizontal size={14} />
            Advanced
            {advancedFilterCount > 0 && <span className="advanced-filter-badge">{advancedFilterCount}</span>}
          </button>
        </div>
      </div>

      {showAdvancedFilters && (
        <div className="advanced-filter-panel">
          <div className="advanced-filter-grid">
            <label className="advanced-filter-field">
              <span>Submitted</span>
              <select
                value={submittedWithinDays}
                onChange={(e) => onSubmittedWithinDaysChange(e.target.value as SubmittedWithinDaysOption)}
                aria-label="Advanced filter by submitted time"
              >
                <option value="all">Any time</option>
                <option value="1">Last 24 hours</option>
                <option value="7">Last 7 days</option>
                <option value="30">Last 30 days</option>
                <option value="90">Last 90 days</option>
              </select>
            </label>
            {!leadOptOnlyView && (
              <label className="advanced-filter-field">
                <span>Seed</span>
                <select
                  value={seedFilter}
                  onChange={(e) => onSeedFilterChange(e.target.value as SeedFilterOption)}
                  aria-label="Advanced filter by seed"
                >
                  <option value="all">Any seed</option>
                  <option value="with_seed">With seed</option>
                  <option value="without_seed">Without seed</option>
                </select>
              </label>
            )}
            {!leadOptOnlyView && (
              <>
                <label className="advanced-filter-field">
                  <span>Min pLDDT</span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step={0.1}
                    value={minPlddt}
                    onChange={(e) => onMinPlddtChange(e.target.value)}
                    placeholder="e.g. 70"
                    aria-label="Minimum pLDDT"
                  />
                </label>
                <label className="advanced-filter-field">
                  <span>Min iPTM</span>
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step={0.001}
                    value={minIptm}
                    onChange={(e) => onMinIptmChange(e.target.value)}
                    placeholder="e.g. 0.70"
                    aria-label="Minimum iPTM"
                  />
                </label>
                <label className="advanced-filter-field">
                  <span>Max PAE</span>
                  <input
                    type="number"
                    min={0}
                    step={0.1}
                    value={maxPae}
                    onChange={(e) => onMaxPaeChange(e.target.value)}
                    placeholder="e.g. 12"
                    aria-label="Maximum PAE"
                  />
                </label>
              </>
            )}
            <div className="advanced-filter-field advanced-filter-check">
              <span>Failure Scope</span>
              <label className="advanced-filter-checkbox-row">
                <input
                  type="checkbox"
                  checked={failureOnly}
                  onChange={(e) => onFailureOnlyChange(e.target.checked)}
                />
                <span>Failures / errors only</span>
              </label>
            </div>
            <div className="advanced-filter-field advanced-filter-field-wide task-structure-query-row">
              <div className="task-structure-query-head">
                <span>Structure Query</span>
                <div className="task-structure-mode-switch" role="tablist" aria-label="Structure search mode">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={structureSearchMode === 'exact'}
                    className={`task-structure-mode-btn ${structureSearchMode === 'exact' ? 'active' : ''}`}
                    onClick={() => onStructureSearchModeChange('exact')}
                  >
                    Exact
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={structureSearchMode === 'substructure'}
                    className={`task-structure-mode-btn ${structureSearchMode === 'substructure' ? 'active' : ''}`}
                    onClick={() => onStructureSearchModeChange('substructure')}
                  >
                    Substructure
                  </button>
                </div>
              </div>
              <div className="jsme-editor-container task-structure-jsme-shell">
                <JSMEEditor smiles={structureSearchQuery} height={300} onSmilesChange={onStructureSearchQueryChange} />
              </div>
              <div className={`task-structure-query-status ${structureSearchError ? 'is-error' : ''}`}>
                {structureSearchLoading
                  ? 'Searching...'
                  : structureSearchError
                    ? 'Invalid query'
                    : structureSearchQuery.trim()
                      ? `Matched ${Object.values(structureSearchMatches).filter(Boolean).length}`
                      : 'Draw query'}
              </div>
            </div>
          </div>
          <div className="advanced-filter-actions">
            <button
              type="button"
              className="btn btn-ghost btn-compact"
              onClick={onClearAdvancedFilters}
              disabled={advancedFilterCount === 0}
            >
              <RefreshCcw size={14} />
              Reset Advanced
            </button>
          </div>
        </div>
      )}
    </>
  );
}
