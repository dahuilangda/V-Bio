import { FormEvent, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Activity,
  Atom,
  Beaker,
  Calendar,
  CheckCircle2,
  Clock3,
  Compass,
  Dna,
  ExternalLink,
  Filter,
  FolderOpen,
  FlaskConical,
  LoaderCircle,
  Plus,
  Search,
  SlidersHorizontal,
  Trash2
} from 'lucide-react';
import { useAuth } from '../hooks/useAuth';
import { useProjects } from '../hooks/useProjects';
import { formatDateTime } from '../utils/date';
import { buildDefaultInputConfig, saveProjectInputConfig } from '../utils/projectInputs';
import { getWorkflowDefinition, type WorkflowKey, WORKFLOWS } from '../utils/workflows';
import type { TaskState } from '../types/models';

const workflowIconMap: Record<WorkflowKey, JSX.Element> = {
  prediction: <Dna size={16} />,
  designer: <Compass size={16} />,
  bicyclic_designer: <Atom size={16} />,
  lead_optimization: <FlaskConical size={16} />,
  affinity: <Beaker size={16} />
};

const stateIconMap: Record<TaskState, JSX.Element> = {
  DRAFT: <SlidersHorizontal size={13} />,
  QUEUED: <Clock3 size={13} />,
  RUNNING: <LoaderCircle size={13} className="spin" />,
  SUCCESS: <CheckCircle2 size={13} />,
  FAILURE: <Activity size={13} />,
  REVOKED: <Activity size={13} />
};

export function ProjectsPage() {
  const navigate = useNavigate();
  const { session } = useAuth();
  const { projects, loading, error, search, setSearch, createProject, softDeleteProject, load } =
    useProjects(session);

  const [showCreate, setShowCreate] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [workflow, setWorkflow] = useState<WorkflowKey>('prediction');
  const [typeFilter, setTypeFilter] = useState<'all' | WorkflowKey>('all');
  const [stateFilter, setStateFilter] = useState<'all' | TaskState>('all');
  const [sortBy, setSortBy] = useState<
    'updated_desc' | 'updated_asc' | 'created_desc' | 'created_asc' | 'name_asc' | 'name_desc'
  >('updated_desc');
  const [pageSize, setPageSize] = useState<number>(12);
  const [page, setPage] = useState<number>(1);
  const hasActiveRuntime = useMemo(
    () => projects.some((item) => item.task_state === 'QUEUED' || item.task_state === 'RUNNING'),
    [projects]
  );

  const countText = useMemo(() => `${projects.length} projects`, [projects.length]);
  const filteredProjects = useMemo(() => {
    const query = search.trim().toLowerCase();
    const filtered = projects.filter((project) => {
      const workflowDef = getWorkflowDefinition(project.task_type);
      if (typeFilter !== 'all' && workflowDef.key !== typeFilter) return false;
      if (stateFilter !== 'all' && project.task_state !== stateFilter) return false;
      if (!query) return true;
      const haystack = [
        project.name,
        project.summary,
        workflowDef.title,
        workflowDef.shortTitle,
        project.task_state,
        project.task_id,
        project.status_text
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
  }, [projects, search, typeFilter, stateFilter, sortBy]);

  const totalPages = useMemo(() => Math.max(1, Math.ceil(filteredProjects.length / pageSize)), [filteredProjects.length, pageSize]);
  const currentPage = Math.min(page, totalPages);
  const pagedProjects = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredProjects.slice(start, start + pageSize);
  }, [filteredProjects, currentPage, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [search, typeFilter, stateFilter, sortBy, pageSize]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  useEffect(() => {
    const onFocus = () => {
      void load();
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void load();
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
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [hasActiveRuntime, load]);

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

    setSaving(true);
    try {
      const created = await createProject({
        name,
        summary: '',
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
                  setSortBy(
                    e.target.value as
                      | 'updated_desc'
                      | 'updated_asc'
                      | 'created_desc'
                      | 'created_asc'
                      | 'name_asc'
                      | 'name_desc'
                  )
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
          <div className="project-toolbar-meta muted small">
            {filteredProjects.length} matched
          </div>
        </div>

        {error && <div className="alert error">{error}</div>}

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
                  return (
                    <tr key={project.id}>
                      <td className="project-col-name">
                        <div className="project-name-row">
                          <FolderOpen size={14} className="project-name-icon" />
                          <Link className="project-name-link" to={`/projects/${project.id}`}>
                            {project.name}
                          </Link>
                        </div>
                        {project.summary ? <div className="muted project-row-summary clamp-1">{project.summary}</div> : null}
                      </td>
                      <td>
                        <span className="badge workflow-badge">
                          {workflowIconMap[workflowDef.key]}
                          {workflowDef.shortTitle}
                        </span>
                      </td>
                      <td>
                        <span className={`badge project-state-badge state-${project.task_state.toLowerCase()}`}>
                          {stateIconMap[project.task_state]}
                          {project.task_state}
                        </span>
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
