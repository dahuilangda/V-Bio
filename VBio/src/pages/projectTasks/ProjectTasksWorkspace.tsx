import { useEffect } from 'react';
import type { ProjectTask } from '../../types/models';
import { ProjectTasksFilters } from './ProjectTasksFilters';
import { ProjectTasksTable } from './ProjectTasksTable';
import type {
  SeedFilterOption,
  SortKey,
  StructureSearchMode,
  SubmittedWithinDaysOption,
  TaskTableMode,
  TaskListRow,
  TaskWorkflowFilter
} from './taskListTypes';

interface ProjectTasksWorkspaceProps {
  taskSearch: string;
  onTaskSearchChange: (value: string) => void;
  stateFilter: 'all' | ProjectTask['task_state'];
  onStateFilterChange: (value: 'all' | ProjectTask['task_state']) => void;
  workflowFilter: TaskWorkflowFilter;
  onWorkflowFilterChange: (value: TaskWorkflowFilter) => void;
  workflowOptions: TaskWorkflowFilter[];
  backendFilter: 'all' | string;
  onBackendFilterChange: (value: string) => void;
  backendOptions: string[];
  filteredCount: number;
  showAdvancedFilters: boolean;
  onToggleAdvancedFilters: () => void;
  advancedFilterCount: number;
  submittedWithinDays: SubmittedWithinDaysOption;
  onSubmittedWithinDaysChange: (value: SubmittedWithinDaysOption) => void;
  seedFilter: SeedFilterOption;
  onSeedFilterChange: (value: SeedFilterOption) => void;
  minPlddt: string;
  onMinPlddtChange: (value: string) => void;
  minIptm: string;
  onMinIptmChange: (value: string) => void;
  maxPae: string;
  onMaxPaeChange: (value: string) => void;
  failureOnly: boolean;
  onFailureOnlyChange: (checked: boolean) => void;
  structureSearchMode: StructureSearchMode;
  onStructureSearchModeChange: (value: StructureSearchMode) => void;
  structureSearchQuery: string;
  onStructureSearchQueryChange: (value: string) => void;
  structureSearchLoading: boolean;
  structureSearchError: string | null;
  structureSearchMatches: Record<string, boolean>;
  onClearAdvancedFilters: () => void;
  sortKey: SortKey;
  sortMark: (key: SortKey) => string;
  onSort: (key: SortKey) => void;
  filteredRows: TaskListRow[];
  pagedRows: TaskListRow[];
  editingTaskNameId: string | null;
  editingTaskNameValue: string;
  savingTaskNameId: string | null;
  openingTaskId: string | null;
  deletingTaskId: string | null;
  terminatingTaskId: string | null;
  onOpenTask: (task: ProjectTask) => void;
  onTerminateTask: (task: ProjectTask) => void;
  onRemoveTask: (task: ProjectTask) => void;
  onBeginTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  onCancelTaskNameEdit: () => void;
  onSaveTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  onEditingTaskNameValueChange: (value: string) => void;
  currentPage: number;
  totalPages: number;
  pageSize: number;
  onPageSizeChange: (value: number) => void;
  onPageChange: (updater: number | ((prev: number) => number)) => void;
  onJumpToPage: (value: string) => void;
}

export function ProjectTasksWorkspace({
  taskSearch,
  onTaskSearchChange,
  stateFilter,
  onStateFilterChange,
  workflowFilter,
  onWorkflowFilterChange,
  workflowOptions,
  backendFilter,
  onBackendFilterChange,
  backendOptions,
  filteredCount,
  showAdvancedFilters,
  onToggleAdvancedFilters,
  advancedFilterCount,
  submittedWithinDays,
  onSubmittedWithinDaysChange,
  seedFilter,
  onSeedFilterChange,
  minPlddt,
  onMinPlddtChange,
  minIptm,
  onMinIptmChange,
  maxPae,
  onMaxPaeChange,
  failureOnly,
  onFailureOnlyChange,
  structureSearchMode,
  onStructureSearchModeChange,
  structureSearchQuery,
  onStructureSearchQueryChange,
  structureSearchLoading,
  structureSearchError,
  structureSearchMatches,
  onClearAdvancedFilters,
  sortKey,
  sortMark,
  onSort,
  filteredRows,
  pagedRows,
  editingTaskNameId,
  editingTaskNameValue,
  savingTaskNameId,
  openingTaskId,
  deletingTaskId,
  terminatingTaskId,
  onOpenTask,
  onTerminateTask,
  onRemoveTask,
  onBeginTaskNameEdit,
  onCancelTaskNameEdit,
  onSaveTaskNameEdit,
  onEditingTaskNameValueChange,
  currentPage,
  totalPages,
  pageSize,
  onPageSizeChange,
  onPageChange,
  onJumpToPage
}: ProjectTasksWorkspaceProps) {
  const hasSingleWorkflow = workflowOptions.length === 1 ? workflowOptions[0] : null;
  const tableMode: TaskTableMode = (() => {
    if (workflowFilter === 'lead_optimization' || (workflowFilter === 'all' && hasSingleWorkflow === 'lead_optimization')) {
      return 'lead_opt';
    }
    if (workflowFilter === 'peptide_design' || (workflowFilter === 'all' && hasSingleWorkflow === 'peptide_design')) {
      return 'peptide';
    }
    return 'default';
  })();
  const compactMetricsView = tableMode !== 'default';

  useEffect(() => {
    if (!compactMetricsView) return;
    if (sortKey === 'submitted') return;
    onSort('submitted');
  }, [compactMetricsView, onSort, sortKey]);

  return (
    <section className="panel">
      <ProjectTasksFilters
        taskSearch={taskSearch}
        onTaskSearchChange={onTaskSearchChange}
        stateFilter={stateFilter}
        onStateFilterChange={onStateFilterChange}
        workflowFilter={workflowFilter}
        onWorkflowFilterChange={onWorkflowFilterChange}
        workflowOptions={workflowOptions}
        backendFilter={backendFilter}
        onBackendFilterChange={onBackendFilterChange}
        backendOptions={backendOptions}
        tableMode={tableMode}
        compactMetricsView={compactMetricsView}
        filteredMatchedCount={filteredRows.length}
        showAdvancedFilters={showAdvancedFilters}
        onToggleAdvancedFilters={onToggleAdvancedFilters}
        advancedFilterCount={advancedFilterCount}
        submittedWithinDays={submittedWithinDays}
        onSubmittedWithinDaysChange={onSubmittedWithinDaysChange}
        seedFilter={seedFilter}
        onSeedFilterChange={onSeedFilterChange}
        minPlddt={minPlddt}
        onMinPlddtChange={onMinPlddtChange}
        minIptm={minIptm}
        onMinIptmChange={onMinIptmChange}
        maxPae={maxPae}
        onMaxPaeChange={onMaxPaeChange}
        failureOnly={failureOnly}
        onFailureOnlyChange={onFailureOnlyChange}
        structureSearchMode={structureSearchMode}
        onStructureSearchModeChange={onStructureSearchModeChange}
        structureSearchQuery={structureSearchQuery}
        onStructureSearchQueryChange={onStructureSearchQueryChange}
        structureSearchLoading={structureSearchLoading}
        structureSearchError={structureSearchError}
        structureSearchMatches={structureSearchMatches}
        onClearAdvancedFilters={onClearAdvancedFilters}
      />

      <ProjectTasksTable
        filteredCount={filteredCount}
        tableMode={tableMode}
        sortKey={sortKey}
        sortMark={sortMark}
        onSort={onSort}
        pagedRows={pagedRows}
        editingTaskNameId={editingTaskNameId}
        editingTaskNameValue={editingTaskNameValue}
        savingTaskNameId={savingTaskNameId}
        openingTaskId={openingTaskId}
        deletingTaskId={deletingTaskId}
        terminatingTaskId={terminatingTaskId}
        onOpenTask={onOpenTask}
        onTerminateTask={onTerminateTask}
        onRemoveTask={onRemoveTask}
        onBeginTaskNameEdit={onBeginTaskNameEdit}
        onCancelTaskNameEdit={onCancelTaskNameEdit}
        onSaveTaskNameEdit={onSaveTaskNameEdit}
        onEditingTaskNameValueChange={onEditingTaskNameValueChange}
        currentPage={currentPage}
        totalPages={totalPages}
        pageSize={pageSize}
        onPageSizeChange={onPageSizeChange}
        onPageChange={onPageChange}
        onJumpToPage={onJumpToPage}
      />
    </section>
  );
}
