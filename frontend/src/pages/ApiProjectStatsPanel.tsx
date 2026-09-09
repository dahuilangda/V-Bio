/**
 * Project stats panel — extracted from ApiAccessPage.
 * All external refs parameterized: values, callbacks, normalizers, icons.
 */
import { BarChart3, ChevronLeft, ChevronRight, Clock3, KeyRound, Search, ShieldCheck } from 'lucide-react';
import { formatIso } from './apiAccessHelpers';
import { PROJECT_STATS_PAGE_SIZE, type ProjectStatsRow, type ProjectStatsSort, type ProjectStatsWorkflowFilter, type UsageWindow } from './apiAccessHelpers';

interface ProjectStatsPanelProps {
  filteredProjectStatsRows: ProjectStatsRow[];
  projectStatsLoading: boolean;
  projectStatsPage: number;
  projectStatsPageCount: number;
  projectStatsSearch: string;
  projectStatsSort: ProjectStatsSort;
  projectStatsWorkflowFilter: ProjectStatsWorkflowFilter;
  usageWindow: UsageWindow;
  selectedTokenProjectId: string | null;
  pagedProjectStatsRows: ProjectStatsRow[];
  normalizeWorkflowFilter: (v: string) => ProjectStatsWorkflowFilter;
  normalizeSort: (v: unknown) => ProjectStatsSort;
  onSearchChange: (v: string) => void;
  onWorkflowFilterChange: (v: ProjectStatsWorkflowFilter) => void;
  onSortChange: (v: ProjectStatsSort) => void;
  onPageChange: (p: number) => void;
  onWindowChange: (w: UsageWindow) => void;
  onSelectProject: (projectId: string) => void;
  onJumpToBuilder: () => void;
  onJumpToBuilderForProject: (projectId: string) => void;
  onOpenTokenPanel: (projectId: string) => void;
  onOpenRegistry: (projectId: string) => void;
}

export function ProjectStatsPanel(p: ProjectStatsPanelProps) {
  return (
<section className="panel api-project-stats-panel">
  <div className="api-section-head">
    <h2><BarChart3 size={16} /> Project Stats</h2>
  </div>
  <div className="api-project-stats-controls">
    <div className="api-project-stats-controls-left">
      <label className="field api-project-search-field">
        <span><Search size={12} /> Find</span>
        <input
          value={p.projectStatsSearch}
          onChange={(e) => p.onSearchChange(e.target.value)}
          placeholder="project / workflow"
        />
      </label>
      <label className="field api-project-filter-field">
        <span>Workflow</span>
        <select
          value={p.projectStatsWorkflowFilter}
          onChange={(e) => p.onWorkflowFilterChange(p.normalizeWorkflowFilter(e.target.value))}
        >
          <option value="all">All</option>
          <option value="prediction">Prediction</option>
          <option value="virtual_screening">Virtual Screening</option>
          <option value="affinity">Affinity</option>
        </select>
      </label>
      <label className="field api-project-sort-field">
        <span>Sort</span>
        <select
          value={p.projectStatsSort}
          onChange={(e) => p.onSortChange(p.normalizeSort(e.target.value))}
        >
          <option value="last_desc">Last call (newest)</option>
          <option value="last_asc">Last call (oldest)</option>
          <option value="calls_desc">Calls (high to low)</option>
          <option value="calls_asc">Calls (low to high)</option>
          <option value="success_desc">Success (high to low)</option>
          <option value="success_asc">Success (low to high)</option>
        </select>
      </label>
    </div>
    <div className="api-project-stats-controls-right">
      <div className="api-range-switch" role="radiogroup" aria-label="Project stats window">
        <span className="api-range-icon" aria-hidden="true"><Clock3 size={13} /></span>
        {(['7d', '30d', '90d', 'all'] as UsageWindow[]).map((item) => (
          <button
            key={item}
            type="button"
            className={`api-range-item ${p.usageWindow === item ? 'active' : ''}`}
            onClick={() => p.onWindowChange(item)}
            aria-pressed={p.usageWindow === item}
          >
            {item.toUpperCase()}
          </button>
        ))}
      </div>
      <button className="btn btn-secondary api-builder-jump-btn" type="button" onClick={p.onJumpToBuilder}>
        <KeyRound size={13} /> Open Builder
      </button>
    </div>
  </div>
  <div className="table-wrap api-project-table-wrap">
    <table className="table api-project-table">
      <thead>
        <tr>
          <th>Project</th>
          <th>Workflow</th>
          <th>Tokens</th>
          <th>Calls</th>
          <th>Success</th>
          <th>Last Call</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {p.projectStatsLoading ? (
          <tr>
            <td colSpan={7} className="muted">Loading project stats...</td>
          </tr>
        ) : p.pagedProjectStatsRows.length === 0 ? (
          <tr>
            <td colSpan={7} className="muted">No projects.</td>
          </tr>
        ) : (
          p.pagedProjectStatsRows.map((item) => {
            const isSelected = item.project.id === p.selectedTokenProjectId;
            return (
              <tr
                key={item.project.id}
                className={isSelected ? 'row-selected' : ''}
                onClick={() => p.onSelectProject(item.project.id)}
              >
                <td>{item.project.name}</td>
                <td>
                  <span className={`api-workflow-pill workflow-${item.workflowKey}`}>
                    {item.workflowLabel}
                  </span>
                </td>
                <td>
                  <div className="api-project-token-stat">{item.activeTokenCount}/{item.tokenCount}</div>
                </td>
                <td>
                  <div className="api-project-calls-cell">
                    <div className="api-project-calls-head">
                      <BarChart3 size={12} />
                      <strong>{item.totalCalls}</strong>
                    </div>
                  </div>
                </td>
                <td>
                  <span className={`api-project-success-chip ${item.successRate >= 80 ? 'high' : item.successRate >= 50 ? 'mid' : 'low'}`}>
                    {item.successRate.toFixed(1)}%
                  </span>
                </td>
                <td>{item.lastEventAt ? formatIso(item.lastEventAt) : '-'}</td>
                <td>
                  <div className="api-project-manage-actions">
                    <button
                      type="button"
                      className="api-project-builder-btn"
                      title="Open Builder"
                      aria-label="Open Builder"
                      onClick={(e) => {
                        e.stopPropagation();
                        p.onJumpToBuilderForProject(item.project.id);
                      }}
                    >
                      <ChevronRight size={12} />
                    </button>
                    <button
                      type="button"
                      className="api-project-token-view-btn"
                      title="View project tokens"
                      aria-label="View project tokens"
                      onClick={(e) => {
                        e.stopPropagation();
                        p.onOpenTokenPanel(item.project.id);
                      }}
                    >
                      <KeyRound size={12} />
                    </button>
                    <button
                      type="button"
                      className="api-project-manage-btn"
                      title="Open token registry"
                      aria-label="Open token registry"
                      onClick={(e) => {
                        e.stopPropagation();
                        p.onOpenRegistry(item.project.id);
                      }}
                    >
                      <ShieldCheck size={12} />
                    </button>
                  </div>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </table>
  </div>
  {p.filteredProjectStatsRows.length > PROJECT_STATS_PAGE_SIZE && (
    <div className="api-pager">
      <button
        type="button"
        className="icon-btn"
        onClick={() => p.onPageChange(Math.max(1, p.projectStatsPage - 1))}
        disabled={p.projectStatsPage <= 1}
        title="Previous page"
        aria-label="Previous page"
      >
        <ChevronLeft size={14} />
      </button>
      <span className="muted small">{p.projectStatsPage} / {p.projectStatsPageCount}</span>
      <button
        type="button"
        className="icon-btn"
        onClick={() => p.onPageChange(Math.min(p.projectStatsPageCount, p.projectStatsPage + 1))}
        disabled={p.projectStatsPage >= p.projectStatsPageCount}
        title="Next page"
        aria-label="Next page"
      >
        <ChevronRight size={14} />
      </button>
    </div>
  )}
</section>
  );
}
