import { FormEvent, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Activity,
  Atom,
  Beaker,
  Calendar,
  Clock3,
  Compass,
  Dna,
  ExternalLink,
  Filter,
  FolderOpen,
  FlaskConical,
  Hash,
  Plus,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Trash2
} from 'lucide-react';
import { useAuth } from '../hooks/useAuth';
import { useProjects } from '../hooks/useProjects';
import { formatDateTime } from '../utils/date';
import { buildDefaultInputConfig, saveProjectInputConfig } from '../utils/projectInputs';
import { getWorkflowDefinition, type WorkflowKey, WORKFLOWS } from '../utils/workflows';
import type { ProjectTaskCounts, TaskState } from '../types/models';

const workflowIconMap: Record<WorkflowKey, JSX.Element> = {
  prediction: <Dna size={16} />,
  designer: <Compass size={16} />,
  bicyclic_designer: <Atom size={16} />,
  lead_optimization: <FlaskConical size={16} />,
  affinity: <Beaker size={16} />
};

function fallbackCounts(taskState: TaskState, hasTaskId: boolean): ProjectTaskCounts {
  const counts: ProjectTaskCounts = {
    total: 0,
    running: 0,
    success: 0,
    failure: 0,
    queued: 0,
    other: 0
  };
  if (!hasTaskId && taskState === 'DRAFT') return counts;
  counts.total = 1;
  if (taskState === 'RUNNING') counts.running = 1;
  else if (taskState === 'SUCCESS') counts.success = 1;
  else if (taskState === 'FAILURE') counts.failure = 1;
  else if (taskState === 'QUEUED') counts.queued = 1;
  else counts.other = 1;
  return counts;
}

type ProjectActivityFilter = 'all' | 'active' | 'completed' | 'failed' | 'no_tasks';
type ProjectSortBy = 'updated_desc' | 'updated_asc' | 'created_desc' | 'created_asc' | 'name_asc' | 'name_desc';
type UpdatedWithinDaysOption = 'all' | '1' | '7' | '30' | '90';
type MinTaskCountOption = 'all' | '1' | '3' | '5' | '10';

const PROJECTS_PAGE_FILTERS_STORAGE_KEY = 'vbio:projects-page-filters:v1';
const PROJECT_SORT_OPTIONS: ProjectSortBy[] = ['updated_desc', 'updated_asc', 'created_desc', 'created_asc', 'name_asc', 'name_desc'];
const UPDATED_WITHIN_DAYS_OPTIONS: UpdatedWithinDaysOption[] = ['all', '1', '7', '30', '90'];
const MIN_TASK_COUNT_OPTIONS: MinTaskCountOption[] = ['all', '1', '3', '5', '10'];
const PROJECTS_PAGE_SIZE_OPTIONS = [8, 12, 20, 50];

function backendLabel(value: string): string {
  if (value === 'alphafold3') return 'AlphaFold3';
  if (value === 'protenix') return 'Protenix';
  if (value === 'boltz') return 'Boltz-2';
  return value ? value.toUpperCase() : 'Unknown';
}

export function ProjectsPage() {
  const navigate = useNavigate();
  const { session } = useAuth();
  const { projects, loading, error, search, setSearch, createProject, patchProject, softDeleteProject, load } =
    useProjects(session);

  const [showCreate, setShowCreate] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editingProjectNameId, setEditingProjectNameId] = useState<string | null>(null);
  const [editingProjectNameValue, setEditingProjectNameValue] = useState('');
  const [savingProjectNameId, setSavingProjectNameId] = useState<string | null>(null);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowKey>('prediction');
  const [typeFilter, setTypeFilter] = useState<'all' | WorkflowKey>('all');
  const [stateFilter, setStateFilter] = useState<'all' | TaskState>('all');
  const [sortBy, setSortBy] = useState<ProjectSortBy>('created_desc');
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
  const [backendFilter, setBackendFilter] = useState<'all' | string>('all');
  const [activityFilter, setActivityFilter] = useState<ProjectActivityFilter>('all');
  const [updatedWithinDays, setUpdatedWithinDays] = useState<UpdatedWithinDaysOption>('all');
  const [minTaskCount, setMinTaskCount] = useState<MinTaskCountOption>('all');
  const [pageSize, setPageSize] = useState<number>(12);
  const [page, setPage] = useState<number>(1);
  const [filtersHydrated, setFiltersHydrated] = useState(false);
  const hasActiveRuntime = useMemo(
    () =>
      projects.some((item) => {
        const counts = item.task_counts || fallbackCounts(item.task_state, Boolean(item.task_id));
        return counts.queued > 0 || counts.running > 0 || item.task_state === 'QUEUED' || item.task_state === 'RUNNING';
      }),
    [projects]
  );
  const backendOptions = useMemo(
    () =>
      Array.from(
        new Set(
          projects
            .map((project) => String(project.backend || '').trim().toLowerCase())
            .filter(Boolean)
        )
      ).sort((a, b) => a.localeCompare(b)),
    [projects]
  );
  const advancedFilterCount = useMemo(() => {
    let count = 0;
    if (backendFilter !== 'all') count += 1;
    if (activityFilter !== 'all') count += 1;
    if (updatedWithinDays !== 'all') count += 1;
    if (minTaskCount !== 'all') count += 1;
    return count;
  }, [backendFilter, activityFilter, updatedWithinDays, minTaskCount]);
  const clearAdvancedFilters = () => {
    setBackendFilter('all');
    setActivityFilter('all');
    setUpdatedWithinDays('all');
    setMinTaskCount('all');
  };

  useEffect(() => {
    if (typeof window === 'undefined') {
      setFiltersHydrated(true);
      return;
    }
    try {
      const raw = window.localStorage.getItem(PROJECTS_PAGE_FILTERS_STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as Record<string, unknown>;
      const workflowKeys = new Set<WorkflowKey>(WORKFLOWS.map((item) => item.key));
      if (typeof saved.search === 'string') setSearch(saved.search);
      if (typeof saved.typeFilter === 'string' && (saved.typeFilter === 'all' || workflowKeys.has(saved.typeFilter as WorkflowKey))) {
        setTypeFilter(saved.typeFilter as 'all' | WorkflowKey);
      }
      if (
        typeof saved.stateFilter === 'string' &&
        ['all', 'DRAFT', 'QUEUED', 'RUNNING', 'SUCCESS', 'FAILURE', 'REVOKED'].includes(saved.stateFilter)
      ) {
        setStateFilter(saved.stateFilter as 'all' | TaskState);
      }
      if (typeof saved.sortBy === 'string' && PROJECT_SORT_OPTIONS.includes(saved.sortBy as ProjectSortBy)) {
        setSortBy(saved.sortBy as ProjectSortBy);
      }
      if (typeof saved.showAdvancedFilters === 'boolean') {
        setShowAdvancedFilters(saved.showAdvancedFilters);
      }
      if (typeof saved.backendFilter === 'string' && saved.backendFilter.trim()) {
        setBackendFilter(saved.backendFilter.trim().toLowerCase());
      }
      if (typeof saved.activityFilter === 'string' && ['all', 'active', 'completed', 'failed', 'no_tasks'].includes(saved.activityFilter)) {
        setActivityFilter(saved.activityFilter as ProjectActivityFilter);
      }
      if (typeof saved.updatedWithinDays === 'string' && UPDATED_WITHIN_DAYS_OPTIONS.includes(saved.updatedWithinDays as UpdatedWithinDaysOption)) {
        setUpdatedWithinDays(saved.updatedWithinDays as UpdatedWithinDaysOption);
      }
      if (typeof saved.minTaskCount === 'string' && MIN_TASK_COUNT_OPTIONS.includes(saved.minTaskCount as MinTaskCountOption)) {
        setMinTaskCount(saved.minTaskCount as MinTaskCountOption);
      }
      if (typeof saved.pageSize === 'number' && PROJECTS_PAGE_SIZE_OPTIONS.includes(saved.pageSize)) {
        setPageSize(saved.pageSize);
      }
    } catch {
      // ignore malformed storage
    } finally {
      setFiltersHydrated(true);
    }
  }, [setSearch]);

  useEffect(() => {
    if (!filtersHydrated || typeof window === 'undefined') return;
    const snapshot = {
      search,
      typeFilter,
      stateFilter,
      sortBy,
      showAdvancedFilters,
      backendFilter,
      activityFilter,
      updatedWithinDays,
      minTaskCount,
      pageSize
    };
    try {
      window.localStorage.setItem(PROJECTS_PAGE_FILTERS_STORAGE_KEY, JSON.stringify(snapshot));
    } catch {
      // ignore storage quota errors
    }
  }, [
    filtersHydrated,
    search,
    typeFilter,
    stateFilter,
    sortBy,
    showAdvancedFilters,
    backendFilter,
    activityFilter,
    updatedWithinDays,
    minTaskCount,
    pageSize
  ]);

  const countText = useMemo(() => `${projects.length} projects`, [projects.length]);
  const filteredProjects = useMemo(() => {
    const query = search.trim().toLowerCase();
    const updatedWindowMs = updatedWithinDays === 'all' ? null : Number(updatedWithinDays) * 24 * 60 * 60 * 1000;
    const updatedCutoff = updatedWindowMs === null ? null : Date.now() - updatedWindowMs;
    const filtered = projects.filter((project) => {
      const workflowDef = getWorkflowDefinition(project.task_type);
      const counts = project.task_counts || fallbackCounts(project.task_state, Boolean(project.task_id));
      const backendValue = String(project.backend || '').trim().toLowerCase();
      const hasActiveRuntime = counts.queued > 0 || counts.running > 0;
      if (typeFilter !== 'all' && workflowDef.key !== typeFilter) return false;
      if (stateFilter !== 'all') {
        if (stateFilter === 'RUNNING' && counts.running <= 0) return false;
        if (stateFilter === 'SUCCESS' && counts.success <= 0) return false;
        if (stateFilter === 'FAILURE' && counts.failure <= 0) return false;
        if (stateFilter === 'QUEUED' && counts.queued <= 0) return false;
        if ((stateFilter === 'DRAFT' || stateFilter === 'REVOKED') && counts.other <= 0) return false;
      }
      if (backendFilter !== 'all' && backendValue !== backendFilter) return false;
      if (activityFilter === 'active' && !hasActiveRuntime) return false;
      if (activityFilter === 'completed' && (counts.success <= 0 || hasActiveRuntime)) return false;
      if (activityFilter === 'failed' && counts.failure <= 0) return false;
      if (activityFilter === 'no_tasks' && counts.total > 0) return false;
      if (minTaskCount !== 'all' && counts.total < Number(minTaskCount)) return false;
      if (updatedCutoff !== null) {
        const updatedTs = new Date(project.updated_at).getTime();
        if (!Number.isFinite(updatedTs) || updatedTs < updatedCutoff) return false;
      }
      if (!query) return true;
      const haystack = [
        project.name,
        project.summary,
        workflowDef.title,
        workflowDef.shortTitle,
        project.task_state,
        project.task_id,
        project.status_text,
        `run ${counts.running}`,
        `success ${counts.success}`,
        `failure ${counts.failure}`,
        `queued ${counts.queued}`,
        `other ${counts.other}`,
        `total ${counts.total}`,
        backendLabel(backendValue)
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(query);
    });

    const sortByDate = (a: string, b: string, desc: boolean) => {
      const delta = new Date(a).getTime() - new Date(b).getTime();
      return desc ? -delta : delta;
    };

    filtered.sort((a, b) => {
      if (sortBy === 'updated_desc') return sortByDate(a.updated_at, b.updated_at, true);
      if (sortBy === 'updated_asc') return sortByDate(a.updated_at, b.updated_at, false);
      if (sortBy === 'created_desc') return sortByDate(a.created_at, b.created_at, true);
      if (sortBy === 'created_asc') return sortByDate(a.created_at, b.created_at, false);
      if (sortBy === 'name_asc') return a.name.localeCompare(b.name);
      return b.name.localeCompare(a.name);
    });

    return filtered;
  }, [projects, search, typeFilter, stateFilter, sortBy, backendFilter, activityFilter, updatedWithinDays, minTaskCount]);

  const totalPages = useMemo(() => Math.max(1, Math.ceil(filteredProjects.length / pageSize)), [filteredProjects.length, pageSize]);
  const currentPage = Math.min(page, totalPages);
  const jumpToPage = (rawValue: string) => {
    const parsed = Number(rawValue);
    if (!Number.isFinite(parsed)) return;
    setPage(Math.min(totalPages, Math.max(1, Math.floor(parsed))));
  };
  const pagedProjects = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredProjects.slice(start, start + pageSize);
  }, [filteredProjects, currentPage, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [search, typeFilter, stateFilter, sortBy, pageSize, backendFilter, activityFilter, updatedWithinDays, minTaskCount]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  useEffect(() => {
    const onFocus = () => {
      void load({ silent: true, preferBackendStatus: true });
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void load({ silent: true, preferBackendStatus: true });
      }
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [load]);

  useEffect(() => {
    if (!hasActiveRuntime) return;
    const timer = window.setInterval(() => {
      void load({ silent: true, statusOnly: true, preferBackendStatus: true });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [hasActiveRuntime, load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void load({ silent: true, preferBackendStatus: true });
    }, 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  const openCreateModal = () => {
    setWorkflow('prediction');
    setCreateError(null);
    setShowCreate(true);
  };

  const fallbackName = () => {
    const info = getWorkflowDefinition(workflow);
    const now = new Date();
    const y = now.getFullYear();
    const m = String(now.getMonth() + 1).padStart(2, '0');
    const d = String(now.getDate()).padStart(2, '0');
    const hh = String(now.getHours()).padStart(2, '0');
    const mm = String(now.getMinutes()).padStart(2, '0');
    return `${info.shortTitle} ${y}-${m}-${d} ${hh}:${mm}`;
  };

  const onCreate = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setCreateError(null);
    const form = new FormData(e.currentTarget);

    const name = String(form.get('name') || '').trim() || fallbackName();
    const summary = String(form.get('summary') || '').trim();

    setSaving(true);
    try {
      const created = await createProject({
        name,
        summary,
        taskType: workflow,
        backend: 'boltz',
        useMsa: false,
        proteinSequence: '',
        ligandSmiles: ''
      });
      if (!created?.id) {
        throw new Error('Project was created but no project ID was returned from PostgREST.');
      }
      saveProjectInputConfig(created.id, buildDefaultInputConfig());
      setShowCreate(false);
      navigate(`/projects/${created.id}?tab=inputs`);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : 'Failed to create project.');
    } finally {
      setSaving(false);
    }
  };

  const beginProjectNameEdit = (projectId: string, displayName: string) => {
    if (savingProjectNameId) return;
    setEditingProjectNameId(projectId);
    setEditingProjectNameValue(displayName);
    setRenameError(null);
  };

  const cancelProjectNameEdit = () => {
    if (savingProjectNameId) return;
    setEditingProjectNameId(null);
    setEditingProjectNameValue('');
  };

  const saveProjectNameEdit = async (projectId: string, currentName: string) => {
    if (editingProjectNameId !== projectId) return;
    if (savingProjectNameId && savingProjectNameId !== projectId) return;
    const normalizedName = editingProjectNameValue.trim();
    if (!normalizedName) {
      setRenameError('Project name cannot be empty.');
      return;
    }
    if (normalizedName === currentName.trim()) {
      setEditingProjectNameId(null);
      setEditingProjectNameValue('');
      return;
    }
    setSavingProjectNameId(projectId);
    setRenameError(null);
    try {
      await patchProject(projectId, { name: normalizedName });
      setEditingProjectNameId(null);
      setEditingProjectNameValue('');
    } catch (err) {
      setRenameError(err instanceof Error ? err.message : 'Failed to update project name.');
    } finally {
      setSavingProjectNameId(null);
    }
  };

  return (
    <div className="page-grid">
      <section className="page-header">
        <div>
          <h1>Projects</h1>
          <p className="muted">
            {session?.name} · {countText}
          </p>
        </div>
        <button className="btn btn-primary" onClick={openCreateModal}>
          <Plus size={16} />
          New Project
        </button>
      </section>

      <section className="panel">
        <div className="toolbar project-toolbar">
          <div className="project-toolbar-filters">
            <div className="project-filter-field project-filter-field-search">
              <div className="input-wrap search-input">
                <Search size={16} />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search name/summary/type/status..."
                />
              </div>
            </div>
            <label className="project-filter-field">
              <Filter size={14} />
              <select
                className="project-filter-select"
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value as 'all' | WorkflowKey)}
                aria-label="Filter by workflow type"
              >
                <option value="all">All Types</option>
                {WORKFLOWS.map((item) => (
                  <option key={`filter-type-${item.key}`} value={item.key}>
                    {item.shortTitle}
                  </option>
                ))}
              </select>
            </label>
            <label className="project-filter-field">
              <Activity size={14} />
              <select
                className="project-filter-select"
                value={stateFilter}
                onChange={(e) => setStateFilter(e.target.value as 'all' | TaskState)}
                aria-label="Filter by task state"
              >
                <option value="all">All States</option>
                <option value="DRAFT">Draft</option>
                <option value="QUEUED">Queued</option>
                <option value="RUNNING">Running</option>
                <option value="SUCCESS">Success</option>
                <option value="FAILURE">Failure</option>
                <option value="REVOKED">Revoked</option>
              </select>
            </label>
            <label className="project-filter-field">
              <SlidersHorizontal size={14} />
              <select
                className="project-filter-select"
                value={sortBy}
                onChange={(e) =>
                  setSortBy(e.target.value as ProjectSortBy)
                }
                aria-label="Sort projects"
              >
                <option value="updated_desc">Updated: Newest</option>
                <option value="updated_asc">Updated: Oldest</option>
                <option value="created_desc">Created: Newest</option>
                <option value="created_asc">Created: Oldest</option>
                <option value="name_asc">Name: A-Z</option>
                <option value="name_desc">Name: Z-A</option>
              </select>
            </label>
          </div>
          <div className="project-toolbar-meta project-toolbar-meta-rich">
            <span className="muted small">{filteredProjects.length} matched</span>
            <button
              type="button"
              className={`btn btn-ghost btn-compact advanced-filter-toggle ${showAdvancedFilters ? 'active' : ''}`}
              onClick={() => setShowAdvancedFilters((prev) => !prev)}
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
                <span>Backend</span>
                <select value={backendFilter} onChange={(e) => setBackendFilter(e.target.value)} aria-label="Advanced filter backend">
                  <option value="all">All backends</option>
                  {backendOptions.map((value) => (
                    <option key={`advanced-backend-${value}`} value={value}>
                      {backendLabel(value)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="advanced-filter-field">
                <span>Activity</span>
                <select
                  value={activityFilter}
                  onChange={(e) => setActivityFilter(e.target.value as ProjectActivityFilter)}
                  aria-label="Advanced filter activity"
                >
                  <option value="all">All projects</option>
                  <option value="active">Has queued/running tasks</option>
                  <option value="completed">Has success and no runtime</option>
                  <option value="failed">Has failed tasks</option>
                  <option value="no_tasks">No task records</option>
                </select>
              </label>
              <label className="advanced-filter-field">
                <span>Updated</span>
                <select
                  value={updatedWithinDays}
                  onChange={(e) => setUpdatedWithinDays(e.target.value as UpdatedWithinDaysOption)}
                  aria-label="Advanced filter updated window"
                >
                  <option value="all">Any time</option>
                  <option value="1">Last 24 hours</option>
                  <option value="7">Last 7 days</option>
                  <option value="30">Last 30 days</option>
                  <option value="90">Last 90 days</option>
                </select>
              </label>
              <label className="advanced-filter-field">
                <span>Min tasks</span>
                <select
                  value={minTaskCount}
                  onChange={(e) => setMinTaskCount(e.target.value as MinTaskCountOption)}
                  aria-label="Advanced filter minimum task count"
                >
                  <option value="all">No minimum</option>
                  <option value="1">At least 1</option>
                  <option value="3">At least 3</option>
                  <option value="5">At least 5</option>
                  <option value="10">At least 10</option>
                </select>
              </label>
            </div>
            <div className="advanced-filter-actions">
              <span className="advanced-filter-hint">Refine list with backend, activity and recency signals.</span>
              <button
                type="button"
                className="btn btn-ghost btn-compact"
                onClick={clearAdvancedFilters}
                disabled={advancedFilterCount === 0}
              >
                <RefreshCcw size={14} />
                Reset Advanced
              </button>
            </div>
          </div>
        )}

        {(error || renameError) && <div className="alert error">{error || renameError}</div>}

        {loading ? (
          <div className="muted">Loading projects...</div>
        ) : filteredProjects.length === 0 ? (
          <div className="empty-state">
            {projects.length === 0 ? 'No projects yet. Create your first one.' : 'No projects match current filters.'}
          </div>
        ) : (
          <div className="table-wrap project-table-wrap">
            <table className="table project-table">
              <thead>
                <tr>
                  <th>
                    <span className="project-th">
                      <FolderOpen size={13} />
                      Project
                    </span>
                  </th>
                  <th>
                    <span className="project-th">
                      <Filter size={13} />
                      Type
                    </span>
                  </th>
                  <th>
                    <span className="project-th">
                      <Hash size={13} />
                      Tasks
                    </span>
                  </th>
                  <th>
                    <span className="project-th">
                      <Activity size={13} />
                      State
                    </span>
                  </th>
                  <th>
                    <span className="project-th">
                      <Clock3 size={13} />
                      Updated
                    </span>
                  </th>
                  <th>
                    <span className="project-th">
                      <Calendar size={13} />
                      Created
                    </span>
                  </th>
                  <th>
                    <span className="project-th">
                      <SlidersHorizontal size={13} />
                      Actions
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {pagedProjects.map((project) => {
                  const workflowDef = getWorkflowDefinition(project.task_type);
                  const counts = project.task_counts || fallbackCounts(project.task_state, Boolean(project.task_id));
                  const projectName = String(project.name || '').trim() || `Project ${String(project.id || '').slice(0, 8)}`;
                  const isEditingProjectName = editingProjectNameId === project.id;
                  const isSavingProjectName = savingProjectNameId === project.id;
                  return (
                    <tr key={project.id}>
                      <td className="project-col-name">
                        <div className="project-name-row">
                          <FolderOpen size={14} className="project-name-icon" />
                          {isEditingProjectName ? (
                            <input
                              className="project-name-input"
                              value={editingProjectNameValue}
                              onChange={(event) => setEditingProjectNameValue(event.target.value)}
                              onBlur={() => void saveProjectNameEdit(project.id, projectName)}
                              onKeyDown={(event) => {
                                if (event.key === 'Escape') {
                                  event.preventDefault();
                                  cancelProjectNameEdit();
                                  return;
                                }
                                if (event.key === 'Enter') {
                                  event.preventDefault();
                                  void saveProjectNameEdit(project.id, projectName);
                                }
                              }}
                              disabled={isSavingProjectName}
                              autoFocus
                            />
                          ) : (
                            <button
                              type="button"
                              className="project-name-edit-btn"
                              onClick={() => beginProjectNameEdit(project.id, projectName)}
                              disabled={Boolean(savingProjectNameId)}
                              title="Edit project name"
                            >
                              <span className="project-name-link">{projectName}</span>
                            </button>
                          )}
                        </div>
                        {project.summary ? <div className="muted project-row-summary clamp-1">{project.summary}</div> : null}
                      </td>
                      <td>
                        <span className="badge workflow-badge">
                          {workflowIconMap[workflowDef.key]}
                          {workflowDef.shortTitle}
                        </span>
                      </td>
                      <td className="project-col-total">
                        <span className={`project-total-pill ${counts.total === 0 ? 'is-empty' : ''}`}>{counts.total}</span>
                      </td>
                      <td>
                        {counts.total === 0 ? (
                          <span className="project-state-empty">No tasks</span>
                        ) : (
                          <div className="project-state-overview">
                            <div className="project-state-pills">
                              {counts.running > 0 ? (
                                <span className="project-state-pill running" title={`Running ${counts.running}`}>
                                  <span className="project-state-pill-key">R</span>
                                  {counts.running}
                                </span>
                              ) : null}
                              {counts.success > 0 ? (
                                <span className="project-state-pill success" title={`Success ${counts.success}`}>
                                  <span className="project-state-pill-key">S</span>
                                  {counts.success}
                                </span>
                              ) : null}
                              {counts.failure > 0 ? (
                                <span className="project-state-pill failure" title={`Failure ${counts.failure}`}>
                                  <span className="project-state-pill-key">F</span>
                                  {counts.failure}
                                </span>
                              ) : null}
                              {counts.queued > 0 ? (
                                <span className="project-state-pill queued" title={`Queued ${counts.queued}`}>
                                  <span className="project-state-pill-key">Q</span>
                                  {counts.queued}
                                </span>
                              ) : null}
                              {counts.other > 0 ? (
                                <span className="project-state-pill other" title={`Other ${counts.other}`}>
                                  <span className="project-state-pill-key">O</span>
                                  {counts.other}
                                </span>
                              ) : null}
                            </div>
                          </div>
                        )}
                      </td>
                      <td className="project-col-time">{formatDateTime(project.updated_at)}</td>
                      <td className="project-col-time">{formatDateTime(project.created_at)}</td>
                      <td className="project-col-actions">
                        <div className="row gap-6 project-action-row">
                          <Link className="btn btn-ghost btn-compact" to={`/projects/${project.id}`}>
                            <ExternalLink size={14} />
                            Open
                          </Link>
                          <button
                            className="icon-btn danger"
                            onClick={() => {
                              if (window.confirm(`Delete project "${project.name}"?`)) {
                                if (editingProjectNameId === project.id) {
                                  setEditingProjectNameId(null);
                                  setEditingProjectNameValue('');
                                }
                                void softDeleteProject(project.id);
                              }
                            }}
                            title="Delete project"
                          >
                            <Trash2 size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {!loading && filteredProjects.length > 0 && (
          <div className="project-pagination">
            <div className="project-pagination-info muted small">
              Page {currentPage} / {totalPages}
            </div>
            <div className="project-pagination-controls">
              <label className="project-page-size">
                <span className="muted small">Per page</span>
                <select
                  value={String(pageSize)}
                  onChange={(e) => setPageSize(Math.max(1, Number(e.target.value) || 12))}
                  aria-label="Projects per page"
                >
                  <option value="8">8</option>
                  <option value="12">12</option>
                  <option value="20">20</option>
                  <option value="50">50</option>
                </select>
              </label>
              <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => setPage(1)}>
                First
              </button>
              <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                Prev
              </button>
              <button
                className="btn btn-ghost btn-compact"
                disabled={currentPage >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                Next
              </button>
              <button className="btn btn-ghost btn-compact" disabled={currentPage >= totalPages} onClick={() => setPage(totalPages)}>
                Last
              </button>
              <label className="project-page-size">
                <span className="muted small">Go to</span>
                <input
                  type="number"
                  min={1}
                  max={totalPages}
                  value={String(currentPage)}
                  onChange={(e) => jumpToPage(e.target.value)}
                  aria-label="Go to projects page"
                />
              </label>
            </div>
          </div>
        )}
      </section>

      {showCreate && (
        <div
          className="modal-mask"
          onClick={() => {
            setShowCreate(false);
            setCreateError(null);
          }}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>New Project</h2>
            <div className="workflow-grid">
              {WORKFLOWS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={`workflow-card ${workflow === item.key ? 'active' : ''}`}
                  onClick={() => setWorkflow(item.key)}
                >
                  <span className="workflow-card-icon">{workflowIconMap[item.key]}</span>
                  <span className="workflow-card-title">{item.title}</span>
                  <span className="workflow-card-desc">{item.description}</span>
                </button>
              ))}
            </div>

            <form className="form-grid" onSubmit={onCreate}>
              <label className="field">
                <span>Name (optional)</span>
                <input name="name" placeholder={fallbackName()} />
              </label>
              <label className="field">
                <span>Summary (optional)</span>
                <textarea name="summary" rows={3} />
              </label>

              {createError && <div className="alert error">{createError}</div>}

              <div className="row gap-8 end">
                <button type="button" className="btn btn-ghost" onClick={() => setShowCreate(false)}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={saving}>
                  {saving ? 'Creating...' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

    </div>
  );
}
