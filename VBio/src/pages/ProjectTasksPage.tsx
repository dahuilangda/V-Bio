import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { terminateTask as terminateBackendTask } from '../api/backendApi';
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
import { getWorkflowDefinition } from '../utils/workflows';
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
  });

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
  } = useTaskListFiltering(taskRows);

  const {
    openingTaskId,
    deletingTaskId,
    editingTaskNameId,
    editingTaskNameValue,
    savingTaskNameId,
    setEditingTaskNameValue,
    openTask,
    beginTaskNameEdit,
    cancelTaskNameEdit,
    saveTaskNameEdit,
    removeTask,
  } = useProjectTaskRowActions({
    project,
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

  const isLeadOptimizationProject = useMemo(
    () => (project ? getWorkflowDefinition(project.task_type).key === 'lead_optimization' : false),
    [project]
  );

  useEffect(() => {
    if (!isLeadOptimizationProject) return;
    if (workspaceView !== 'api') return;
    setWorkspaceViewWithUrl('tasks');
  }, [isLeadOptimizationProject, workspaceView, setWorkspaceViewWithUrl]);

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
          exportingExcel={exportingExcel}
          filteredCount={filteredRows.length}
          onDownloadExcel={() => {
            void downloadExcel();
          }}
          onOpenApi={() => {
            if (isLeadOptimizationProject) return;
            setWorkspaceViewWithUrl('api');
          }}
          apiAccessDisabled={isLeadOptimizationProject}
          apiAccessDisabledReason="Lead Optimization does not support API Access."
        />
      )}

      {error && workspaceView === 'tasks' && <div className="alert error">{error}</div>}

      {workspaceView === 'api' && !isLeadOptimizationProject ? (
        <ApiAccessPage />
      ) : (
        <ProjectTasksWorkspace
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
          onOpenTask={openTask}
          onRemoveTask={removeTask}
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
    </div>
  );
}
