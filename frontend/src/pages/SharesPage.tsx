import { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronDown, ExternalLink, Eye, Filter, PencilLine, Search, Share2, SlidersHorizontal, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import {
  deleteProjectShare,
  deleteProjectTaskShare,
  listIncomingProjectShares,
  listIncomingTaskShares,
  listOutgoingProjectShares,
  listOutgoingTaskShares,
  updateProjectShareAccessLevel,
  updateProjectTaskShareAccessLevel
} from '../api/supabaseLite';
import type { ProjectShareRecord, ProjectTaskShareRecord, ShareAccessLevel } from '../types/models';
import { formatDateTime } from '../utils/date';

type ShareDirection = 'incoming' | 'outgoing';
type ShareKind = 'project' | 'task';
type ShareKindFilter = 'all' | ShareKind;
type ShareAccessFilter = 'all' | ShareAccessLevel;
type ShareDirectionFilter = 'all' | ShareDirection;
type SharesSortBy = 'latest_desc' | 'project_name_asc' | 'project_name_desc';

const PAGE_SIZE_OPTIONS = [6, 10, 20, 50];

interface ShareRow {
  id: string;
  kind: ShareKind;
  direction: ShareDirection;
  projectId: string;
  projectName: string;
  projectSummary: string;
  taskId: string;
  taskName: string;
  taskSummary: string;
  accessLevel: ShareAccessLevel;
  counterpartyLabel: string;
  counterpartyUsername: string;
  grantedAt: string;
}

interface ShareProjectGroup {
  projectId: string;
  projectName: string;
  projectSummary: string;
  latestGrantedAt: string;
  rows: ShareRow[];
  incomingCount: number;
  outgoingCount: number;
  taskCount: number;
  projectShareCount: number;
}

function normalizeShareRows(params: {
  incomingProjectShares: ProjectShareRecord[];
  outgoingProjectShares: ProjectShareRecord[];
  incomingTaskShares: ProjectTaskShareRecord[];
  outgoingTaskShares: ProjectTaskShareRecord[];
}): ShareRow[] {
  const incomingProjects: ShareRow[] = params.incomingProjectShares.map((row) => ({
    id: row.id,
    kind: 'project',
    direction: 'incoming',
    projectId: row.project_id,
    projectName: String(row.project_name || '').trim() || `Project ${String(row.project_id || '').slice(0, 8)}`,
    projectSummary: String(row.project_summary || '').trim(),
    taskId: '',
    taskName: '',
    taskSummary: '',
    accessLevel: row.access_level,
    counterpartyLabel: String(row.granted_by_name || row.granted_by_username || '').trim() || row.granted_by_user_id || '-',
    counterpartyUsername: String(row.granted_by_username || '').trim(),
    grantedAt: row.created_at
  }));
  const outgoingProjects: ShareRow[] = params.outgoingProjectShares.map((row) => ({
    id: row.id,
    kind: 'project',
    direction: 'outgoing',
    projectId: row.project_id,
    projectName: String(row.project_name || '').trim() || `Project ${String(row.project_id || '').slice(0, 8)}`,
    projectSummary: String(row.project_summary || '').trim(),
    taskId: '',
    taskName: '',
    taskSummary: '',
    accessLevel: row.access_level,
    counterpartyLabel: String(row.target_name || row.target_username || '').trim() || row.user_id || '-',
    counterpartyUsername: String(row.target_username || '').trim(),
    grantedAt: row.created_at
  }));
  const incomingTasks: ShareRow[] = params.incomingTaskShares.map((row) => ({
    id: row.id,
    kind: 'task',
    direction: 'incoming',
    projectId: row.project_id,
    projectName: String(row.project_name || '').trim() || `Project ${String(row.project_id || '').slice(0, 8)}`,
    projectSummary: String(row.project_summary || '').trim(),
    taskId: row.project_task_id,
    taskName: String(row.task_name || '').trim() || `Task ${String(row.project_task_id || '').slice(0, 8)}`,
    taskSummary: String(row.task_summary || '').trim(),
    accessLevel: row.access_level,
    counterpartyLabel: String(row.granted_by_name || row.granted_by_username || '').trim() || row.granted_by_user_id || '-',
    counterpartyUsername: String(row.granted_by_username || '').trim(),
    grantedAt: row.created_at
  }));
  const outgoingTasks: ShareRow[] = params.outgoingTaskShares.map((row) => ({
    id: row.id,
    kind: 'task',
    direction: 'outgoing',
    projectId: row.project_id,
    projectName: String(row.project_name || '').trim() || `Project ${String(row.project_id || '').slice(0, 8)}`,
    projectSummary: String(row.project_summary || '').trim(),
    taskId: row.project_task_id,
    taskName: String(row.task_name || '').trim() || `Task ${String(row.project_task_id || '').slice(0, 8)}`,
    taskSummary: String(row.task_summary || '').trim(),
    accessLevel: row.access_level,
    counterpartyLabel: String(row.target_name || row.target_username || '').trim() || row.user_id || '-',
    counterpartyUsername: String(row.target_username || '').trim(),
    grantedAt: row.created_at
  }));

  return [...incomingProjects, ...outgoingProjects, ...incomingTasks, ...outgoingTasks].sort(
    (left, right) => new Date(right.grantedAt).getTime() - new Date(left.grantedAt).getTime()
  );
}

function shareHref(row: ShareRow): string {
  if (row.kind === 'task') {
    const params = new URLSearchParams({
      tab: 'results',
      task_row_id: row.taskId
    });
    return `/projects/${row.projectId}?${params.toString()}`;
  }
  return `/projects/${row.projectId}`;
}

function groupRowsByProject(rows: ShareRow[], sortBy: SharesSortBy): ShareProjectGroup[] {
  const grouped = new Map<string, ShareProjectGroup>();
  rows.forEach((row) => {
    const current = grouped.get(row.projectId);
    if (!current) {
      grouped.set(row.projectId, {
        projectId: row.projectId,
        projectName: row.projectName,
        projectSummary: row.projectSummary,
        latestGrantedAt: row.grantedAt,
        rows: [row],
        incomingCount: row.direction === 'incoming' ? 1 : 0,
        outgoingCount: row.direction === 'outgoing' ? 1 : 0,
        taskCount: row.kind === 'task' ? 1 : 0,
        projectShareCount: row.kind === 'project' ? 1 : 0
      });
      return;
    }
    current.rows.push(row);
    if (new Date(row.grantedAt).getTime() > new Date(current.latestGrantedAt).getTime()) {
      current.latestGrantedAt = row.grantedAt;
    }
    if (row.direction === 'incoming') current.incomingCount += 1;
    if (row.direction === 'outgoing') current.outgoingCount += 1;
    if (row.kind === 'task') current.taskCount += 1;
    if (row.kind === 'project') current.projectShareCount += 1;
  });

  return Array.from(grouped.values())
    .map((group) => ({
      ...group,
      rows: [...group.rows].sort((left, right) => new Date(right.grantedAt).getTime() - new Date(left.grantedAt).getTime())
    }))
    .sort((left, right) => {
      if (sortBy === 'project_name_asc') return left.projectName.localeCompare(right.projectName, undefined, { sensitivity: 'base' });
      if (sortBy === 'project_name_desc') return right.projectName.localeCompare(left.projectName, undefined, { sensitivity: 'base' });
      return new Date(right.latestGrantedAt).getTime() - new Date(left.latestGrantedAt).getTime();
    });
}

function ShareAccessPills(props: {
  value: ShareAccessLevel;
  disabled?: boolean;
  onChange?: (value: ShareAccessLevel) => void;
}) {
  const { value, disabled = false, onChange } = props;
  return (
    <div className="share-mini-toggle" role="radiogroup" aria-label="Share access level">
      <button
        type="button"
        className={`share-mini-toggle-btn ${value === 'viewer' ? 'is-active' : ''}`}
        aria-pressed={value === 'viewer'}
        disabled={disabled}
        title="Viewer"
        onClick={() => onChange?.('viewer')}
      >
        <Eye size={13} />
      </button>
      <button
        type="button"
        className={`share-mini-toggle-btn ${value === 'editor' ? 'is-active' : ''}`}
        aria-pressed={value === 'editor'}
        disabled={disabled}
        title="Editor"
        onClick={() => onChange?.('editor')}
      >
        <PencilLine size={13} />
      </button>
    </div>
  );
}

function ShareRowCard(props: {
  row: ShareRow;
  savingRowId: string | null;
  onChangeAccess: (row: ShareRow, nextAccessLevel: ShareAccessLevel) => void;
  onRevoke: (row: ShareRow) => void;
}) {
  const { row, savingRowId, onChangeAccess, onRevoke } = props;
  const isSaving = savingRowId === row.id;
  const title = row.kind === 'task' ? row.taskName : row.projectName;
  const summary = row.kind === 'task' ? row.taskSummary : row.projectSummary;
  const canManage = row.direction === 'outgoing';

  return (
    <article className="shares-item-card">
      <div className="shares-item-top">
        <div className="shares-item-main">
          <div className="shares-item-head">
            <strong>{title}</strong>
            <div className="shares-item-badges">
              <span className={`badge shares-direction-badge ${row.direction === 'incoming' ? 'is-incoming' : 'is-outgoing'}`}>
                {row.direction === 'incoming' ? 'Received' : 'Sent'}
              </span>
              <span className={`badge shares-kind-badge ${row.kind === 'task' ? 'is-task' : 'is-project'}`}>
                {row.kind === 'task' ? 'Task' : 'Project'}
              </span>
            </div>
          </div>
        </div>

        <div className="shares-item-side">
          {canManage ? (
            <ShareAccessPills
              value={row.accessLevel}
              disabled={isSaving}
              onChange={(nextAccessLevel) => {
                if (nextAccessLevel === row.accessLevel) return;
                onChangeAccess(row, nextAccessLevel);
              }}
            />
          ) : (
            <span className={`badge shares-access-badge ${row.accessLevel === 'editor' ? 'is-editor' : 'is-viewer'}`}>
              {row.accessLevel}
            </span>
          )}
          <div className="shares-actions">
            <Link className="task-row-action-btn" to={shareHref(row)} title="Open shared item" aria-label="Open shared item">
              <ExternalLink size={14} />
            </Link>
            {canManage ? (
              <button
                type="button"
                className="task-row-action-btn danger"
                onClick={() => onRevoke(row)}
                disabled={isSaving}
                title="Revoke share"
                aria-label="Revoke share"
              >
                <Trash2 size={14} />
              </button>
            ) : null}
          </div>
        </div>
      </div>

      <div className="shares-item-bottom muted small">
        <span>{canManage ? 'Shared to' : 'Shared by'} {row.counterpartyLabel}</span>
        {row.counterpartyUsername ? <code>@{row.counterpartyUsername}</code> : null}
        {summary ? <span className="shares-item-summary">{summary}</span> : null}
        <span>{formatDateTime(row.grantedAt)}</span>
      </div>
    </article>
  );
}

function ProjectShareCard(props: {
  group: ShareProjectGroup;
  collapsed: boolean;
  onToggle: (projectId: string) => void;
  savingRowId: string | null;
  onChangeAccess: (row: ShareRow, nextAccessLevel: ShareAccessLevel) => void;
  onRevoke: (row: ShareRow) => void;
}) {
  const { group, collapsed, onToggle, savingRowId, onChangeAccess, onRevoke } = props;
  return (
    <article className="project-card shares-project-card">
      <div className="project-card-main shares-project-card-head">
        <div className="project-title-block">
          <h3>
            <Link to={`/projects/${group.projectId}`}>{group.projectName}</Link>
          </h3>
          {group.projectSummary ? <p className="project-summary muted">{group.projectSummary}</p> : null}
        </div>
        <div className="project-badges shares-project-badges">
          {group.incomingCount > 0 ? <span className="badge shares-direction-badge is-incoming">{group.incomingCount} received</span> : null}
          {group.outgoingCount > 0 ? <span className="badge shares-direction-badge is-outgoing">{group.outgoingCount} sent</span> : null}
          {group.projectShareCount > 0 ? <span className="badge badge-muted">{group.projectShareCount} project</span> : null}
          {group.taskCount > 0 ? <span className="badge badge-muted">{group.taskCount} tasks</span> : null}
          <button
            type="button"
            className={`shares-project-toggle ${collapsed ? 'is-collapsed' : ''}`}
            onClick={() => onToggle(group.projectId)}
            aria-expanded={!collapsed}
            title={collapsed ? 'Expand project shares' : 'Collapse project shares'}
          >
            <span className="share-count-pill">{group.rows.length}</span>
            <ChevronDown size={15} />
          </button>
        </div>
      </div>

      <div className="project-card-footer shares-project-card-foot">
        <span className="muted small">Latest share {formatDateTime(group.latestGrantedAt)}</span>
      </div>

      {!collapsed ? (
        <div className="shares-project-items">
          {group.rows.map((row) => (
            <ShareRowCard
              key={row.id}
              row={row}
              savingRowId={savingRowId}
              onChangeAccess={onChangeAccess}
              onRevoke={onRevoke}
            />
          ))}
        </div>
      ) : null}
    </article>
  );
}

export function SharesPage() {
  const { session } = useAuth();
  const [rows, setRows] = useState<ShareRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingRowId, setSavingRowId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [directionFilter, setDirectionFilter] = useState<ShareDirectionFilter>('all');
  const [kindFilter, setKindFilter] = useState<ShareKindFilter>('all');
  const [accessFilter, setAccessFilter] = useState<ShareAccessFilter>('all');
  const [sortBy, setSortBy] = useState<SharesSortBy>('latest_desc');
  const [pageSize, setPageSize] = useState<number>(10);
  const [page, setPage] = useState<number>(1);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [prefsHydrated, setPrefsHydrated] = useState(false);

  const sharesPrefsStorageKey = useMemo(() => {
    const sessionIdentity =
      String(session?.userId || '').trim() ||
      String(session?.username || '').trim().toLowerCase() ||
      '__anonymous__';
    return `vbio:shares-page:v3:${sessionIdentity}`;
  }, [session?.userId, session?.username]);

  const load = useCallback(async () => {
    if (!session?.userId) {
      setRows([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const [incomingProjectShares, outgoingProjectShares, incomingTaskShares, outgoingTaskShares] = await Promise.all([
        listIncomingProjectShares(session.userId),
        listOutgoingProjectShares(session.userId),
        listIncomingTaskShares(session.userId),
        listOutgoingTaskShares(session.userId)
      ]);
      setRows(
        normalizeShareRows({
          incomingProjectShares,
          outgoingProjectShares,
          incomingTaskShares,
          outgoingTaskShares
        })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load share records.');
    } finally {
      setLoading(false);
    }
  }, [session?.userId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setPrefsHydrated(false);
    if (typeof window === 'undefined') {
      setPrefsHydrated(true);
      return;
    }
    try {
      const raw = window.localStorage.getItem(sharesPrefsStorageKey);
      if (!raw) return;
      const parsed = JSON.parse(raw) as {
        search?: string;
        directionFilter?: ShareDirectionFilter;
        kindFilter?: ShareKindFilter;
        accessFilter?: ShareAccessFilter;
        sortBy?: SharesSortBy;
        pageSize?: number;
        collapsedProjects?: Record<string, boolean>;
      };
      if (typeof parsed.search === 'string') setSearch(parsed.search);
      if (parsed.directionFilter && ['all', 'incoming', 'outgoing'].includes(parsed.directionFilter)) {
        setDirectionFilter(parsed.directionFilter);
      }
      if (parsed.kindFilter && ['all', 'project', 'task'].includes(parsed.kindFilter)) {
        setKindFilter(parsed.kindFilter);
      }
      if (parsed.accessFilter && ['all', 'viewer', 'editor'].includes(parsed.accessFilter)) {
        setAccessFilter(parsed.accessFilter);
      }
      if (parsed.sortBy && ['latest_desc', 'project_name_asc', 'project_name_desc'].includes(parsed.sortBy)) {
        setSortBy(parsed.sortBy);
      }
      if (typeof parsed.pageSize === 'number' && PAGE_SIZE_OPTIONS.includes(parsed.pageSize)) {
        setPageSize(parsed.pageSize);
      }
      if (parsed.collapsedProjects) setCollapsedProjects(parsed.collapsedProjects);
    } catch {
      // ignore malformed storage
    } finally {
      setPrefsHydrated(true);
    }
  }, [sharesPrefsStorageKey]);

  useEffect(() => {
    if (!prefsHydrated || typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(
        sharesPrefsStorageKey,
        JSON.stringify({
          search,
          directionFilter,
          kindFilter,
          accessFilter,
          sortBy,
          pageSize,
          collapsedProjects
        })
      );
    } catch {
      // ignore storage quota errors
    }
  }, [prefsHydrated, sharesPrefsStorageKey, search, directionFilter, kindFilter, accessFilter, sortBy, pageSize, collapsedProjects]);

  const incomingCount = useMemo(() => rows.filter((row) => row.direction === 'incoming').length, [rows]);
  const outgoingCount = useMemo(() => rows.filter((row) => row.direction === 'outgoing').length, [rows]);

  const filteredRows = useMemo(() => {
    const query = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (directionFilter !== 'all' && row.direction !== directionFilter) return false;
      if (kindFilter !== 'all' && row.kind !== kindFilter) return false;
      if (accessFilter !== 'all' && row.accessLevel !== accessFilter) return false;
      if (!query) return true;
      const haystack = [
        row.projectName,
        row.projectSummary,
        row.taskName,
        row.taskSummary,
        row.counterpartyLabel,
        row.counterpartyUsername,
        row.direction,
        row.kind,
        row.accessLevel
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [accessFilter, directionFilter, kindFilter, rows, search]);

  const groupedProjects = useMemo(() => groupRowsByProject(filteredRows, sortBy), [filteredRows, sortBy]);
  const totalPages = useMemo(() => Math.max(1, Math.ceil(groupedProjects.length / pageSize)), [groupedProjects.length, pageSize]);
  const currentPage = Math.min(page, totalPages);
  const pagedProjects = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return groupedProjects.slice(start, start + pageSize);
  }, [currentPage, groupedProjects, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [search, directionFilter, kindFilter, accessFilter, sortBy, pageSize]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const jumpToPage = (rawValue: string) => {
    const parsed = Number(rawValue);
    if (!Number.isFinite(parsed)) return;
    setPage(Math.min(totalPages, Math.max(1, Math.floor(parsed))));
  };

  const handleToggleProject = useCallback((projectId: string) => {
    setCollapsedProjects((current) => ({
      ...current,
      [projectId]: !current[projectId]
    }));
  }, []);

  const handleChangeAccess = useCallback(async (row: ShareRow, nextAccessLevel: ShareAccessLevel) => {
    setSavingRowId(row.id);
    setError(null);
    try {
      if (row.kind === 'task') {
        await updateProjectTaskShareAccessLevel(row.id, nextAccessLevel);
      } else {
        await updateProjectShareAccessLevel(row.id, nextAccessLevel);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update share access.');
    } finally {
      setSavingRowId(null);
    }
  }, [load]);

  const handleRevoke = useCallback(async (row: ShareRow) => {
    const subject = row.kind === 'task' ? row.taskName : row.projectName;
    if (!window.confirm(`Revoke ${row.kind} share for "${subject}"?`)) return;
    setSavingRowId(row.id);
    setError(null);
    try {
      if (row.kind === 'task') {
        await deleteProjectTaskShare(row.id);
      } else {
        await deleteProjectShare(row.id);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke share.');
    } finally {
      setSavingRowId(null);
    }
  }, [load]);

  return (
    <div className="page-grid shares-page">
      <section className="page-header">
        <div className="page-header-left">
          <h1>Shares</h1>
          <p className="muted">Browse all incoming and outgoing shares in one list, grouped by project.</p>
        </div>
        <div className="row gap-8 page-header-actions page-header-actions-minimal">
          <div className="project-compact-meta">
            <span className="meta-chip">
              <Share2 size={13} />
              {incomingCount} received
            </span>
            <span className="meta-chip">
              <Share2 size={13} />
              {outgoingCount} sent
            </span>
          </div>
        </div>
      </section>

      <section className="panel toolbar project-toolbar shares-toolbar">
        <div className="project-toolbar-filters">
          <div className="project-filter-field project-filter-field-search shares-search-field">
            <div className="input-wrap search-input">
              <Search size={16} />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search project, task, user..."
                aria-label="Search shares"
              />
            </div>
          </div>
          <label className="project-filter-field">
            <Share2 size={14} />
            <select
              className="project-filter-select"
              value={directionFilter}
              onChange={(event) => setDirectionFilter(event.target.value as ShareDirectionFilter)}
              aria-label="Filter by share direction"
            >
              <option value="all">All Directions</option>
              <option value="incoming">Received</option>
              <option value="outgoing">Sent</option>
            </select>
          </label>
          <label className="project-filter-field">
            <Filter size={14} />
            <select
              className="project-filter-select"
              value={kindFilter}
              onChange={(event) => setKindFilter(event.target.value as ShareKindFilter)}
              aria-label="Filter by share item type"
            >
              <option value="all">All Items</option>
              <option value="project">Projects</option>
              <option value="task">Tasks</option>
            </select>
          </label>
          <label className="project-filter-field">
            <Eye size={14} />
            <select
              className="project-filter-select"
              value={accessFilter}
              onChange={(event) => setAccessFilter(event.target.value as ShareAccessFilter)}
              aria-label="Filter by share access"
            >
              <option value="all">Any Access</option>
              <option value="viewer">Viewer</option>
              <option value="editor">Editor</option>
            </select>
          </label>
          <label className="project-filter-field">
            <SlidersHorizontal size={14} />
            <select
              className="project-filter-select"
              value={sortBy}
              onChange={(event) => setSortBy(event.target.value as SharesSortBy)}
              aria-label="Sort shares"
            >
              <option value="latest_desc">Latest shared</option>
              <option value="project_name_asc">Project name A-Z</option>
              <option value="project_name_desc">Project name Z-A</option>
            </select>
          </label>
        </div>
        <div className="project-toolbar-meta project-toolbar-meta-rich">
          <span className="muted small">{groupedProjects.length} projects</span>
          <span className="muted small">{filteredRows.length} shares</span>
        </div>
      </section>

      <section className="panel shares-list-panel">
        {error ? <div className="alert error">{error}</div> : null}
        {loading ? (
          <div className="muted">Loading shares...</div>
        ) : groupedProjects.length === 0 ? (
          <div className="empty-state">No share records match the current filters.</div>
        ) : (
          <>
            <div className="project-list shares-project-list">
              {pagedProjects.map((group) => (
                <ProjectShareCard
                  key={group.projectId}
                  group={group}
                  collapsed={Boolean(collapsedProjects[group.projectId])}
                  onToggle={handleToggleProject}
                  savingRowId={savingRowId}
                  onChangeAccess={handleChangeAccess}
                  onRevoke={handleRevoke}
                />
              ))}
            </div>

            <div className="project-pagination">
              <div className="project-pagination-info muted small">
                Page {currentPage} / {totalPages}
              </div>
              <div className="project-pagination-controls">
                <label className="project-page-size">
                  <span className="muted small">Per page</span>
                  <select
                    value={String(pageSize)}
                    onChange={(event) => setPageSize(Math.max(1, Number(event.target.value) || 10))}
                    aria-label="Share projects per page"
                  >
                    {PAGE_SIZE_OPTIONS.map((option) => (
                      <option key={option} value={String(option)}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => setPage(1)}>
                  First
                </button>
                <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
                  Prev
                </button>
                <button className="btn btn-ghost btn-compact" disabled={currentPage >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}>
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
                    onChange={(event) => jumpToPage(event.target.value)}
                    aria-label="Go to shares page"
                  />
                </label>
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
