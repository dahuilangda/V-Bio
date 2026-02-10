import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Clock3, ExternalLink, LoaderCircle, RefreshCcw, Trash2 } from 'lucide-react';
import { deleteProjectTask, getProjectById, listProjectTasks, updateProject } from '../api/supabaseLite';
import { Ligand2DPreview } from '../components/project/Ligand2DPreview';
import { useAuth } from '../hooks/useAuth';
import type { Project, ProjectTask } from '../types/models';
import { formatDateTime, formatDuration } from '../utils/date';
import { normalizeComponentSequence } from '../utils/projectInputs';

function readTaskPrimaryLigand(task: ProjectTask): { smiles: string; isSmiles: boolean } {
  const directLigand = normalizeComponentSequence('ligand', task.ligand_smiles || '');
  if (directLigand) {
    return { smiles: directLigand, isSmiles: true };
  }
  const components = Array.isArray(task.components) ? task.components : [];
  const ligand = components.find((item) => item.type === 'ligand' && normalizeComponentSequence('ligand', item.sequence));
  if (!ligand) {
    return { smiles: '', isSmiles: false };
  }
  const value = normalizeComponentSequence('ligand', ligand.sequence);
  return {
    smiles: value,
    isSmiles: ligand.inputMethod !== 'ccd'
  };
}

function sortProjectTasks(rows: ProjectTask[]): ProjectTask[] {
  return [...rows].sort((a, b) => {
    const at = new Date(a.submitted_at || a.created_at).getTime();
    const bt = new Date(b.submitted_at || b.created_at).getTime();
    return bt - at;
  });
}

function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function readFirstFiniteMetric(data: Record<string, unknown>, paths: string[]): number | null {
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';
type SortKey = 'plddt' | 'iptm' | 'pae' | 'submitted' | 'backend' | 'seed' | 'duration';
type SortDirection = 'asc' | 'desc';

interface TaskConfidenceMetrics {
  plddt: number | null;
  iptm: number | null;
  pae: number | null;
}

interface TaskListRow {
  task: ProjectTask;
  metrics: TaskConfidenceMetrics;
  submittedTs: number;
  backendValue: string;
  durationValue: number | null;
}

function toneForPlddt(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value >= 90) return 'excellent';
  if (value >= 70) return 'good';
  if (value >= 50) return 'medium';
  return 'low';
}

function toneForProbability(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const pct = value <= 1 ? value * 100 : value;
  if (pct >= 90) return 'excellent';
  if (pct >= 70) return 'good';
  if (pct >= 50) return 'medium';
  return 'low';
}

function toneForPae(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value <= 5) return 'excellent';
  if (value <= 10) return 'good';
  if (value <= 20) return 'medium';
  return 'low';
}

function readTaskConfidenceMetrics(task: ProjectTask): TaskConfidenceMetrics {
  const confidence = task.confidence || {};
  const plddtRaw = readFirstFiniteMetric(confidence, ['complex_plddt_protein', 'complex_plddt', 'plddt']);
  const iptmRaw = readFirstFiniteMetric(confidence, ['iptm', 'ligand_iptm', 'protein_iptm']);
  const paeRaw = readFirstFiniteMetric(confidence, ['complex_pde', 'complex_pae', 'pae']);
  return {
    plddt: plddtRaw === null ? null : plddtRaw <= 1 ? plddtRaw * 100 : plddtRaw,
    iptm: normalizeProbability(iptmRaw),
    pae: paeRaw
  };
}

function formatMetric(value: number | null, fractionDigits: number): string {
  if (value === null) return '-';
  return value.toFixed(fractionDigits);
}

function compareNullableNumber(a: number | null, b: number | null, ascending: boolean): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return ascending ? a - b : b - a;
}

function defaultSortDirection(key: SortKey): SortDirection {
  if (key === 'pae' || key === 'backend' || key === 'seed') return 'asc';
  return 'desc';
}

function nextSortDirection(current: SortDirection): SortDirection {
  return current === 'asc' ? 'desc' : 'asc';
}

export function ProjectTasksPage() {
  const { projectId = '' } = useParams();
  const navigate = useNavigate();
  const { session } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [openingTaskId, setOpeningTaskId] = useState<string | null>(null);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('submitted');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [pageSize, setPageSize] = useState<number>(12);
  const [page, setPage] = useState<number>(1);

  const loadData = useCallback(
    async (options?: { silent?: boolean }) => {
      const silent = Boolean(options?.silent);
      if (silent) {
        setRefreshing(true);
      } else {
        setLoading(true);
      }
      setError(null);
      try {
        const [nextProject, taskRows] = await Promise.all([getProjectById(projectId), listProjectTasks(projectId)]);
        if (!nextProject || nextProject.deleted_at) {
          throw new Error('Project not found or already deleted.');
        }
        if (session && nextProject.user_id !== session.userId) {
          throw new Error('You do not have permission to access this project.');
        }
        setProject(nextProject);
        setTasks(sortProjectTasks(taskRows));
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load task history.');
      } finally {
        if (silent) {
          setRefreshing(false);
        } else {
          setLoading(false);
        }
      }
    },
    [projectId, session]
  );

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const taskCountText = useMemo(() => `${tasks.length} tasks`, [tasks.length]);

  const taskRows = useMemo<TaskListRow[]>(() => {
    return tasks.map((task) => {
      const submittedTs = new Date(task.submitted_at || task.created_at).getTime();
      const durationValue = typeof task.duration_seconds === 'number' && Number.isFinite(task.duration_seconds) ? task.duration_seconds : null;
      return {
        task,
        metrics: readTaskConfidenceMetrics(task),
        submittedTs,
        backendValue: String(task.backend || '').trim().toLowerCase(),
        durationValue
      };
    });
  }, [tasks]);

  const filteredRows = useMemo(() => {
    const filtered = [...taskRows];

    filtered.sort((a, b) => {
      if (sortKey === 'submitted') {
        return sortDirection === 'asc' ? a.submittedTs - b.submittedTs : b.submittedTs - a.submittedTs;
      }
      if (sortKey === 'plddt') {
        return compareNullableNumber(a.metrics.plddt, b.metrics.plddt, sortDirection === 'asc');
      }
      if (sortKey === 'iptm') {
        return compareNullableNumber(a.metrics.iptm, b.metrics.iptm, sortDirection === 'asc');
      }
      if (sortKey === 'pae') {
        return compareNullableNumber(a.metrics.pae, b.metrics.pae, sortDirection === 'asc');
      }
      if (sortKey === 'duration') {
        return compareNullableNumber(a.durationValue, b.durationValue, sortDirection === 'asc');
      }
      if (sortKey === 'seed') {
        return compareNullableNumber(a.task.seed, b.task.seed, sortDirection === 'asc');
      }
      const result = a.backendValue.localeCompare(b.backendValue);
      return sortDirection === 'asc' ? result : -result;
    });

    return filtered;
  }, [taskRows, sortKey, sortDirection]);

  const totalPages = useMemo(() => Math.max(1, Math.ceil(filteredRows.length / pageSize)), [filteredRows.length, pageSize]);
  const currentPage = Math.min(page, totalPages);
  const pagedRows = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredRows.slice(start, start + pageSize);
  }, [filteredRows, currentPage, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [sortKey, sortDirection, pageSize]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const openTask = async (task: ProjectTask) => {
    if (!project || !task.task_id) return;
    setOpeningTaskId(task.id);
    setError(null);
    try {
      await updateProject(project.id, {
        task_id: task.task_id,
        task_state: task.task_state,
        status_text: task.status_text || '',
        error_text: task.error_text || '',
        submitted_at: task.submitted_at,
        completed_at: task.completed_at,
        duration_seconds: task.duration_seconds,
        confidence: task.confidence || {},
        affinity: task.affinity || {},
        structure_name: task.structure_name || '',
        backend: task.backend || project.backend
      });
      navigate(`/projects/${project.id}?tab=results`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to open selected task.');
    } finally {
      setOpeningTaskId(null);
    }
  };

  const removeTask = async (task: ProjectTask) => {
    if (!window.confirm(`Delete task "${task.task_id || task.id}" from this project?`)) return;
    setDeletingTaskId(task.id);
    setError(null);
    try {
      await deleteProjectTask(task.id);
      setTasks((prev) => prev.filter((row) => row.id !== task.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete task.');
    } finally {
      setDeletingTaskId(null);
    }
  };

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => nextSortDirection(prev));
      return;
    }
    setSortKey(key);
    setSortDirection(defaultSortDirection(key));
  };

  if (loading && !project) {
    return <div className="centered-page">Loading tasks...</div>;
  }

  if (!project) {
    return (
      <div className="page-grid">
        {error && <div className="alert error">{error}</div>}
        <section className="panel">
          <Link className="btn btn-ghost btn-compact" to="/projects">
            <ArrowLeft size={14} />
            Back to projects
          </Link>
        </section>
      </div>
    );
  }

  const sortMark = (key: SortKey) => {
    if (sortKey !== key) return '↕';
    return sortDirection === 'asc' ? '↑' : '↓';
  };

  return (
    <div className="page-grid">
      <section className="page-header">
        <div className="page-header-left">
          <h1>Tasks</h1>
          <p className="muted">
            {project.name} · {taskCountText}
          </p>
        </div>
        <div className="row gap-8 page-header-actions">
          <Link className="btn btn-ghost btn-compact" to={`/projects/${project.id}`}>
            <ArrowLeft size={14} />
            Back to Project
          </Link>
          <button className="btn btn-ghost btn-compact btn-square" onClick={() => void loadData({ silent: true })} disabled={refreshing}>
            <RefreshCcw size={15} className={refreshing ? 'spin' : undefined} />
          </button>
        </div>
      </section>

      {error && <div className="alert error">{error}</div>}

      <section className="panel">
        <div className="row between">
          <span className="muted small">{filteredRows.length} matched</span>
        </div>

        {filteredRows.length === 0 ? (
          <div className="empty-state">No task runs yet.</div>
        ) : (
          <div className="table-wrap project-table-wrap task-table-wrap">
            <table className="table project-table task-table">
              <thead>
                <tr>
                  <th>
                    <span className="project-th">Ligand 2D</span>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'plddt' ? 'active' : ''}`} onClick={() => handleSort('plddt')}>
                      <span className="project-th">pLDDT <span className="task-th-arrow">{sortMark('plddt')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'iptm' ? 'active' : ''}`} onClick={() => handleSort('iptm')}>
                      <span className="project-th">iPTM <span className="task-th-arrow">{sortMark('iptm')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'pae' ? 'active' : ''}`} onClick={() => handleSort('pae')}>
                      <span className="project-th">PAE <span className="task-th-arrow">{sortMark('pae')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'submitted' ? 'active' : ''}`} onClick={() => handleSort('submitted')}>
                      <span className="project-th"><Clock3 size={13} /> Submitted <span className="task-th-arrow">{sortMark('submitted')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'backend' ? 'active' : ''}`} onClick={() => handleSort('backend')}>
                      <span className="project-th">Backend <span className="task-th-arrow">{sortMark('backend')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'seed' ? 'active' : ''}`} onClick={() => handleSort('seed')}>
                      <span className="project-th">Seed <span className="task-th-arrow">{sortMark('seed')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'duration' ? 'active' : ''}`} onClick={() => handleSort('duration')}>
                      <span className="project-th">Duration <span className="task-th-arrow">{sortMark('duration')}</span></span>
                    </button>
                  </th>
                  <th>
                    <span className="project-th">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {pagedRows.map((row) => {
                  const { task, metrics } = row;
                  const ligand = readTaskPrimaryLigand(task);
                  const runNote = (task.status_text || '').trim();
                  const plddtTone = toneForPlddt(metrics.plddt);
                  const iptmTone = toneForProbability(metrics.iptm);
                  const paeTone = toneForPae(metrics.pae);
                  return (
                    <tr key={task.id}>
                      <td className="task-col-ligand">
                        {ligand.smiles && ligand.isSmiles ? (
                          <div className="task-ligand-thumb">
                            <Ligand2DPreview smiles={ligand.smiles} width={160} height={102} />
                          </div>
                        ) : (
                          <div className="task-ligand-thumb task-ligand-thumb-empty">
                            <span className="muted small">No ligand</span>
                          </div>
                        )}
                      </td>
                      <td className="task-col-metric">
                        <span className={`task-metric-value metric-value-${plddtTone}`}>{formatMetric(metrics.plddt, 1)}</span>
                      </td>
                      <td className="task-col-metric">
                        <span className={`task-metric-value metric-value-${iptmTone}`}>{formatMetric(metrics.iptm, 3)}</span>
                      </td>
                      <td className="task-col-metric">
                        <span className={`task-metric-value metric-value-${paeTone}`}>{formatMetric(metrics.pae, 2)}</span>
                      </td>
                      <td className="project-col-time task-col-submitted">
                        <div className="task-submitted-cell">
                          <span>{formatDateTime(task.submitted_at || task.created_at)}</span>
                          {runNote ? <div className="task-run-note">{runNote}</div> : null}
                        </div>
                      </td>
                      <td className="task-col-backend">
                        <span className="badge task-backend-badge">{task.backend || '-'}</span>
                      </td>
                      <td className="task-col-seed">{task.seed ?? '-'}</td>
                      <td className="project-col-time">{formatDuration(task.duration_seconds)}</td>
                      <td className="project-col-actions">
                        <div className="row gap-6 project-action-row">
                          <button
                            className="btn btn-ghost btn-compact task-action-open"
                            onClick={() => void openTask(task)}
                            disabled={!task.task_id || openingTaskId === task.id}
                            title="Open this task result"
                          >
                            {openingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <ExternalLink size={13} />}
                            Current
                          </button>
                          <button
                            className="icon-btn danger"
                            onClick={() => void removeTask(task)}
                            disabled={deletingTaskId === task.id}
                            title="Delete task"
                          >
                            {deletingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={14} />}
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

        {filteredRows.length > 0 && (
          <div className="project-pagination">
            <div className="project-pagination-info muted small">
              Page {currentPage} / {totalPages}
            </div>
            <div className="project-pagination-controls">
              <label className="project-page-size">
                <span className="muted small">Per page</span>
                <select value={String(pageSize)} onChange={(e) => setPageSize(Math.max(1, Number(e.target.value) || 12))}>
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
    </div>
  );
}
