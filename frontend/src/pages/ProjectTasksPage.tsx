import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { terminateTask as terminateBackendTask } from '../api/backendApi';
import { ProjectCopilotModal, readStoredCopilotOpen, writeStoredCopilotOpen } from '../components/copilot/ProjectCopilotModal';
import { SharingModal } from '../components/project/SharingModal';
import { ApiAccessPage } from './ApiAccessPage';
import { useAuth } from '../hooks/useAuth';
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

function findTaskForCopilot(content: string, rows: ProjectTask[]): ProjectTask | null {
  const normalized = content.toLowerCase();
  const quoted = content.match(/["“”']([^"“”']+)["“”']/)?.[1]?.trim();
  const tokens = [
    quoted,
    ...Array.from(content.matchAll(/\b(?:task[_\s-]?id|task|任务)\s*(?:是|为|=|:)?\s*([A-Za-z0-9_.:-]{4,})/gi)).map((match) => match[1])
  ]
    .map((item) => String(item || '').trim().toLowerCase())
    .filter(Boolean);
  for (const row of rows) {
    const taskId = String(row.task_id || '').trim().toLowerCase();
    const rowId = String(row.id || '').trim().toLowerCase();
    const name = String(row.name || '').trim().toLowerCase();
    if (tokens.some((token) => token === taskId || token === rowId || (name && name.includes(token)))) return row;
  }
  return rows.find((row) => normalized.includes(String(row.task_id || '').trim().toLowerCase()) && String(row.task_id || '').trim()) || null;
}

export function ProjectTasksPage() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { session } = useAuth();
  const { workspaceView, setWorkspaceViewWithUrl } = useProjectTasksWorkspaceView({
    location,
    navigate
  });

  const [exportingExcel, setExportingExcel] = useState(false);
  const [sharedTaskRow, setSharedTaskRow] = useState<ProjectTask | null>(null);
  const [copilotOpen, setCopilotOpen] = useState(() => readStoredCopilotOpen({ contextType: 'task_list', projectId }));
  useEffect(() => {
    writeStoredCopilotOpen({ contextType: 'task_list', projectId }, copilotOpen);
  }, [copilotOpen, projectId]);
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

  const buildTaskListCopilotActions = useCallback((content: string): CopilotPlanAction[] => {
    const text = content.toLowerCase();
    const actions: CopilotPlanAction[] = [];
    const matchedTask = findTaskForCopilot(content, taskRows.map((row) => row.task));
    if (text.includes('fail') || text.includes('失败')) {
      actions.push({ id: 'tasks:failure', label: 'Show failed tasks', description: 'Filter the task list to FAILURE.' });
    }
    if (text.includes('running') || text.includes('运行')) {
      actions.push({ id: 'tasks:running', label: 'Show running tasks', description: 'Filter the task list to RUNNING.' });
    }
    if (text.includes('queued') || text.includes('排队')) {
      actions.push({ id: 'tasks:queued', label: 'Show queued tasks', description: 'Filter the task list to QUEUED.' });
    }
    if (text.includes('success') || text.includes('成功')) {
      actions.push({ id: 'tasks:success', label: 'Show successful tasks', description: 'Filter the task list to SUCCESS.' });
    }
    if (text.includes('latest') || text.includes('newest') || text.includes('最近') || text.includes('最新')) {
      actions.push({ id: 'tasks:submitted', label: 'Sort by submitted time', description: 'Sort tasks by submitted time.' });
    }
    if (text.includes('boltz')) {
      actions.push({ id: 'tasks:backend_boltz', label: 'Filter Boltz tasks', description: 'Show tasks submitted to the Boltz backend.' });
    }
    if (text.includes('new task') || text.includes('add task') || text.includes('create task') || text.includes('新增任务') || text.includes('添加任务') || text.includes('新任务')) {
      actions.push({
        id: 'tasks:create',
        label: 'Create new task',
        description: canEdit ? 'Open the project task builder.' : 'This project is read-only for your account.'
      });
    }
    if (matchedTask && (text.includes('open') || text.includes('view') || text.includes('查看') || text.includes('打开'))) {
      actions.push({
        id: 'tasks:open',
        label: 'Open task',
        description: matchedTask.name || matchedTask.task_id || matchedTask.id,
        payload: { taskRowId: matchedTask.id }
      });
    }
    if (matchedTask && (text.includes('cancel') || text.includes('stop') || text.includes('取消') || text.includes('停止'))) {
      actions.push({
        id: 'tasks:cancel',
        label: 'Cancel task',
        description: matchedTask.name || matchedTask.task_id || matchedTask.id,
        payload: { taskRowId: matchedTask.id }
      });
    }
    return actions;
  }, [canEdit, taskRows]);

  const applyTaskListCopilotAction = useCallback(async (action: CopilotPlanAction) => {
    if (action.id === 'tasks:failure') setStateFilter('FAILURE');
    if (action.id === 'tasks:running') setStateFilter('RUNNING');
    if (action.id === 'tasks:queued') setStateFilter('QUEUED');
    if (action.id === 'tasks:success') setStateFilter('SUCCESS');
    if (action.id === 'tasks:submitted') handleSort('submitted');
    if (action.id === 'tasks:backend_boltz') setBackendFilter('boltz');
    if (action.id === 'tasks:create') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      navigate(createTaskHref);
    }
    if (action.id === 'tasks:open' || action.id === 'tasks:cancel') {
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error('Could not find the task referenced by Copilot.');
      if (action.id === 'tasks:open') {
        await openTask(task);
      } else {
        await terminateTask(task);
      }
    }
  }, [canEdit, createTaskHref, handleSort, navigate, openTask, setBackendFilter, setStateFilter, taskRows, terminateTask]);

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
      {project && session?.userId ? (
        <ProjectCopilotModal
          open={copilotOpen}
          title="Task List Copilot"
          subtitle={`${filteredRows.length} matched / ${taskRows.length} total`}
          contextType="task_list"
          projectId={project.id}
          currentUserId={session.userId}
          currentUsername={session.username}
          contextPayload={{
            project: { id: project.id, name: project.name, task_type: project.task_type },
            filters: { taskSearch, stateFilter, workflowFilter, backendFilter, sortKey, pageSize, page: currentPage },
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
          buildPlanActions={buildTaskListCopilotActions}
          onApplyPlanAction={applyTaskListCopilotAction}
          onOpen={() => setCopilotOpen(true)}
          onClose={() => setCopilotOpen(false)}
        />
      ) : null}
    </div>
  );
}
