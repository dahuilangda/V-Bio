import { FormEvent, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Atom,
  Beaker,
  Compass,
  Dna,
  FlaskConical,
  Plus,
  Search,
  Trash2
} from 'lucide-react';
import { useAuth } from '../hooks/useAuth';
import { useProjects } from '../hooks/useProjects';
import { formatDateTime } from '../utils/date';
import { buildDefaultInputConfig, saveProjectInputConfig } from '../utils/projectInputs';
import { getWorkflowDefinition, type WorkflowKey, WORKFLOWS } from '../utils/workflows';

const workflowIconMap: Record<WorkflowKey, JSX.Element> = {
  prediction: <Dna size={16} />,
  designer: <Compass size={16} />,
  bicyclic_designer: <Atom size={16} />,
  lead_optimization: <FlaskConical size={16} />,
  affinity: <Beaker size={16} />
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

  const countText = useMemo(() => `${projects.length} projects`, [projects.length]);

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
        <div className="row gap-8">
          <button className="btn btn-ghost" onClick={() => void load()}>
            Refresh
          </button>
          <button className="btn btn-primary" onClick={openCreateModal}>
            <Plus size={16} />
            New Project
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="toolbar">
          <div className="input-wrap search-input">
            <Search size={16} />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search projects..."
            />
          </div>
        </div>

        {error && <div className="alert error">{error}</div>}

        <div className="project-list">
          {loading ? (
            <div className="muted">Loading projects...</div>
          ) : projects.length === 0 ? (
            <div className="empty-state">No projects yet. Create your first one.</div>
          ) : (
            projects.map((project) => (
              <article key={project.id} className="project-card">
                <div className="project-card-main">
                  <div>
                    <h3>
                      <Link to={`/projects/${project.id}`}>{project.name}</Link>
                    </h3>
                    <p className="muted clamp-2">{project.summary || 'No summary'}</p>
                  </div>
                  <div className="project-badges">
                    <span className="badge workflow-badge">
                      {workflowIconMap[getWorkflowDefinition(project.task_type).key]}
                      {getWorkflowDefinition(project.task_type).shortTitle}
                    </span>
                    <span className={`badge state-${project.task_state.toLowerCase()}`}>{project.task_state}</span>
                  </div>
                </div>

                <div className="project-card-footer">
                  <span>Updated {formatDateTime(project.updated_at)}</span>
                  <button
                    className="icon-btn"
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
              </article>
            ))
          )}
        </div>
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
