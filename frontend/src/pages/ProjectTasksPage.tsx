import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { terminateTask as terminateBackendTask } from '../api/backendApi';
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
import type { ProjectTask } from '../types/models';
import '../styles/project-tasks.css';

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
    pageSize,
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
    setPageSize,
    setPage,
    clearAdvancedFilters,
    handleSort,
    sortMark,
    jumpToPage,
  } = useTaskListFiltering(taskRows, {
    storageScope: session?.userId || session?.username || null,
    initialPage
  });

  useEffect(() => {
    const query = new URLSearchParams(location.search);
    const currentQueryPage = Number(query.get('page') || '');
    const normalizedQueryPage = Number.isFinite(currentQueryPage) && currentQueryPage >= 1 ? Math.floor(currentQueryPage) : 1;
    if (normalizedQueryPage === currentPage) return;
    if (currentPage > 1) {
      query.set('page', String(currentPage));
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
  }, [currentPage, location.pathname, location.search, navigate]);

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
          onClearAdvancedFilters={clearAdvancedFilters}
          sortKey={sortKey}
          sortMark={sortMark}
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
    </div>
  );
}
