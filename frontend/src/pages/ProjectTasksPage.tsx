import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { terminateTask as terminateBackendTask } from '../api/backendApi';
import { rcsbCifUrl } from '../utils/structureParser';
import { ProjectCopilotModal, readStoredCopilotOpen, writeStoredCopilotOpen } from '../components/copilot/ProjectCopilotModal';
import { SharingModal } from '../components/project/SharingModal';
import { ApiAccessPage } from './ApiAccessPage';
import { useAuth } from '../hooks/useAuth';
import { useCopilotAvailability } from '../hooks/useCopilotAvailability';
import { ProjectTasksHeader, type ExportProgressInfo } from './projectTasks/ProjectTasksHeader';
import { ProjectTasksWorkspace } from './projectTasks/ProjectTasksWorkspace';
import { exportTaskRowsToExcel } from './projectTasks/exportTaskRowsToExcel';
import { cancelTasksExcelExport } from '../api/backendExportApi';
import { countProjectTasks } from '../api/supabaseLite';
import type { TaskListRow } from './projectTasks/taskListTypes';
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
import { readCopilotText, readCopilotNumber, isOneOf } from '../utils/copilotPayload';

const TASK_STATE_FILTER_OPTIONS = ['all', 'DRAFT', 'QUEUED', 'RUNNING', 'SUCCESS', 'FAILURE', 'REVOKED'] as const;
const TASK_SORT_OPTIONS = ['submitted', 'plddt', 'ipsae', 'iptm', 'pae', 'backend', 'seed', 'mode'] as const;
const TASK_SORT_DIRECTION_OPTIONS = ['asc', 'desc'] as const;
const TASK_SUBMITTED_WITHIN_OPTIONS = ['all', '1', '7', '30', '90'] as const;
const TASK_SEED_FILTER_OPTIONS = ['all', 'with_seed', 'without_seed'] as const;
const TASK_PAGE_SIZE_OPTIONS = [8, 12, 20, 50] as const;
const TASK_METRIC_COLUMN_OPTIONS = ['plddt', 'ipsae', 'iptm', 'pae'] as const;
const WORKFLOW_FILTER_OPTIONS = ['all', 'prediction', 'virtual_screening', 'affinity', 'peptide_design', 'lead_optimization'] as const;

// A confirmed Copilot action can reference a task that is no longer in the loaded list (deleted,
// or the list reloaded since the plan was made). The message must tell BOTH audiences the way
// out: the user (refresh and retry, or ask Copilot to create a new task) and the planner, whose
// PLAN RECOVERY reads this same error text on the next turn.
const COPILOT_TASK_NOT_FOUND_ERROR =
  'Could not find the task Copilot referenced — it may have been deleted or is no longer in this list. ' +
  'Refresh the task list and pick again, or ask Copilot to create a new task.';

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
  const [exportProgress, setExportProgress] = useState<ExportProgressInfo | null>(null);
  // The route is NOT keyed by projectId, so a project switch reuses this
  // component instance. An in-flight export must die with its project: the
  // cancelled ref alone cannot do it (React reruns cleanup+setup back-to-back
  // on dep change, leaving no observable window), so compare the live project
  // id against the one captured at click time.
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;
  const exportCancelledRef = useRef(false);
  // Export id of the in-flight server job (for real cancellation).
  const activeExportIdRef = useRef<string | null>(null);
  useEffect(() => {
    exportCancelledRef.current = false;
    return () => {
      exportCancelledRef.current = true;
    };
  }, [projectId]);
  const [sharedTaskRow, setSharedTaskRow] = useState<ProjectTask | null>(null);
  const [copilotOpen, setCopilotOpen] = useState(() => readStoredCopilotOpen({ contextType: 'task_list', projectId, userId: session?.userId || null }));
  useEffect(() => {
    writeStoredCopilotOpen({ contextType: 'task_list', projectId, userId: session?.userId || null }, copilotOpen);
  }, [copilotOpen, projectId, session?.userId]);
  const [priorityTaskRowIds, setPriorityTaskRowIds] = useState<string[]>([]);
  // Mirrors the structure-search flag (owned by the filtering hook, which runs below) into the
  // loader: while a SMILES/SMARTS query is active, lightweight tail rows backfill components.
  const [structureSearchActive, setStructureSearchActive] = useState(false);
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
    allTasksLoaded,
    setTasks,
    setError,
    ensureAllTasksLoaded,
    removeTaskRow,
  } = useProjectTasksDataLoader({
    projectId,
    sessionUserId: session?.userId || null,
    workspaceView,
    priorityTaskRowIds,
    structureSearchActive
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

  // Derived from structureSearchQuery, but computed with the render-time
  // adjust pattern instead of an effect: the loader (which consumes the flag)
  // is upstream of the filtering hook that owns the query, so plain derivation
  // during render is not possible. Setting state here re-renders immediately
  // BEFORE the tree commits — no effect pass, no post-paint flash.
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [prevStructureSearchQuery, setPrevStructureSearchQuery] = useState(structureSearchQuery);
  if (structureSearchQuery !== prevStructureSearchQuery) {
    setPrevStructureSearchQuery(structureSearchQuery);
    setStructureSearchActive(structureSearchQuery.trim().length > 0);
  }

  // Mirrors the CURRENT filtered rows across renders so the export flow can
  // read the complete filtered set after awaiting the full list load — a
  // closure captured at click time only holds the rows loaded so far.
  const filteredRowsRef = useRef<TaskListRow[]>([]);
  filteredRowsRef.current = filteredRows;

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
    removeTaskRow,
    terminateBackendTask,
  });

  const downloadExcel = useCallback(async () => {
    if (!project || filteredRows.length === 0) return;
    const clickedProjectId = projectId;
    const exportAbandoned = () =>
      exportCancelledRef.current || projectIdRef.current !== clickedProjectId;
    setExportingExcel(true);
    activeExportIdRef.current = null;
    setError(null);
    try {
      // 1) Precise total in one cheap request — the paginated task list may
      //    still be loading in the background, so tasks.length alone is not
      //    trustworthy yet. Fails hard; no estimate substitute.
      const totalCount = await countProjectTasks(project.id, {
        taskRowIds: project.access_scope === 'task_share' ? project.accessible_task_ids || [] : undefined,
        accessScope: project.access_scope || 'owner',
        accessLevel: project.access_level || 'owner',
        editableTaskIds: project.editable_task_ids || []
      });
      if (exportAbandoned()) return;

      // 2) Wait until every row is loaded; the export must cover the FULL set
      //    under the current filters, not what happened to be visible at click
      //    time. Waiting never duplicates an in-flight load; if none is running
      //    only the missing tail rows are fetched (each exactly once).
      if (!allTasksLoaded) {
        setExportProgress({ phase: 'collecting', done: tasks.length, total: totalCount });
      }
      await ensureAllTasksLoaded((loaded) => {
        if (exportAbandoned()) return;
        setExportProgress({ phase: 'collecting', done: loaded, total: totalCount });
      });
      if (exportAbandoned()) return;

      // 3) Export the complete filtered set (ref = post-load value).
      const completeFilteredRows = filteredRowsRef.current;
      if (completeFilteredRows.length === 0) {
        setError('No tasks match the current filters.');
        return;
      }
      let completionWarning = '';
      await exportTaskRowsToExcel({
        project,
        filteredRows: completeFilteredRows,
        onProgress: (info) => {
          if (exportAbandoned()) return;
          setExportProgress(info);
        },
        onWarning: (warning) => {
          completionWarning = warning;
        },
        onSubmitted: (exportId) => {
          activeExportIdRef.current = exportId;
        },
        isCancelled: exportAbandoned
      });
      if (!exportAbandoned() && completionWarning) {
        setError(`Excel export completed with a warning: ${completionWarning}`);
      }
    } catch (err) {
      if (exportAbandoned()) return;
      setError(err instanceof Error ? `Failed to export Excel: ${err.message}` : 'Failed to export Excel.');
    } finally {
      activeExportIdRef.current = null;
      if (!exportAbandoned()) {
        setExportingExcel(false);
        setExportProgress(null);
      }
    }
  }, [project, filteredRows.length, tasks.length, allTasksLoaded, projectId, ensureAllTasksLoaded, setError]);

  /**
   * Real cancellation: stop the poll loop immediately AND revoke the server
   * job so the CPU worker stops building instead of burning the slot.
   */
  const cancelExcelExport = useCallback(() => {
    exportCancelledRef.current = true;
    setExportingExcel(false);
    setExportProgress(null);
    const exportId = activeExportIdRef.current;
    activeExportIdRef.current = null;
    if (exportId) {
      void cancelTasksExcelExport(exportId);
    }
  }, []);

  const projectWorkflowKey = useMemo(
    () => (project ? getWorkflowDefinition(project.task_type).key : null),
    [project]
  );
  const supportsApiAccess =
    projectWorkflowKey === 'prediction' || projectWorkflowKey === 'virtual_screening' || projectWorkflowKey === 'affinity';
  const apiAccessDisabledReason = useMemo(() => {
    if (projectWorkflowKey === 'lead_optimization') {
      return 'Lead Optimization does not support API Access.';
    }
    if (projectWorkflowKey === 'peptide_design') {
      return 'Peptide Design does not support API Access.';
    }
    return 'API Access is available for Prediction, Virtual Screening, and Docking.';
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
    if (action.id === 'tasks:create_virtual_screening') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const components = Array.isArray(action.payload?.components) ? action.payload.components : [];
      const compounds = Array.isArray(action.payload?.screeningCompounds) ? action.payload.screeningCompounds : [];
      const url = new URL(createTaskHref, window.location.origin);
      if (components.length > 0) {
        url.searchParams.set('copilot_components', JSON.stringify(components));
      }
      if (compounds.length > 0) {
        const screeningInput = compounds
          .map((compound: { smiles?: unknown; name?: unknown }, index: number) => {
            const smiles = String(compound?.smiles || '').trim();
            if (!smiles) return '';
            const name = String(compound?.name || '').trim() || `Compound ${index + 1}`;
            return `>${name}\n${smiles}`;
          })
          .filter(Boolean)
          .join('\n');
        if (screeningInput) url.searchParams.set('copilot_screening_input', screeningInput);
      }
      navigate(url.pathname + url.search);
      return;
    }
    if (action.id === 'tasks:create_docking') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      // Identifier-first (pi/MCP rule): the host builds the guaranteed-valid mmCIF URL from
      // the entry id itself; a raw URL is honored only as the explicit non-RCSB fallback.
      const targetPdbId = String(action.payload?.targetPdbId || '').trim();
      const rawUrl = String(action.payload?.targetStructureUrl || '').trim();
      const targetStructureUrl = targetPdbId ? rcsbCifUrl(targetPdbId) : rawUrl;
      if (!targetStructureUrl) {
        throw new Error('No docking target was provided — pass the chosen entry\'s targetPdbId (preferred) or a cifUrl returned by a lookup.');
      }
      const resolvedName = targetPdbId
        ? (String(action.payload?.targetStructureName || '').trim() || `${targetPdbId.toUpperCase()}.cif`)
        : String(action.payload?.targetStructureName || '').trim();
      const url = new URL(createTaskHref, window.location.origin);
      url.searchParams.set('copilot_docking_target_url', targetStructureUrl);
      if (resolvedName) url.searchParams.set('copilot_docking_target_name', resolvedName);
      const ligandSmiles = String(action.payload?.ligandSmiles || '').trim();
      if (ligandSmiles) url.searchParams.set('copilot_docking_ligand_smiles', ligandSmiles);
      const taskName = String(action.payload?.taskName || '').trim();
      if (taskName) url.searchParams.set('copilot_task_name', taskName);
      const taskSummary = String(action.payload?.taskSummary || '').trim();
      if (taskSummary) url.searchParams.set('copilot_task_summary', taskSummary);
      navigate(url.pathname + url.search);
      return;
    }
    if (action.id === 'tasks:copy') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!project) throw new Error('Project is not loaded.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error(COPILOT_TASK_NOT_FOUND_ERROR);
      const params = new URLSearchParams();
      params.set('tab', 'components');
      params.set('new_task', '1');
      params.set('source_task_row_id', task.id);
      if (currentPage > 1) {
        params.set('task_list_page', String(currentPage));
      }
      const targetPath = `/projects/${project.id}`;
      const targetSearch = `?${params.toString()}`;
      const targetUrl = `${targetPath}${targetSearch}`;
      // SPA navigation only — no window.location.assign fallback. A full page load on a Copilot
      // copy action is the exact reload the user sees; react-router carries the
      // new_task/source_task_row_id params and the workspace loader applies them on mount.
      // Parameter changes are separate atomic operations on the task-detail page
      // (task_detail:apply_parameter_patch) — every skill stays a single unit of work.
      navigate(targetUrl);
      return;
    }
    if (action.id === 'tasks:delete') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error(COPILOT_TASK_NOT_FOUND_ERROR);
      await removeTask(task);
      return;
    }
    if (action.id === 'tasks:rename') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error(COPILOT_TASK_NOT_FOUND_ERROR);
      const nextName = typeof action.payload?.taskName === 'string' ? action.payload.taskName : undefined;
      const nextSummary = typeof action.payload?.taskSummary === 'string' ? action.payload.taskSummary : undefined;
      await updateTaskMetadata(task, { name: nextName, summary: nextSummary });
      return;
    }
    if (action.id === 'tasks:cancel') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error(COPILOT_TASK_NOT_FOUND_ERROR);
      await terminateTask(task);
      return;
    }
    if (action.id === 'tasks:open') {
      const taskRowId = String(action.payload?.taskRowId || '').trim();
      const task = taskRows.find((row) => row.task.id === taskRowId)?.task;
      if (!task) throw new Error(COPILOT_TASK_NOT_FOUND_ERROR);
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
    // Until the project resolves, supportsApiAccess is false for every workflow — resetting now
    // would destroy a `?view=api` deep link while the fetch is still in flight.
    if (!project) return;
    if (supportsApiAccess) return;
    if (workspaceView !== 'api') return;
    setWorkspaceViewWithUrl('tasks');
  }, [project, supportsApiAccess, workspaceView, setWorkspaceViewWithUrl]);

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
          exportProgress={exportProgress}
          filteredCount={filteredRows.length}
          onDownloadExcel={() => {
            if (exportingExcel) {
              cancelExcelExport();
              return;
            }
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
            // The page block carries the USER-FACING workflow naming (title/shortTitle) —
            // without it the model only sees the internal task_type token and addresses the
            // workflow by its machine key in user-facing prose.
            page: {
              contextType: 'task_list',
              workflowKey: projectWorkflowKey || project.task_type,
              workflowTitle: project ? getWorkflowDefinition(project.task_type).title : '',
              workflowShortTitle: project ? getWorkflowDefinition(project.task_type).shortTitle : ''
            },
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
