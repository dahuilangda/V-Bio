import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { terminateTask as terminateBackendTask } from '../api/backendApi';
import { ProjectCopilotModal, readStoredCopilotOpen, writeStoredCopilotOpen } from '../components/copilot/ProjectCopilotModal';
import { SharingModal } from '../components/project/SharingModal';
import { ApiAccessPage } from './ApiAccessPage';
import { useAuth } from '../hooks/useAuth';
import { useCopilotAvailability } from '../hooks/useCopilotAvailability';
import { ProjectTasksHeader } from './projectTasks/ProjectTasksHeader';
import { ProjectTasksWorkspace } from './projectTasks/ProjectTasksWorkspace';
import { exportTaskRowsToExcel } from './projectTasks/exportTaskRowsToExcel';
import { useProjectTaskRowActions } from './projectTasks/useProjectTaskRowActions';
import { useProjectTasksDataLoader } from './projectTasks/useProjectTasksDataLoader';
import { useTaskListFiltering } from './projectTasks/useTaskListFiltering';
import { useProjectTasksWorkspaceContext } from './projectTasks/useProjectTasksWorkspaceContext';
import { useProjectTasksWorkspaceView } from './projectTasks/useProjectTasksWorkspaceView';
import { useProjectTasksApiContextSync } from './projectTasks/useProjectTasksApiContextSync';
import { canEditProject, canManageProjectShares } from '../utils/accessControl';
import { getWorkflowDefinition } from '../utils/workflows';
import type { CopilotPlanAction, ProjectTask } from '../types/models';
import '../styles/project-tasks.css';

const TASK_STATE_FILTER_OPTIONS = ['all', 'DRAFT', 'QUEUED', 'RUNNING', 'SUCCESS', 'FAILURE', 'REVOKED'] as const;
const TASK_SORT_OPTIONS = ['submitted', 'plddt', 'ipsae', 'iptm', 'pae', 'backend', 'seed', 'mode'] as const;
const TASK_SORT_DIRECTION_OPTIONS = ['asc', 'desc'] as const;
const TASK_SUBMITTED_WITHIN_OPTIONS = ['all', '1', '7', '30', '90'] as const;
const TASK_SEED_FILTER_OPTIONS = ['all', 'with_seed', 'without_seed'] as const;
const TASK_PAGE_SIZE_OPTIONS = [8, 12, 20, 50] as const;
const TASK_METRIC_COLUMN_OPTIONS = ['plddt', 'ipsae', 'iptm', 'pae'] as const;
const WORKFLOW_FILTER_OPTIONS = ['all', 'prediction', 'affinity', 'peptide_design', 'lead_optimization'] as const;

function readCopilotText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function readCopilotNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function isOneOf<T extends readonly string[]>(value: string, options: T): value is T[number] {
  return (options as readonly string[]).includes(value);
}

function normalizeCopilotTaskParameterPatch(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object') return {};
  const row = value as Record<string, unknown>;
  const patch: Record<string, unknown> = {};
  const backend = readCopilotText(row.backend).toLowerCase();
  if (backend === 'boltz' || backend === 'alphafold3' || backend === 'protenix') {
    patch.backend = backend;
  }
  const seed = readCopilotNumber(row.seed);
  if (seed !== null) {
    patch.seed = Math.max(0, Math.floor(seed));
  }
  if (Array.isArray(row.componentsAdd)) {
    patch.componentsAdd = row.componentsAdd;
  }
  if (Array.isArray(row.componentsPatch)) {
    patch.componentsPatch = row.componentsPatch;
  }
  if (row.componentsReplacement && typeof row.componentsReplacement === 'object') {
    patch.componentsReplacement = row.componentsReplacement;
  }
  return patch;
}

function summarizeTaskStates(rows: ProjectTask[]): Record<string, number> {
  return rows.reduce<Record<string, number>>((acc, row) => {
    const state = String(row.task_state || 'UNKNOWN').trim().toUpperCase() || 'UNKNOWN';
    acc[state] = (acc[state] || 0) + 1;
    return acc;
  }, {});
}

export function ProjectTasksPage() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { session } = useAuth();
  const copilotAvailable = useCopilotAvailability();
  const { workspaceView, setWorkspaceViewWithUrl } = useProjectTasksWorkspaceView({
    location,
    navigate
  });

  const [exportingExcel, setExportingExcel] = useState(false);
  const [sharedTaskRow, setSharedTaskRow] = useState<ProjectTask | null>(null);
  const [copilotOpen, setCopilotOpen] = useState(() => readStoredCopilotOpen({ contextType: 'task_list', projectId, userId: session?.userId || null }));
  useEffect(() => {
    writeStoredCopilotOpen({ contextType: 'task_list', projectId, userId: session?.userId || null }, copilotOpen);
  }, [copilotOpen, projectId, session?.userId]);
  const [priorityTaskRowIds, setPriorityTaskRowIds] = useState<string[]>([]);
  const initialPage = useMemo(() => {
    const parsed = Number(new URLSearchParams(location.search).get('page') || '');
    if (!Number.isFinite(parsed)) return 1;
    return Math.max(1, Math.floor(parsed));
  }, [location.search]);

  const {
    project,
    tasks,
    loading,
    refreshing,
    error,
    setTasks,
    setError,
  } = useProjectTasksDataLoader({
    projectId,
    sessionUserId: session?.userId || null,
    workspaceView,
    priorityTaskRowIds
  });
  const canEdit = useMemo(() => Boolean(session) && canEditProject(project), [project, session]);
  const canManageShares = useMemo(
    () => canManageProjectShares(project, session?.userId || null),
    [project, session?.userId]
  );

  const {
    taskCountText,
    currentTaskRow,
    backToCurrentTaskHref,
    createTaskHref,
    workspacePairPreference,
    taskRows,
    workflowOptions,
    backendOptions
  } = useProjectTasksWorkspaceContext({
    project,
    tasks
  });

  useProjectTasksApiContextSync({
    workspaceView,
    currentTaskRow,
    location,
    navigate
  });

  const {
    sortKey,
    taskSearch,
    stateFilter,
    workflowFilter,
    backendFilter,
    showAdvancedFilters,
    submittedWithinDays,
    seedFilter,
    failureOnly,
    minPlddt,
    minIptm,
    maxPae,
    structureSearchMode,
    structureSearchQuery,
    structureSearchMatches,
    structureSearchLoading,
    structureSearchError,
    visibleMetricColumns,
    pageSize,
    page,
    advancedFilterCount,
    filteredRows,
    pagedRows,
    totalPages,
    currentPage,
    setTaskSearch,
    setStateFilter,
    setWorkflowFilter,
    setBackendFilter,
    setSortDirection,
    setShowAdvancedFilters,
    setSubmittedWithinDays,
    setSeedFilter,
    setFailureOnly,
    setMinPlddt,
    setMinIptm,
    setMaxPae,
    setStructureSearchMode,
    setStructureSearchQuery,
    setVisibleMetricColumns,
    setPageSize,
    setPage,
    clearAdvancedFilters,
    normalizeSortKey,
    handleSort,
    sortMark,
    jumpToPage,
  } = useTaskListFiltering(taskRows, {
    storageScope: [session?.userId || session?.username || '__anonymous__', projectId].join(':'),
    initialPage,
    suspendPageNormalization: loading || !project
  });

  useEffect(() => {
    if (loading || !project) return;
    const query = new URLSearchParams(location.search);
    const currentQueryPage = Number(query.get('page') || '');
    const normalizedQueryPage = Number.isFinite(currentQueryPage) && currentQueryPage >= 1 ? Math.floor(currentQueryPage) : 1;
    const nextPage = Math.max(1, page);
    if (normalizedQueryPage === nextPage) return;
    if (nextPage > 1) {
      query.set('page', String(nextPage));
    } else {
      query.delete('page');
    }
    const nextSearch = query.toString();
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : ''
      },
      { replace: true }
    );
  }, [loading, location.pathname, location.search, navigate, page, project]);

  useEffect(() => {
    const nextPriorityTaskRowIds = pagedRows
      .map((row) => String(row.task.id || '').trim())
      .filter(Boolean);
    setPriorityTaskRowIds((prev) => {
      if (prev.length === nextPriorityTaskRowIds.length && prev.every((value, index) => value === nextPriorityTaskRowIds[index])) {
        return prev;
      }
      return nextPriorityTaskRowIds;
    });
  }, [pagedRows]);

  const {
    openingTaskId,
    deletingTaskId,
    terminatingTaskId,
    editingTaskNameId,
    editingTaskNameValue,
    savingTaskNameId,
    setEditingTaskNameValue,
    openTask,
    beginTaskNameEdit,
    cancelTaskNameEdit,
    saveTaskNameEdit,
    updateTaskMetadata,
    terminateTask,
    removeTask,
  } = useProjectTaskRowActions({
    project,
    canManageProject: canEdit,
    taskListPage: currentPage,
    navigate,
    setError,
    setTasks,
    terminateBackendTask,
  });

  const downloadExcel = useCallback(async () => {
    if (!project || filteredRows.length === 0) return;
    setExportingExcel(true);
    setError(null);
    try {
      await exportTaskRowsToExcel({
        project,
        filteredRows,
        workspacePairPreference
      });
    } catch (err) {
      setError(err instanceof Error ? `Failed to export Excel: ${err.message}` : 'Failed to export Excel.');
    } finally {
      setExportingExcel(false);
    }
  }, [project, filteredRows, workspacePairPreference, setError]);

  const projectWorkflowKey = useMemo(
    () => (project ? getWorkflowDefinition(project.task_type).key : null),
    [project]
  );
  const supportsApiAccess = projectWorkflowKey === 'prediction' || projectWorkflowKey === 'affinity';
  const apiAccessDisabledReason = useMemo(() => {
    if (projectWorkflowKey === 'lead_optimization') {
      return 'Lead Optimization does not support API Access.';
    }
    if (projectWorkflowKey === 'peptide_design') {
      return 'Peptide Design does not support API Access.';
    }
    return 'API Access is only available for Prediction and Affinity Scoring.';
  }, [projectWorkflowKey]);

  const applyTaskListCopilotAction = useCallback(async (action: CopilotPlanAction) => {
    if (action.id === 'tasks:update_view') {
      const payload = action.payload || {};
      const search = typeof payload.search === 'string' ? payload.search : null;
      if (search !== null) setTaskSearch(search);

      const state = readCopilotText(payload.stateFilter);
      if (isOneOf(state, TASK_STATE_FILTER_OPTIONS)) setStateFilter(state);

      const workflow = readCopilotText(payload.workflowFilter);
      if (isOneOf(workflow, WORKFLOW_FILTER_OPTIONS) && (workflow === 'all' || workflowOptions.includes(workflow))) {
        setWorkflowFilter(workflow);
      }

      const backend = readCopilotText(payload.backendFilter).toLowerCase();
      if (backend === 'all') {
        setBackendFilter('all');
      } else if (backend && backendOptions.includes(backend)) {
        setBackendFilter(backend);
      }

      const sortKeyPatch = readCopilotText(payload.sortKey);
      if (isOneOf(sortKeyPatch, TASK_SORT_OPTIONS)) normalizeSortKey(sortKeyPatch);

      const sortDirectionPatch = readCopilotText(payload.sortDirection);
      if (isOneOf(sortDirectionPatch, TASK_SORT_DIRECTION_OPTIONS)) setSortDirection(sortDirectionPatch);

      const pageSizePatch = readCopilotNumber(payload.pageSize);
      if (pageSizePatch !== null && (TASK_PAGE_SIZE_OPTIONS as readonly number[]).includes(pageSizePatch)) {
        setPageSize(pageSizePatch);
      }

      let advancedUpdated = false;
      const submittedWithinDaysPatch = readCopilotText(payload.submittedWithinDays);
      if (isOneOf(submittedWithinDaysPatch, TASK_SUBMITTED_WITHIN_OPTIONS)) {
        setSubmittedWithinDays(submittedWithinDaysPatch);
        advancedUpdated = true;
      }

      const seedFilterPatch = readCopilotText(payload.seedFilter);
      if (isOneOf(seedFilterPatch, TASK_SEED_FILTER_OPTIONS)) {
        setSeedFilter(seedFilterPatch);
        advancedUpdated = true;
      }

      if (typeof payload.failureOnly === 'boolean') {
        setFailureOnly(payload.failureOnly);
        advancedUpdated = true;
      }

      const minPlddtPatch = readCopilotNumber(payload.minPlddt);
      if (minPlddtPatch !== null) {
        setMinPlddt(String(Math.min(100, Math.max(0, minPlddtPatch))));
        advancedUpdated = true;
      }

      const minIptmPatch = readCopilotNumber(payload.minIptm);
      if (minIptmPatch !== null) {
        setMinIptm(String(Math.min(1, Math.max(0, minIptmPatch))));
        advancedUpdated = true;
      }

      const maxPaePatch = readCopilotNumber(payload.maxPae);
      if (maxPaePatch !== null) {
        setMaxPae(String(Math.max(0, maxPaePatch)));
        advancedUpdated = true;
      }

      if (Array.isArray(payload.visibleMetricColumns)) {
        const requestedColumns = payload.visibleMetricColumns;
        const nextColumns = TASK_METRIC_COLUMN_OPTIONS.filter((key) => requestedColumns.includes(key));
        if (nextColumns.length > 0) {
          setVisibleMetricColumns(nextColumns);
          advancedUpdated = true;
        }
      }

      if (advancedUpdated) setShowAdvancedFilters(true);
      return;
    }
    if (action.id === 'tasks:clear_filters') {
      setTaskSearch('');
      setStateFilter('all');
      setWorkflowFilter('all');
      setBackendFilter('all');
      normalizeSortKey('submitted');
      clearAdvancedFilters();
      setShowAdvancedFilters(false);
      return;
    }
    if (action.id === 'tasks:failure') setStateFilter('FAILURE');
    if (action.id === 'tasks:failure') return;
    if (action.id === 'tasks:running') {
      setStateFilter('RUNNING');
      return;
    }
    if (action.id === 'tasks:queued') {
      setStateFilter('QUEUED');
      return;
    }
    if (action.id === 'tasks:success') {
      setStateFilter('SUCCESS');
      return;
    }
    if (action.id === 'tasks:submitted') {
      handleSort('submitted');
      return;
    }
    if (action.id === 'tasks:sort_plddt') {
      handleSort('plddt');
      return;
    }
    if (action.id === 'tasks:sort_iptm') {
      handleSort('iptm');
      return;
    }
    if (action.id === 'tasks:sort_ipsae') {
      handleSort('ipsae');
      return;
    }
    if (action.id === 'tasks:sort_pae') {
      handleSort('pae');
      return;
    }
    if (action.id === 'tasks:backend_boltz') {
      setBackendFilter('boltz');
      return;
    }
    if (action.id === 'tasks:create') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      navigate(createTaskHref);
      return;
    }
    if (action.id === 'tasks:create_with_sequence') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const sequence = String(action.payload?.protein_sequence || '').trim();
      const components = Array.isArray(action.payload?.components) ? action.payload.components : [];
      const url = new URL(createTaskHref, window.location.origin);
      if (components.length > 0) {
        url.searchParams.set('copilot_components', JSON.stringify(components));
      } else if (sequence) {
        url.searchParams.set('copilot_sequence', sequence);
      }
      navigate(url.pathname + url.search);
      return;
    }
    if (action.id === 'tasks:copy_with_patch') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!project) throw new Error('Project is not loaded.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error('Could not find the task referenced by Copilot.');
      const parameterPatch = normalizeCopilotTaskParameterPatch(action.payload?.parameterPatch);
      if (Object.keys(parameterPatch).length === 0) throw new Error('Copilot did not provide any task changes to apply.');
      const params = new URLSearchParams();
      params.set('tab', 'components');
      params.set('new_task', '1');
      params.set('source_task_row_id', task.id);
      params.set('copilot_parameter_patch', JSON.stringify(parameterPatch));
      if (currentPage > 1) {
        params.set('task_list_page', String(currentPage));
      }
      const targetPath = `/projects/${project.id}`;
      const targetSearch = `?${params.toString()}`;
      const targetUrl = `${targetPath}${targetSearch}`;
      navigate(targetUrl);
      window.setTimeout(() => {
        if (window.location.pathname === targetPath && window.location.search === targetSearch) return;
        window.location.assign(targetUrl);
      }, 150);
      return;
    }
    if (action.id === 'tasks:delete') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error('Could not find the task referenced by Copilot.');
      await removeTask(task);
      return;
    }
    if (action.id === 'tasks:rename') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error('Could not find the task referenced by Copilot.');
      const nextName = typeof action.payload?.taskName === 'string' ? action.payload.taskName : undefined;
      const nextSummary = typeof action.payload?.taskSummary === 'string' ? action.payload.taskSummary : undefined;
      await updateTaskMetadata(task, { name: nextName, summary: nextSummary });
      return;
    }
    if (action.id === 'tasks:cancel') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error('Could not find the task referenced by Copilot.');
      await terminateTask(task);
      return;
    }
    if (action.id === 'tasks:open') {
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error('Could not find the task referenced by Copilot.');
      await openTask(task);
      return;
    }
    throw new Error(`Unsupported Copilot task-list action: ${action.id}`);
  }, [
    canEdit,
    clearAdvancedFilters,
    createTaskHref,
    currentPage,
    handleSort,
    navigate,
    normalizeSortKey,
    openTask,
    project,
    removeTask,
    setBackendFilter,
    setMaxPae,
    setMinIptm,
    setMinPlddt,
    setPageSize,
    setSeedFilter,
    setShowAdvancedFilters,
    setSortDirection,
    setStateFilter,
    setSubmittedWithinDays,
    setTaskSearch,
    setVisibleMetricColumns,
    setWorkflowFilter,
    taskRows,
    terminateTask,
    updateTaskMetadata,
    workflowOptions,
    backendOptions
  ]);

  useEffect(() => {
    if (supportsApiAccess) return;
    if (workspaceView !== 'api') return;
    setWorkspaceViewWithUrl('tasks');
  }, [supportsApiAccess, workspaceView, setWorkspaceViewWithUrl]);

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

  return (
    <div className="page-grid">
      {workspaceView === 'tasks' && (
        <ProjectTasksHeader
          projectName={project.name}
          taskCountText={taskCountText}
          refreshing={refreshing}
          createTaskHref={createTaskHref}
          backToCurrentTaskHref={backToCurrentTaskHref}
          canEdit={canEdit}
          exportingExcel={exportingExcel}
          filteredCount={filteredRows.length}
          onDownloadExcel={() => {
            void downloadExcel();
          }}
          onOpenApi={() => {
            if (!supportsApiAccess) return;
            setWorkspaceViewWithUrl('api');
          }}
          apiAccessDisabled={!supportsApiAccess}
          apiAccessDisabledReason={apiAccessDisabledReason}
        />
      )}

      {error && workspaceView === 'tasks' && <div className="alert error">{error}</div>}

      {workspaceView === 'api' && supportsApiAccess ? (
        <ApiAccessPage />
      ) : (
        <ProjectTasksWorkspace
          totalRowCount={taskRows.length}
          canManageShares={canManageShares}
          taskSearch={taskSearch}
          onTaskSearchChange={setTaskSearch}
          stateFilter={stateFilter}
          onStateFilterChange={setStateFilter}
          workflowFilter={workflowFilter}
          onWorkflowFilterChange={setWorkflowFilter}
          workflowOptions={workflowOptions}
          backendFilter={backendFilter}
          onBackendFilterChange={setBackendFilter}
          backendOptions={backendOptions}
          filteredCount={filteredRows.length}
          showAdvancedFilters={showAdvancedFilters}
          onToggleAdvancedFilters={() => setShowAdvancedFilters((prev) => !prev)}
          advancedFilterCount={advancedFilterCount}
          submittedWithinDays={submittedWithinDays}
          onSubmittedWithinDaysChange={setSubmittedWithinDays}
          seedFilter={seedFilter}
          onSeedFilterChange={setSeedFilter}
          minPlddt={minPlddt}
          onMinPlddtChange={setMinPlddt}
          minIptm={minIptm}
          onMinIptmChange={setMinIptm}
          maxPae={maxPae}
          onMaxPaeChange={setMaxPae}
          failureOnly={failureOnly}
          onFailureOnlyChange={setFailureOnly}
          structureSearchMode={structureSearchMode}
          onStructureSearchModeChange={setStructureSearchMode}
          structureSearchQuery={structureSearchQuery}
          onStructureSearchQueryChange={setStructureSearchQuery}
          structureSearchLoading={structureSearchLoading}
          structureSearchError={structureSearchError}
          structureSearchMatches={structureSearchMatches}
          visibleMetricColumns={visibleMetricColumns}
          onVisibleMetricColumnsChange={setVisibleMetricColumns}
          onClearAdvancedFilters={clearAdvancedFilters}
          sortKey={sortKey}
          sortMark={sortMark}
          onNormalizeSortKey={normalizeSortKey}
          onSort={handleSort}
          filteredRows={filteredRows}
          pagedRows={pagedRows}
          editingTaskNameId={editingTaskNameId}
          editingTaskNameValue={editingTaskNameValue}
          savingTaskNameId={savingTaskNameId}
          openingTaskId={openingTaskId}
          deletingTaskId={deletingTaskId}
          terminatingTaskId={terminatingTaskId}
          onOpenTask={openTask}
          onTerminateTask={terminateTask}
          onRemoveTask={removeTask}
          onOpenShareTask={setSharedTaskRow}
          onBeginTaskNameEdit={beginTaskNameEdit}
          onCancelTaskNameEdit={cancelTaskNameEdit}
          onSaveTaskNameEdit={saveTaskNameEdit}
          onEditingTaskNameValueChange={setEditingTaskNameValue}
          currentPage={currentPage}
          totalPages={totalPages}
          pageSize={pageSize}
          onPageSizeChange={setPageSize}
          onPageChange={setPage}
          onJumpToPage={jumpToPage}
        />
      )}
      {project && session?.userId && sharedTaskRow && canManageShares ? (
        <SharingModal
          open={Boolean(sharedTaskRow)}
          mode="task"
          projectId={project.id}
          projectName={project.name}
          projectTaskId={sharedTaskRow.id}
          taskLabel={String(sharedTaskRow.name || '').trim() || `Task ${String(sharedTaskRow.task_id || sharedTaskRow.id).slice(0, 8)}`}
          currentUserId={session.userId}
          onClose={() => setSharedTaskRow(null)}
        />
      ) : null}
      {project && copilotAvailable && session?.userId ? (
        <ProjectCopilotModal
          open={copilotOpen}
          title="Copilot"
          subtitle={`${filteredRows.length} matched / ${taskRows.length} total`}
          contextType="task_list"
          projectId={project.id}
          currentUserId={session.userId}
          currentUsername={session.username}
          contextPayload={{
            project: { id: project.id, name: project.name, task_type: project.task_type },
            options: { workflowOptions, backendOptions },
            summary: {
              totalTasks: taskRows.length,
              matchedTasks: filteredRows.length,
              allStateCounts: summarizeTaskStates(tasks),
              matchedStateCounts: summarizeTaskStates(filteredRows.map((row) => row.task)),
              currentTask: currentTaskRow
                ? {
                    id: currentTaskRow.id,
                    name: currentTaskRow.name,
                    task_id: currentTaskRow.task_id,
                    state: currentTaskRow.task_state,
                    backend: currentTaskRow.backend
                  }
                : null
            },
            filters: {
              taskSearch,
              stateFilter,
              workflowFilter,
              backendFilter,
              sortKey,
              pageSize,
              page: currentPage,
              submittedWithinDays,
              seedFilter,
              failureOnly,
              minPlddt,
              minIptm,
              maxPae,
              visibleMetricColumns
            },
            rows: filteredRows.slice(0, 60).map((row) => ({
              id: row.task.id,
              name: row.task.name,
              task_id: row.task.task_id,
              state: row.task.task_state,
              backend: row.backendValue,
              workflow: row.workflowKey,
              metrics: row.metrics,
              submitted_at: row.task.submitted_at || row.task.created_at
            }))
          }}
          onApplyPlanAction={applyTaskListCopilotAction}
          onOpen={() => setCopilotOpen(true)}
          onClose={() => setCopilotOpen(false)}
        />
      ) : null}
    </div>
  );
}
