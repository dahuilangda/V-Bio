import { useCallback, useEffect, useMemo, useRef } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import type { InputComponent, ProjectTask } from '../../types/models';
import { getTaskStatuses } from '../../api/backendApi';
import { hasLeadOptPredictionRuntime, readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';
import { hasObjectContent } from '../projectTasks/recordReaders';
import { buildTaskRuntimeFailureMessage } from '../../utils/taskRuntime';
import { useAuth } from '../../hooks/useAuth';
import {
  deleteProjectTask,
  findProjectTaskByTaskId,
  getProjectTaskById,
  insertProjectTask,
  listProjectTasksForList,
  updateProject,
  updateProjectTask
} from '../../api/supabaseLite';
import { canEditProject, isTaskEditableForProject } from '../../utils/accessControl';
import { saveProjectInputConfig } from '../../utils/projectInputs';
import { pocketOptionsWithRestoredTemplate } from '../../utils/peptidePocket';
import { validateComponents } from '../../utils/inputValidation';
import { getWorkflowDefinition, isPredictionLikeWorkflowKey } from '../../utils/workflows';
import { createWorkflowSubmitters } from './workflowSubmitters';
import { useEntryRoutingResolution } from './useEntryRoutingResolution';
import { useProjectTaskActions } from './useProjectTaskActions';
import { useProjectAffinityWorkspace } from './useProjectAffinityWorkspace';
import {
  addTemplatesToTaskSnapshotComponents,
  buildAffinityUploadSnapshotComponents,
  isDraftTaskSnapshot,
  mergeTaskSnapshotIntoConfig,
  hasStoredTaskInputOptions,
  readLeadOptUploadsFromComponents,
  readTaskInputOptions,
} from './projectTaskSnapshot';
import {
  createAffinityUploadsFingerprint,
  computeUseMsaFlag,
  createComputationFingerprint,
  createDraftFingerprint,
  createProteinTemplatesFingerprint,
  listIncompleteComponentOrders,
  nonEmptyComponents,
  normalizeConfigForBackend,
  sortProjectTasks
} from './projectDraftUtils';
import {
  inferTaskStateFromStatusPayload,
  readTaskRuntimeStatusText as readStatusText
} from '../../utils/taskRuntime';

import { useResultSnapshot } from './useResultSnapshot';
import { useProjectRunUiEffects } from './useProjectRunUiEffects';
import { useProjectRuntimeEffects } from './useProjectRuntimeEffects';
import { useProjectTaskStatusContext } from './useProjectTaskStatusContext';
import { useProjectWorkflowContext } from './useProjectWorkflowContext';
import { useConstraintTemplateContext } from './useConstraintTemplateContext';
import { useWorkspaceAffinitySelection } from './useWorkspaceAffinitySelection';
import { useProjectDraftSynchronizers } from './useProjectDraftSynchronizers';
import { useProjectWorkspaceRuntimeUi } from './useProjectWorkspaceRuntimeUi';
import { createAdaptivePollScheduler } from '../../utils/adaptivePollScheduler';
import { isTransientRuntimeStatusText } from '../../utils/taskRuntime';
import { useProjectWorkspaceLoader } from './useProjectWorkspaceLoader';
import {
  showRunQueuedNotice as showRunQueuedNoticeControl,
  submitTaskByWorkflow
} from './runControls';
import { useProjectDirtyState } from './useProjectDirtyState';
import { useProjectConfidenceSignals } from './useProjectConfidenceSignals';
import { useComponentTypeBuckets } from './useComponentTypeBuckets';
import { useProjectDetailLocalState } from './useProjectDetailLocalState';
import { hasPeptideCandidateRows } from '../projectTasks/taskDataPeptide';
import {
  buildRuntimePollingSignature,
  buildTaskRuntimeSignature,
  countLeadOptUploadPayloads,
  hasLeadOptResultSummaryPayload,
  hasTaskInputSnapshotPayload,
  isRuntimeTaskState,
  LEADOPT_CANDIDATE_HYDRATION_RETRY_MS,
  mergeProjectTaskDetailOptions,
  mergeTaskRuntimeFields,
  normalizeAffinityMode,
  normalizeCustomResidueDefinition,
  normalizeLeadOptRuntimeRow,
  normalizeProjectTaskDetailOptions,
  overlayRowsWithRuntimeStatus,
  projectTaskDetailOptionsCover,
  readLeadOptEnumeratedCandidateCount,
  readLeadOptQueryIdFromRow,
  RUNTIME_STATUS_LIGHT_POLL_MAX_TASKS,
  type CachedProjectTaskDetail,
  type NormalizedProjectTaskDetailOptions,
  type ProjectTaskDetailOptions
} from './projectRuntimeRowHelpers';


export function useProjectDetailRuntimeContext() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { session } = useAuth();
  const hasExplicitWorkspaceQuery = useMemo(() => {
    const query = new URLSearchParams(location.search);
    if (query.get('new_task') === '1') return true;
    return query.has('tab') || query.has('task_row_id');
  }, [location.search]);
  const requestNewTask = useMemo(() => {
    const query = new URLSearchParams(location.search);
    return query.get('new_task') === '1';
  }, [location.search]);
  const sourceTaskRowId = useMemo(() => {
    const query = new URLSearchParams(location.search);
    return String(query.get('source_task_row_id') || '').trim();
  }, [location.search]);

  const local = useProjectDetailLocalState();
  const leadOptTabHydrationRef = useRef<Record<string, string>>({});
  const peptideResultHydrationRef = useRef<Record<string, string>>({});
  const viewerResultHydrationRef = useRef<Record<string, string>>({});
  const {
    project,
    setProject,
    projectTasks,
    setProjectTasks,
    draft,
    setDraft,
    setLoading,
    saving,
    setSaving,
    submitting,
    setSubmitting,
    setError,
    setResultError,
    runRedirectTaskId,
    setRunRedirectTaskId,
    setRunSuccessNotice,
    setShowFloatingRunButton,
    setStructureText,
    setStructureFormat,
    structureTaskId,
    setStructureTaskId,
    statusInfo,
    setStatusInfo,
    workspaceTab,
    setWorkspaceTab,
    savedDraftFingerprint,
    setSavedDraftFingerprint,
    savedComputationFingerprint,
    setSavedComputationFingerprint,
    savedTemplateFingerprint,
    setSavedTemplateFingerprint,
    savedAffinityUploadsFingerprint,
    setSavedAffinityUploadsFingerprint,
    runMenuOpen,
    setRunMenuOpen,
    proteinTemplates,
    setProteinTemplates,
    customResidueLibrary,
    setCustomResidueLibrary,
    taskProteinTemplates,
    setTaskProteinTemplates,
    taskAffinityUploads,
    setTaskAffinityUploads,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    setPickedResidue,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    updateConstraintPickSlot,
    constraintPickSlot,
    constraintSelectionAnchorRef,
    statusRefreshInFlightRef,
    submitInFlightRef,
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    runActionRef,
    topRunButtonRef,
    activeComponentId,
    setActiveComponentId,
  } = local;
  const fallbackEditableTaskRowId = useMemo(() => {
    if (!project) return '';
    const requestedTaskRowId = String(new URLSearchParams(location.search).get('task_row_id') || '').trim();
    if (requestedTaskRowId && isTaskEditableForProject(project, requestedTaskRowId)) {
      return requestedTaskRowId;
    }
    if (sourceTaskRowId && isTaskEditableForProject(project, sourceTaskRowId)) {
      return sourceTaskRowId;
    }

    const activeTaskId = String(project.task_id || '').trim();
    if (activeTaskId) {
      const activeTaskRowId =
        projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId)?.id || '';
      if (activeTaskRowId && isTaskEditableForProject(project, activeTaskRowId)) {
        return activeTaskRowId;
      }
    }

    return projectTasks.find((item) => isTaskEditableForProject(project, item.id))?.id || '';
  }, [project, projectTasks, location.search, sourceTaskRowId]);
  const entryRoutingResolved = useEntryRoutingResolution({
    projectId,
    hasExplicitWorkspaceQuery,
    navigate,
  });
  useEffect(() => {
    const tab = new URLSearchParams(location.search).get('tab');
    if (tab === 'inputs') {
      setWorkspaceTab('basics');
      return;
    }
    if (tab === 'results' || tab === 'basics' || tab === 'components' || tab === 'constraints') {
      setWorkspaceTab(tab);
    }
  }, [location.search, projectId]);

  // Sync the URL tab param with the active workspace tab. draft is NOT in the deps —
  // the tab only needs to sync when workspaceTab changes, not on every draft edit.
  useEffect(() => {
    if (!projectId || !project) return;
    const query = new URLSearchParams(location.search);
    const currentTab = String(query.get('tab') || '').trim().toLowerCase();
    if (currentTab === workspaceTab) return;
    query.set('tab', workspaceTab);
    navigate(`/projects/${projectId}?${query.toString()}`, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceTab, projectId, project, navigate]);

  // projectTasks/location.search are NOT in deps — they caused a navigate cascade
  // after submit (submit → syncWorkspaceTaskRow → search change → this effect → navigate).
  useEffect(() => {
    if (!projectId || !project) return;
    if (!requestNewTask) return;
    if (!(isPredictionLikeWorkflowKey(getWorkflowDefinition(project.task_type).key) || getWorkflowDefinition(project.task_type).key === 'affinity')) {
      return;
    }
    if (!fallbackEditableTaskRowId) return;
    const query = new URLSearchParams(location.search);
    const currentSourceTaskRowId = String(query.get('source_task_row_id') || '').trim();
    if (currentSourceTaskRowId) return; // already set
    query.set('new_task', '1');
    query.set('source_task_row_id', fallbackEditableTaskRowId);
    query.set('tab', workspaceTab);
    navigate(`/projects/${projectId}?${query.toString()}`, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestNewTask, fallbackEditableTaskRowId, projectId, project, workspaceTab, navigate]);

  const canEdit = useMemo(() => {
    if (!project) return false;
    if (canEditProject(project)) return true;

    const requestedTaskRowId = String(new URLSearchParams(location.search).get('task_row_id') || '').trim();
    if (requestedTaskRowId) {
      return isTaskEditableForProject(project, requestedTaskRowId);
    }
    if (requestNewTask) {
      const effectiveSourceTaskRowId = sourceTaskRowId || fallbackEditableTaskRowId;
      if (effectiveSourceTaskRowId) {
        return isTaskEditableForProject(project, effectiveSourceTaskRowId);
      }
    }

    const activeTaskId = String(project.task_id || '').trim();
    if (!activeTaskId) return false;
    const activeTaskRowId =
      projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId)?.id || '';
    return isTaskEditableForProject(project, activeTaskRowId);
  }, [project, projectTasks, location.search, requestNewTask, sourceTaskRowId, fallbackEditableTaskRowId]);
  const workflowKey = useMemo(() => getWorkflowDefinition(project?.task_type).key, [project?.task_type]);
  const isPredictionWorkflow = isPredictionLikeWorkflowKey(workflowKey);
  const isPeptideDesignWorkflow = workflowKey === 'peptide_design';
  const isVirtualScreeningWorkflow = workflowKey === 'virtual_screening';
  const isAffinityWorkflow = workflowKey === 'affinity';
  const isLeadOptimizationWorkflow = workflowKey === 'lead_optimization';
  const runtimePollingSignature = useMemo(() => buildRuntimePollingSignature(projectTasks), [projectTasks]);
  const runtimePollingSummary = useMemo(() => {
    let hasRuntimeTasks = false;
    let hasRunning = false;
    let hasQueued = false;

    for (const row of projectTasks) {
      const runtimeTaskState = String(row.task_state || '').trim().toUpperCase();
      const hasRuntimeTaskState = isRuntimeTaskState(runtimeTaskState) && String(row.task_id || '').trim();
      if (hasRuntimeTaskState) {
        hasRuntimeTasks = true;
        if (runtimeTaskState === 'RUNNING') hasRunning = true;
        if (runtimeTaskState === 'QUEUED') hasQueued = true;
      }

      if (!hasLeadOptPredictionRuntime(row)) continue;
      hasRuntimeTasks = true;
      const summary = readLeadOptTaskSummary(row);
      const stage = String(summary?.stage || '').trim().toLowerCase();
      const queued = Math.max(0, summary?.predictionQueued || 0);
      const running = Math.max(0, summary?.predictionRunning || 0);
      if (running > 0 || stage === 'running' || stage === 'prediction_running') {
        hasRunning = true;
      } else if (queued > 0 || stage === 'queued' || stage === 'prediction_queued') {
        hasQueued = true;
      }
    }

    return {
      hasRuntimeTasks,
      hasRunning,
      hasQueued
    };
  }, [runtimePollingSignature, projectTasks]);
  const leadOptCandidateHydrationAtRef = useRef<Record<string, number>>({});
  const leadOptResultMaterializationRef = useRef<Record<string, string>>({});
  const runtimeTaskStatusCursorRef = useRef(0);
  const runtimeTerminalStatusByTaskIdRef = useRef<Record<string, { task_id: string; state: string; info?: Record<string, unknown> }>>({});
  const projectTaskDetailCacheRef = useRef<Record<string, CachedProjectTaskDetail>>({});
  const projectTaskDetailInFlightRef = useRef<
    Record<string, { options: NormalizedProjectTaskDetailOptions; promise: Promise<ProjectTask | null> }>
  >({});

  const getProjectTaskDetailCached = useCallback(
    async (taskRowId: string, options?: ProjectTaskDetailOptions): Promise<ProjectTask | null> => {
      const normalizedTaskRowId = String(taskRowId || '').trim();
      if (!normalizedTaskRowId) return null;

      const requestedOptions = normalizeProjectTaskDetailOptions(options);
      const currentRow =
        projectTasks.find((row) => String(row.id || '').trim() === normalizedTaskRowId) || null;
      const currentUpdatedAt = String(currentRow?.updated_at || '').trim();
      const cached = projectTaskDetailCacheRef.current[normalizedTaskRowId];
      const shouldRefetchLeadOptCandidates =
        requestedOptions.includeLeadOptCandidates &&
        (hasLeadOptResultSummaryPayload(currentRow) || hasLeadOptResultSummaryPayload(cached?.task || null)) &&
        Math.max(
          readLeadOptEnumeratedCandidateCount(currentRow),
          readLeadOptEnumeratedCandidateCount(cached?.task || null)
        ) === 0;
      if (
        cached &&
        cached.updatedAt === currentUpdatedAt &&
        projectTaskDetailOptionsCover(cached.options, requestedOptions) &&
        !shouldRefetchLeadOptCandidates
      ) {
        return currentRow ? mergeTaskRuntimeFields(cached.task, currentRow) : cached.task;
      }

      const inFlight = projectTaskDetailInFlightRef.current[normalizedTaskRowId];
      if (inFlight && projectTaskDetailOptionsCover(inFlight.options, requestedOptions) && !shouldRefetchLeadOptCandidates) {
        return await inFlight.promise;
      }

      const mergedOptions = mergeProjectTaskDetailOptions(inFlight?.options, requestedOptions);
      const promise = getProjectTaskById(normalizedTaskRowId, mergedOptions)
        .then((detailRow) => {
          if (!detailRow) return null;
          const latestRow =
            projectTasks.find((row) => String(row.id || '').trim() === normalizedTaskRowId) || null;
          const mergedRow = latestRow ? mergeTaskRuntimeFields(detailRow, latestRow) : detailRow;
          projectTaskDetailCacheRef.current[normalizedTaskRowId] = {
            updatedAt: String(mergedRow.updated_at || '').trim(),
            options: mergedOptions,
            task: mergedRow,
          };
          return mergedRow;
        })
        .finally(() => {
          const currentInFlight = projectTaskDetailInFlightRef.current[normalizedTaskRowId];
          if (currentInFlight?.promise === promise) {
            delete projectTaskDetailInFlightRef.current[normalizedTaskRowId];
          }
        });

      projectTaskDetailInFlightRef.current[normalizedTaskRowId] = {
        options: mergedOptions,
        promise,
      };
      return await promise;
    },
    [projectTasks]
  );

  useEffect(() => {
    const projectIdValue = String(project?.id || '').trim();
    if (!projectIdValue) return;
    const shouldHydrateLeadOptSnapshot = workflowKey === 'lead_optimization';
    if (!runtimePollingSummary.hasRuntimeTasks && !shouldHydrateLeadOptSnapshot) return;
    const query = new URLSearchParams(location.search);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const effectiveContextTaskRowId = requestedTaskRowId || String(query.get('source_task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const resolveFocusedRow = (
      rows: Array<{ id?: string | null; task_id?: string | null; properties?: unknown; confidence?: unknown }>
    ) => {
      const requestedRow = effectiveContextTaskRowId
        ? rows.find((row) => String(row.id || '').trim() === effectiveContextTaskRowId) || null
        : null;
      const activeRow = activeTaskId
        ? rows.find((row) => String(row.task_id || '').trim() === activeTaskId) || null
        : null;
      return requestedRow || activeRow || null;
    };
    const resolveFocusedQueryId = (
      rows: Array<{ id?: string | null; task_id?: string | null; properties?: unknown; confidence?: unknown }>
    ): string => {
      return readLeadOptQueryIdFromRow(resolveFocusedRow(rows));
    };
    const focusedQueryId = resolveFocusedQueryId(projectTasks);
    const hasFocusedQueryCandidates = focusedQueryId
      ? projectTasks.some((row) => readLeadOptQueryIdFromRow(row) === focusedQueryId && readLeadOptEnumeratedCandidateCount(row) > 0)
      : projectTasks.some((row) => readLeadOptEnumeratedCandidateCount(row) > 0);
    const shouldRequestLeadOptCandidates =
      workflowKey === 'lead_optimization' &&
      workspaceTab === 'results' &&
      (!runtimePollingSummary.hasRuntimeTasks || !hasFocusedQueryCandidates);

    let cancelled = false;

    // adaptive cadence (see adaptivePollScheduler)
    let lastFetchedRowsSignature = '';
    let pauseChainOnce = false;

    const refreshTaskRows = async (): Promise<{ changed: boolean; shouldContinue: boolean }> => {
      if (cancelled) return { changed: false, shouldContinue: false };
      let hasLeadOptSummaryRows = false;
      let hasLeadOptCandidates = false;
      let hadError = false;
      let rowsForUiSnapshot: ProjectTask[] = [];
      try {
        const rowsRaw = await listProjectTasksForList(projectIdValue, {
          includeComponents: false,
          includeConfidence: false,
          includeConfidenceSummary: workflowKey !== 'lead_optimization',
          includeProperties: false,
          includePropertiesSummary: workflowKey !== 'lead_optimization',
          includeLeadOptSummary: workflowKey === 'lead_optimization',
          includeLeadOptCandidates: false,
          taskRowIds:
            String(project?.access_scope || 'owner').trim() === 'task_share'
              ? project?.accessible_task_ids
              : undefined,
          accessScope: project?.access_scope || 'owner',
          accessLevel: project?.access_level || 'owner',
          editableTaskIds: project?.editable_task_ids || []
        });
        if (cancelled) return { changed: false, shouldContinue: false };
        const nextRows = sortProjectTasks(rowsRaw).map((row) => normalizeLeadOptRuntimeRow(row));
        const terminalStatusByTaskId = runtimeTerminalStatusByTaskIdRef.current;
        const cachedStatusByTaskId: Record<string, { task_id: string; state: string; info?: Record<string, unknown> }> = {};
        for (const row of nextRows) {
          const taskId = String(row.task_id || '').trim();
          if (!taskId) continue;
          const cached = terminalStatusByTaskId[taskId];
          if (!cached) continue;
          cachedStatusByTaskId[taskId] = cached;
        }
        let runtimeEnhancedRows =
          Object.keys(cachedStatusByTaskId).length > 0
            ? overlayRowsWithRuntimeStatus(nextRows, cachedStatusByTaskId).map((row) => normalizeLeadOptRuntimeRow(row))
            : nextRows;
        const runtimeRowByTaskId = new Map(
          runtimeEnhancedRows
            .map((row) => [String(row.task_id || '').trim(), row] as const)
            .filter(([taskId]) => Boolean(taskId))
        );

        const runtimeTaskIds = Array.from(
          new Set(
            runtimeEnhancedRows
              .map((row) => ({
                taskId: String(row.task_id || '').trim(),
                taskState: String(row.task_state || '').trim().toUpperCase()
              }))
              .filter(
                (row) =>
                  row.taskId &&
                  (row.taskState === 'QUEUED' || row.taskState === 'RUNNING') &&
                  !terminalStatusByTaskId[row.taskId]
              )
              .map((row) => row.taskId)
          )
        );

        if (runtimeTaskIds.length > 0) {
          try {
            const pollSize = Math.min(RUNTIME_STATUS_LIGHT_POLL_MAX_TASKS, runtimeTaskIds.length);
            const startCursor = ((runtimeTaskStatusCursorRef.current % runtimeTaskIds.length) + runtimeTaskIds.length) % runtimeTaskIds.length;
            const taskIdsForPoll: string[] = [];
            for (let i = 0; i < pollSize; i += 1) {
              taskIdsForPoll.push(runtimeTaskIds[(startCursor + i) % runtimeTaskIds.length]);
            }
            runtimeTaskStatusCursorRef.current = (startCursor + pollSize) % runtimeTaskIds.length;
            const statusByTaskId: Record<string, { task_id: string; state: string; info?: Record<string, unknown> }> = {};
            for (let i = 0; i < taskIdsForPoll.length; i += 64) {
              const chunk = taskIdsForPoll.slice(i, i + 64);
              try {
                Object.assign(statusByTaskId, await getTaskStatuses(chunk));
              } catch (err) {
                console.error('Task chunk update failed; keeping partial successes.', err);
              }
            }
            for (const [taskId, status] of Object.entries(statusByTaskId)) {
              const inferred = inferTaskStateFromStatusPayload(status);
              if (inferred === 'SUCCESS' || inferred === 'FAILURE' || inferred === 'REVOKED') {
                runtimeTerminalStatusByTaskIdRef.current[taskId] = status;
                const runtimeRow = runtimeRowByTaskId.get(taskId);
                const runtimeRowId = String(runtimeRow?.id || '').trim();
                const runtimeRowState = String(runtimeRow?.task_state || '').trim().toUpperCase();
                const shouldPersistRuntimeTerminal =
                  runtimeRowId &&
                  (runtimeRowState === 'QUEUED' || runtimeRowState === 'RUNNING');
                if (shouldPersistRuntimeTerminal) {
                  const runtimeStatusText = String(readStatusText(status) || '').trim();
                  const runtimeFailureText =
                    inferred === 'FAILURE' ? buildTaskRuntimeFailureMessage(status, runtimeStatusText || 'Task failed.').trim() : '';
                  try {
                    await updateProjectTask(
                      runtimeRowId,
                      {
                        task_state: inferred,
                        status_text:
                          inferred === 'FAILURE'
                            ? runtimeFailureText || runtimeStatusText || 'Task failed.'
                            : runtimeStatusText || (inferred === 'SUCCESS' ? 'Task completed.' : 'Task unavailable or expired.'),
                        error_text:
                          inferred === 'FAILURE'
                            ? runtimeFailureText || runtimeStatusText || 'Task failed.'
                            : ''
                      },
                      { minimalReturn: true }
                    );
                  } catch (err) {
                    console.error('Runtime overlay persistence failed; keeping in-memory overlay.', err);
                  }
                }
              }
              statusByTaskId[taskId] = status;
            }
            if (!cancelled && Object.keys(statusByTaskId).length > 0) {
              runtimeEnhancedRows = overlayRowsWithRuntimeStatus(nextRows, {
                ...cachedStatusByTaskId,
                ...statusByTaskId
              }).map((row) => normalizeLeadOptRuntimeRow(row));
            }
          } catch (err) {
            console.error('Runtime overlay apply failed; keeping DB snapshot.', err);
          }
        }

        let rowsForUi = runtimeEnhancedRows;
        if (workflowKey === 'lead_optimization' && workspaceTab === 'results') {
          hasLeadOptSummaryRows = rowsForUi.some((row) => hasLeadOptResultSummaryPayload(row));
          const focusedQueryIdInRows = resolveFocusedQueryId(rowsForUi);
          const focusedRow = resolveFocusedRow(rowsForUi);
          const focusedRowId = String(focusedRow?.id || '').trim();
          hasLeadOptCandidates = focusedQueryIdInRows
            ? rowsForUi.some(
                (row) => readLeadOptQueryIdFromRow(row) === focusedQueryIdInRows && readLeadOptEnumeratedCandidateCount(row) > 0
              )
            : rowsForUi.some((row) => readLeadOptEnumeratedCandidateCount(row) > 0);
          if (
            shouldRequestLeadOptCandidates &&
            hasLeadOptSummaryRows &&
            !hasLeadOptCandidates &&
            focusedRowId
          ) {
            const lastAt = Number(leadOptCandidateHydrationAtRef.current[focusedRowId] || 0);
            const nowTs = Date.now();
            if (!Number.isFinite(lastAt) || nowTs - lastAt >= LEADOPT_CANDIDATE_HYDRATION_RETRY_MS) {
              leadOptCandidateHydrationAtRef.current[focusedRowId] = nowTs;
              try {
                const detailRow = await getProjectTaskDetailCached(focusedRowId, {
                  includeComponents: false,
                  includeConstraints: false,
                  includeProperties: false,
                  includeLeadOptSummary: true,
                  includeLeadOptCandidates: true,
                  includeConfidence: false,
                  includeAffinity: false,
                  includeProteinSequence: false
                });
                if (!cancelled && detailRow) {
                  const normalizedDetailRow = normalizeLeadOptRuntimeRow(detailRow);
                  rowsForUi = rowsForUi.map((row) =>
                    String(row.id || '').trim() === focusedRowId ? mergeTaskRuntimeFields(normalizedDetailRow, row) : row
                  );
                  hasLeadOptCandidates = focusedQueryIdInRows
                    ? rowsForUi.some(
                        (row) =>
                          readLeadOptQueryIdFromRow(row) === focusedQueryIdInRows && readLeadOptEnumeratedCandidateCount(row) > 0
                      )
                    : rowsForUi.some((row) => readLeadOptEnumeratedCandidateCount(row) > 0);
                }
              } catch (err) {
                console.error('Lightweight summary refresh failed; keeping current rows.', err);
              }
            }
          }
        }

        // a cancelled poll must not land its stale-closure writes
        if (cancelled) return { changed: false, shouldContinue: false };

        setProjectTasks((prev) => {
          const prevById = new Map(prev.map((item) => [item.id, item]));
          const mergedRows = rowsForUi.map((row) => {
            const prevRow = prevById.get(row.id);
            if (!prevRow) return row;
            return mergeTaskRuntimeFields(row, prevRow);
          });
          return buildTaskRuntimeSignature(prev) === buildTaskRuntimeSignature(mergedRows) ? prev : mergedRows;
        });

        const activeProjectTaskId = String(project?.task_id || '').trim();
        if (!activeProjectTaskId) return { changed: false, shouldContinue: true };
        const activeRow = rowsForUi.find((row) => String(row.task_id || '').trim() === activeProjectTaskId) || null;
        if (!activeRow) return { changed: false, shouldContinue: true };
        setProject((prev) => {
          if (!prev) return prev;
          const mergedActiveRow = mergeTaskRuntimeFields(activeRow, prev);
          const rawTaskState = String(mergedActiveRow.task_state || '').trim().toUpperCase();
          const nextTaskState = (
            rawTaskState === 'QUEUED' ||
            rawTaskState === 'RUNNING' ||
            rawTaskState === 'SUCCESS' ||
            rawTaskState === 'FAILURE' ||
            rawTaskState === 'REVOKED' ||
            rawTaskState === 'DRAFT'
              ? rawTaskState
              : prev.task_state
          ) as typeof prev.task_state;
          const nextStatusText = String(mergedActiveRow.status_text || '').trim();
          const nextErrorText = String(mergedActiveRow.error_text || '').trim();
          const nextCompletedAt = mergedActiveRow.completed_at || null;
          const nextDurationCandidate = Number(mergedActiveRow.duration_seconds);
          const nextDurationSeconds = Number.isFinite(nextDurationCandidate) ? nextDurationCandidate : null;
          if (
            prev.task_state === nextTaskState &&
            String(prev.status_text || '') === nextStatusText &&
            String(prev.error_text || '') === nextErrorText &&
            (prev.completed_at || null) === nextCompletedAt &&
            (prev.duration_seconds ?? null) === nextDurationSeconds
          ) {
            return prev;
          }
          return {
            ...prev,
            task_state: nextTaskState,
            status_text: nextStatusText,
            error_text: nextErrorText,
            completed_at: nextCompletedAt,
            duration_seconds: nextDurationSeconds
          };
        });
        rowsForUiSnapshot = rowsForUi;
      } catch (err) {
        console.error('refreshTaskRows polling failed; keeping local state.', err);
        hadError = true;
      }
      // "changed" compares fetches, not React state (the effect closure is stale mid-run)
      const fetchedSignature = buildTaskRuntimeSignature(rowsForUiSnapshot);
      const changed = !hadError && fetchedSignature !== lastFetchedRowsSignature;
      lastFetchedRowsSignature = fetchedSignature;
      const shouldContinue =
        !cancelled &&
        !(
          workflowKey === 'lead_optimization' &&
          !runtimePollingSummary.hasRuntimeTasks &&
          (workspaceTab !== 'results' || (hasLeadOptSummaryRows && hasLeadOptCandidates))
        );
      return { changed, shouldContinue };
    };

    const scheduler = createAdaptivePollScheduler({
      tickOnStart: true,
      resolveIntervals: () => {
        if (pauseChainOnce) {
          pauseChainOnce = false;
          return null;
        }
        if (workflowKey === 'lead_optimization' && !runtimePollingSummary.hasRuntimeTasks) {
          return { activeMs: 20000, idleMs: 20000 };
        }
        const isLeadOptOrPeptide = workflowKey === 'lead_optimization' || workflowKey === 'peptide_design';
        let activeMs: number;
        if (runtimePollingSummary.hasRunning) {
          activeMs = isLeadOptOrPeptide ? 4500 : 4200;
        } else if (runtimePollingSummary.hasQueued) {
          activeMs = isLeadOptOrPeptide ? 7500 : 7000;
        } else {
          activeMs = 12000;
        }
        return { activeMs, idleMs: Math.min(Math.max(activeMs * 2, 12000), 30000) };
      },
      idleAfterUnchangedTicks: 3,
      hiddenIntervalMultiplier: 2,
      maxIntervalMs: 40000,
      tick: async () => {
        const outcome = await refreshTaskRows();
        if (!outcome.shouldContinue) pauseChainOnce = true;
        return outcome.changed;
      }
    });
    scheduler.start();

    return () => {
      cancelled = true;
      scheduler.stop();
    };
  }, [
    project?.access_level,
    project?.access_scope,
    project?.accessible_task_ids,
    project?.editable_task_ids,
    location.search,
    project?.id,
    project?.task_id,
    runtimePollingSignature,
    runtimePollingSummary.hasQueued,
    runtimePollingSummary.hasRunning,
    runtimePollingSummary.hasRuntimeTasks,
    setProject,
    setProjectTasks,
    workspaceTab,
    workflowKey
  ]);

  // Peptide design: rehydrate the task snapshot on task switch; per-task runtime
  // options live under properties.__vbio_input_options_v1. List rows are
  // lightweight, so fetch the detail through the cache on a snapshot miss.
  const peptideTaskSwitchRef = useRef<string>('');
  useEffect(() => {
    if (!isPeptideDesignWorkflow) return;

    const query = new URLSearchParams(location.search);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const focusedRow =
      (requestedTaskRowId
        ? projectTasks.find((row) => String(row.id || '').trim() === requestedTaskRowId)
        : undefined) ||
      (activeTaskId ? projectTasks.find((row) => String(row.task_id || '').trim() === activeTaskId) : undefined) ||
      null;
    const focusedRowId = String(focusedRow?.id || '').trim();
    if (!focusedRowId) return;

    let cancelled = false;
    const applyTaskSnapshot = (taskRow: ProjectTask) => {
      if (!hasStoredTaskInputOptions(taskRow)) return;
      const taskOptions = readTaskInputOptions(taskRow);
      if (Object.keys(taskOptions).length === 0) return;
      // apply on a real task/options change, not on updated_at (polls bump it
      // and would rebuild the inputConfig mid-edit)
      const marker = `${focusedRowId}|${JSON.stringify(taskOptions)}`;
      if (peptideTaskSwitchRef.current === marker) return;
      peptideTaskSwitchRef.current = marker;

      const taskPoolEntries = Array.isArray(taskOptions.peptideResiduePool) ? taskOptions.peptideResiduePool : [];
      const taskCustomDefinitions = taskPoolEntries
        .filter((item) => item && item.kind === 'custom')
        .map((item) => normalizeCustomResidueDefinition(item))
        .filter(Boolean);
      if (taskCustomDefinitions.length > 0) {
        setCustomResidueLibrary((prev) => {
          const byCode = new Map<string, typeof taskCustomDefinitions[number]>();
          prev.forEach((item) => {
            const normalized = normalizeCustomResidueDefinition(item);
            if (normalized) byCode.set(normalized.ccd, normalized);
          });
          taskCustomDefinitions.forEach((item) => {
            if (item) byCode.set(item.ccd, item);
          });
          return Array.from(byCode.values()).filter(Boolean) as NonNullable<typeof taskCustomDefinitions[number]>[];
        });
      }

      // pocket picks reference the uploaded target structure; without its
      // content they are dropped (plain sequence picks stay)
      const hasTargetTemplate = Object.values(proteinTemplates).some(
        (template) => String(template?.content || '').trim().length > 0
      );
      setDraft((prev) => {
        if (!prev) return prev;
        const mergedConfig = normalizeConfigForBackend(
          mergeTaskSnapshotIntoConfig(prev.inputConfig, taskRow),
          prev.backend
        );
        return {
          ...prev,
          taskName: String(taskRow.name || prev.taskName || '').trim(),
          taskSummary: String(taskRow.summary || prev.taskSummary || '').trim(),
          inputConfig: {
            ...mergedConfig,
            options: pocketOptionsWithRestoredTemplate(
              mergedConfig.options,
              hasTargetTemplate
            )
          }
        };
      });
    };

    if (hasStoredTaskInputOptions(focusedRow)) {
      applyTaskSnapshot(focusedRow as ProjectTask);
      return;
    }

    void (async () => {
      try {
        const detailRow = await getProjectTaskDetailCached(focusedRowId, {
          includeComponents: true,
          includeConstraints: true,
          includeProperties: true,
          includeLeadOptSummary: false,
          includeLeadOptCandidates: false,
          includeConfidence: true,
          includeAffinity: false,
          includeProteinSequence: true
        });
        if (cancelled || !detailRow) return;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === focusedRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
        applyTaskSnapshot(detailRow);
      } catch (err) {
        console.error('Task detail hydration failed; keeping current editor state.', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    getProjectTaskDetailCached,
    isPeptideDesignWorkflow,
    location.search,
    proteinTemplates,
    project?.task_id,
    projectTasks,
    setDraft,
    setProjectTasks,
    setCustomResidueLibrary,
    normalizeConfigForBackend
  ]);

  // Re-align the draft to the selected task's snapshot in place (no workspace
  // refetch) for the component-editor workflows; peptide/lead-opt have their
  // own effects above. The marker ref dedupes against projectTasks refreshes.
  const componentTaskSwitchRef = useRef<string>('');
  useEffect(() => {
    if (!isPredictionWorkflow && !isAffinityWorkflow && !isVirtualScreeningWorkflow) return;

    const query = new URLSearchParams(location.search);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const focusedRow =
      (requestedTaskRowId
        ? projectTasks.find((row) => String(row.id || '').trim() === requestedTaskRowId)
        : undefined) ||
      (activeTaskId ? projectTasks.find((row) => String(row.task_id || '').trim() === activeTaskId) : undefined) ||
      null;
    const focusedRowId = String(focusedRow?.id || '').trim();
    if (!focusedRowId) return;

    let cancelled = false;
    const applyTaskSnapshot = (taskRow: ProjectTask) => {
      const marker = `${focusedRowId}|${String(taskRow.updated_at || '').trim()}`;
      if (componentTaskSwitchRef.current === marker) return;
      componentTaskSwitchRef.current = marker;
      setDraft((prev) => {
        if (!prev) return prev;
        const mergedConfig = normalizeConfigForBackend(
          mergeTaskSnapshotIntoConfig(prev.inputConfig, taskRow),
          prev.backend
        );
        return {
          ...prev,
          taskName: String(taskRow.name || prev.taskName || '').trim(),
          taskSummary: String(taskRow.summary || prev.taskSummary || '').trim(),
          inputConfig: mergedConfig
        };
      });
    };

    // If the focused row already carries full component data (from the list fetch), merge now.
    if (focusedRow && Array.isArray((focusedRow as ProjectTask).components) && (focusedRow as ProjectTask).components.length >= 0 && hasStoredTaskInputOptions(focusedRow as ProjectTask)) {
      applyTaskSnapshot(focusedRow as ProjectTask);
      return;
    }

    void (async () => {
      try {
        const detailRow = await getProjectTaskDetailCached(focusedRowId, {
          includeComponents: true,
          includeConstraints: true,
          includeProperties: true,
          includeLeadOptSummary: false,
          includeLeadOptCandidates: false,
          includeConfidence: true,
          includeAffinity: isAffinityWorkflow,
          includeProteinSequence: true
        });
        if (cancelled || !detailRow) return;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === focusedRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
        applyTaskSnapshot(detailRow);
      } catch (err) {
        console.error('Task detail hydration failed; keeping current editor state.', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    getProjectTaskDetailCached,
    isAffinityWorkflow,
    isPredictionWorkflow,
    isVirtualScreeningWorkflow,
    location.search,
    project?.task_id,
    projectTasks,
    setDraft,
    setProjectTasks,
    normalizeConfigForBackend
  ]);

  useEffect(() => {
    if (workflowKey !== 'lead_optimization') return;
    if (workspaceTab !== 'constraints' && workspaceTab !== 'components') return;
    if (!draft) return;

    const needsConstraints = workspaceTab === 'constraints' && (draft.inputConfig.constraints?.length || 0) === 0;
    const needsLeadOptUploads =
      workspaceTab === 'components' && countLeadOptUploadPayloads({ components: draft.inputConfig.components }) === 0;
    if (!needsConstraints && !needsLeadOptUploads) return;

    const query = new URLSearchParams(location.search);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const effectiveContextTaskRowId = requestedTaskRowId || String(query.get('source_task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const sourceRow =
      (effectiveContextTaskRowId
        ? projectTasks.find((row) => String(row.id || '').trim() === effectiveContextTaskRowId)
        : undefined) ||
      (activeTaskId ? projectTasks.find((row) => String(row.task_id || '').trim() === activeTaskId) : undefined) ||
      null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    if (!sourceRowId) return;
    const marker = `${workspaceTab}|${sourceRowId}|${String(sourceRow?.updated_at || '').trim()}`;
    if (leadOptTabHydrationRef.current[sourceRowId] === marker) return;

    let cancelled = false;
    void (async () => {
      try {
        const detailRow = await getProjectTaskDetailCached(sourceRowId, {
          includeComponents: true,
          includeConstraints: true,
          includeProperties: true,
          includeLeadOptSummary: true,
          includeLeadOptCandidates: false,
          includeConfidence: false,
          includeAffinity: false,
          includeProteinSequence: true
        });
        if (cancelled || !detailRow) return;
        leadOptTabHydrationRef.current[sourceRowId] = marker;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === sourceRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
        setDraft((prev) => {
          if (!prev) return prev;
          const mergedConfig = normalizeConfigForBackend(
            mergeTaskSnapshotIntoConfig(prev.inputConfig, detailRow),
            prev.backend
          );
          return {
            ...prev,
            taskName: String(detailRow.name || prev.taskName || '').trim(),
            taskSummary: String(detailRow.summary || prev.taskSummary || '').trim(),
            inputConfig: mergedConfig
          };
        });
      } catch (err) {
        console.error('On-demand task hydration failed; keeping existing editor state.', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    draft,
    location.search,
    project?.task_id,
    projectTasks,
    setDraft,
    setProjectTasks,
    workflowKey,
    workspaceTab
  ]);

  const {
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    displaySubmittedAt,
    progressPercent,
    isActiveRuntime,
    totalRuntimeSeconds
  } = useProjectTaskStatusContext({
    project,
    projectTasks,
    locationSearch: location.search,
    statusInfo
  });

  useEffect(() => {
    if (workflowKey !== 'lead_optimization') return;
    if (workspaceTab === 'components' || workspaceTab === 'constraints') return;

    const query = new URLSearchParams(location.search);
    const explicitTaskRowId =
      String(query.get('task_row_id') || '').trim() || String(query.get('source_task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const focusedRow =
      (explicitTaskRowId
        ? projectTasks.find((row) => String(row.id || '').trim() === explicitTaskRowId)
        : undefined) ||
      (activeTaskId ? projectTasks.find((row) => String(row.task_id || '').trim() === activeTaskId) : undefined) ||
      null;
    const focusedRowId = String(focusedRow?.id || '').trim();
    if (!focusedRowId || focusedRowId.startsWith('local-')) return;

    const needsDetailWarmup =
      !Array.isArray(focusedRow?.components) ||
      focusedRow.components.length === 0 ||
      !Array.isArray(focusedRow?.constraints) ||
      !hasObjectContent(focusedRow?.properties) ||
      !String(focusedRow?.protein_sequence || '').trim();
    if (!needsDetailWarmup) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const detailRow = await getProjectTaskDetailCached(focusedRowId, {
            includeComponents: true,
            includeConstraints: true,
            includeProperties: true,
            includeLeadOptSummary: true,
            includeLeadOptCandidates: false,
            includeConfidence: false,
            includeAffinity: false,
            includeProteinSequence: true,
          });
          if (cancelled || !detailRow) return;
          setProjectTasks((prev) =>
            prev.map((row) =>
              String(row.id || '').trim() === focusedRowId ? mergeTaskRuntimeFields(detailRow, row) : row
            )
          );
        } catch (err) {
          console.error('Warmup failed; keeping current lightweight row.', err);
        }
      })();
    }, 240);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [getProjectTaskDetailCached, location.search, project?.task_id, projectTasks, setProjectTasks, workflowKey, workspaceTab]);

  useEffect(() => {
    if (workflowKey !== 'lead_optimization') return;
    if (workspaceTab !== 'results') return;

    const query = new URLSearchParams(location.search);
    const explicitTaskRowId =
      String(query.get('task_row_id') || '').trim() || String(query.get('source_task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const focusedRow =
      (explicitTaskRowId
        ? projectTasks.find((row) => String(row.id || '').trim() === explicitTaskRowId)
        : undefined) ||
      (activeTaskId ? projectTasks.find((row) => String(row.task_id || '').trim() === activeTaskId) : undefined) ||
      null;
    const focusedRowId = String(focusedRow?.id || '').trim();
    if (!focusedRowId || focusedRowId.startsWith('local-')) return;

    const enumeratedCount = readLeadOptEnumeratedCandidateCount(focusedRow);
    if (!hasLeadOptResultSummaryPayload(focusedRow) || enumeratedCount > 0) return;

    const lastAt = Number(leadOptCandidateHydrationAtRef.current[focusedRowId] || 0);
    const nowTs = Date.now();
    if (Number.isFinite(lastAt) && nowTs - lastAt < 3000) return;
    leadOptCandidateHydrationAtRef.current[focusedRowId] = nowTs;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const detailRow = await getProjectTaskDetailCached(focusedRowId, {
            includeComponents: false,
            includeConstraints: false,
            includeProperties: false,
            includeLeadOptSummary: true,
            includeLeadOptCandidates: true,
            includeConfidence: false,
            includeAffinity: false,
            includeProteinSequence: false
          });
          if (cancelled || !detailRow) return;
          if (readLeadOptEnumeratedCandidateCount(detailRow) <= 0) return;
          setProjectTasks((prev) =>
            prev.map((row) =>
              String(row.id || '').trim() === focusedRowId ? mergeTaskRuntimeFields(detailRow, row) : row
            )
          );
        } catch (err) {
          console.error('Results fetch failed; keeping the lightweight row.', err);
        }
      })();
    }, 120);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [getProjectTaskDetailCached, location.search, project?.task_id, projectTasks, setProjectTasks, workflowKey, workspaceTab]);

  useEffect(() => {
    const templateContextTask = requestedStatusTaskRow || activeStatusTaskRow;
    if (!templateContextTask || !isDraftTaskSnapshot(templateContextTask)) return;
    rememberTemplatesForTaskRow(templateContextTask.id, proteinTemplates);
  }, [requestedStatusTaskRow, activeStatusTaskRow, proteinTemplates, rememberTemplatesForTaskRow]);

  const affinityMode = normalizeAffinityMode(draft?.inputConfig?.options?.affinityMode);

  const {
    normalizedDraftComponents,
    leadOptPrimary,
    leadOptChainContext,
    componentCompletion,
    hasIncompleteComponents,
    allowedConstraintTypes,
    isBondOnlyBackend
  } = useProjectWorkflowContext({
    draft,
    fallbackBackend: project?.backend || 'boltz',
    isPeptideDesignWorkflow
  });

  const componentTypeBuckets = useComponentTypeBuckets(normalizedDraftComponents);

  const constraintCount = draft?.inputConfig.constraints.length || 0;
  const {
    runtimeResultTask,
    activeResultTask,
    affinityUploadScopeTaskRowId,
    resultChainIds,
    resultChainShortLabelById,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    selectedResultLigandComponent,
    selectedResultLigandSequence,
    overviewPrimaryLigand,
    snapshotConfidence,
    resultChainConsistencyWarning,
    snapshotAffinity,
    snapshotLigandAtomPlddts,
    snapshotLigandResiduePlddts,
    snapshotLigandMeanPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotPlddt,
    snapshotSelectedPairIptm,
    snapshotIptm,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotIc50Um,
    snapshotPic50,
    snapshotPic50Mw,
    snapshotIc50Error,
    snapshotPlddtTone,
    snapshotIptmTone,
    snapshotIc50Tone,
    snapshotBindingTone,
  } = useResultSnapshot({
    project,
    projectTasks,
    draftProperties: draft?.inputConfig.properties,
    statusContextTaskRow,
    requestedStatusTaskRow,
    viewerTaskId: structureTaskId,
    normalizedDraftComponents,
    workflowKey,
    shouldComputeResultMetrics: workspaceTab === 'results',
    isDraftTaskSnapshot: (task) => isDraftTaskSnapshot(task ?? null),
  });

  useEffect(() => {
    if (workspaceTab !== 'results') return;
    const sourceRow = activeResultTask || null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    if (!sourceRowId || sourceRowId.startsWith('local-')) return;
    if (hasTaskInputSnapshotPayload(sourceRow)) return;

    const marker = [sourceRowId, String(sourceRow?.updated_at || '').trim(), String(sourceRow?.task_id || '').trim()].join('|');
    if (viewerResultHydrationRef.current[sourceRowId] === marker) return;

    let cancelled = false;
    void (async () => {
      try {
        const detailRow = await getProjectTaskDetailCached(sourceRowId, {
          includeComponents: true,
          includeConstraints: false,
          includeProperties: true,
          includeConfidence: true,
          includeAffinity: true,
          includeProteinSequence: true
        });
        if (cancelled || !detailRow) return;
        if (!hasTaskInputSnapshotPayload(detailRow)) return;
        viewerResultHydrationRef.current[sourceRowId] = marker;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === sourceRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
      } catch (err) {
        console.error('Detail hydration failed; keeping the current snapshot.', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [activeResultTask, getProjectTaskDetailCached, setProjectTasks, workspaceTab]);

  useEffect(() => {
    if (workflowKey !== 'peptide_design') return;
    if (workspaceTab !== 'results') return;

    const sourceRow = requestedStatusTaskRow || statusContextTaskRow || activeResultTask || null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    if (!sourceRowId || sourceRowId.startsWith('local-')) return;

    const sourceTaskState = String(sourceRow?.task_state || '').trim().toUpperCase();
    if (sourceTaskState !== 'SUCCESS') return;

    const missingPeptideCandidates = !hasPeptideCandidateRows(sourceRow?.confidence);
    const missingAffinity = !hasObjectContent(sourceRow?.affinity);
    if (!missingPeptideCandidates && !missingAffinity) return;

    const marker = [
      sourceRowId,
      String(sourceRow?.updated_at || '').trim(),
      missingPeptideCandidates ? 'peptide-candidates' : '',
      missingAffinity ? 'affinity' : ''
    ].join('|');
    if (peptideResultHydrationRef.current[sourceRowId] === marker) return;

    let cancelled = false;
    void (async () => {
      try {
        const detailRow = await getProjectTaskDetailCached(sourceRowId, {
          includeComponents: false,
          includeConstraints: false,
          includeProperties: true,
          includeConfidence: true,
          includeAffinity: true,
          includeProteinSequence: false
        });
        if (cancelled || !detailRow) return;
        if (!hasPeptideCandidateRows(detailRow.confidence) && !hasObjectContent(detailRow.affinity)) return;
        peptideResultHydrationRef.current[sourceRowId] = marker;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === sourceRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
      } catch (err) {
        console.error('Row update hydration failed; keeping current snapshot.', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    activeResultTask,
    getProjectTaskDetailCached,
    requestedStatusTaskRow,
    setProjectTasks,
    statusContextTaskRow,
    workflowKey,
    workspaceTab
  ]);
  const leadOptPersistedUploads = useMemo(() => {
    const draftUploadSource =
      normalizedDraftComponents.length > 0
        ? ({ components: normalizedDraftComponents } as { components: unknown })
        : null;
    const sourceCandidates = [
      requestedStatusTaskRow,
      statusContextTaskRow,
      activeResultTask,
      draftUploadSource
    ];
    const hasText = (value: unknown) => String(value || '').trim().length > 0;
    let sourceTask: { components?: unknown } | null = null;
    let bestScore = -1;
    for (const candidate of sourceCandidates) {
      const score = countLeadOptUploadPayloads(candidate);
      if (score <= bestScore) continue;
      sourceTask = candidate;
      bestScore = score;
      if (score >= 2) break;
    }
    if (!sourceTask) {
      return { target: null, ligand: null };
    }
    const uploads = readLeadOptUploadsFromComponents(sourceTask.components);
    return {
      target:
        hasText(uploads.target?.fileName) && hasText(uploads.target?.content)
          ? { ...uploads.target! }
          : null,
      ligand:
        hasText(uploads.ligand?.fileName) && hasText(uploads.ligand?.content)
          ? { ...uploads.ligand! }
          : null
    };
  }, [activeResultTask, normalizedDraftComponents, requestedStatusTaskRow, statusContextTaskRow]);
  const activeConstraintIndex = useMemo(() => {
    if (!draft || !activeConstraintId) return -1;
    return draft.inputConfig.constraints.findIndex((item) => item.id === activeConstraintId);
  }, [draft, activeConstraintId]);
  const selectedContactConstraintIdSet = useMemo(() => {
    return new Set(selectedContactConstraintIds);
  }, [selectedContactConstraintIds]);

  const {
    activeChainInfos,
    chainInfoById,
    ligandChainOptions,
    workspaceTargetOptions,
    selectedWorkspaceTarget,
    workspaceLigandSelectableOptions,
    selectedWorkspaceLigand,
    canEnableAffinityFromWorkspace,
    affinityEnableDisabledReason,
  } = useWorkspaceAffinitySelection({
    normalizedDraftComponents,
    draftProperties: draft?.inputConfig.properties,
    isPeptideDesignWorkflow,
  });
  const {
    constraintTemplateOptions,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    resolveTemplateComponentIdForConstraint,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    constraintSelectedAtomRefs
  } = useConstraintTemplateContext({
    draft,
    proteinTemplates,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    activeConstraintId,
    activeConstraintPickSlot: constraintPickSlot[activeConstraintId ?? ''] ?? 'first',
    selectedContactConstraintIds,
    chainInfoById,
    activeChainInfos
  });

  useProjectDraftSynchronizers({
    draft,
    setDraft,
    proteinTemplates,
    setProteinTemplates,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    updateConstraintPickSlot,
    activeComponentId,
    setActiveComponentId,
    workflowKey,
    isPeptideDesignWorkflow,
    selectedWorkspaceLigandChainId: selectedWorkspaceLigand.chainId,
    selectedWorkspaceTargetChainId: selectedWorkspaceTarget.chainId,
    canEnableAffinityFromWorkspace,
  });

  const loadProject = useProjectWorkspaceLoader({
    entryRoutingResolved,
    projectId,
    locationSearch: location.search,
    requestNewTask,
    sessionUserId: session?.userId,
    setLoading,
    setSaving,
    setSubmitting,
    setError,
    setProjectTasks,
    setWorkspaceTab,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setSavedAffinityUploadsFingerprint,
    setRunMenuOpen,
    setProteinTemplates,
    setCustomResidueLibrary,
    setTaskProteinTemplates,
    setTaskAffinityUploads,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    setSelectedConstraintTemplateComponentId,
    setPickedResidue,
    setProject,
  });

  useProjectWorkspaceRuntimeUi({
    project,
    backend: draft?.backend || project?.backend || '',
    workspaceTab,
    setWorkspaceTab,
    proteinTemplates,
    customResidueLibrary,
    taskProteinTemplates,
    taskAffinityUploads,
    activeConstraintId,
    selectedConstraintTemplateComponentId,
  });

  const {
    targetFile: affinityTargetFile,
    ligandFile: affinityLigandFile,
    ligandSmiles: affinityLigandSmiles,
    targetChainIds: affinityTargetChainIds,
    ligandChainId: affinityLigandChainId,
    preview: affinityPreview,
    previewTargetStructureText: affinityPreviewTargetStructureText,
    previewTargetStructureFormat: affinityPreviewTargetStructureFormat,
    previewLigandStructureText: affinityPreviewLigandStructureText,
    previewLigandStructureFormat: affinityPreviewLigandStructureFormat,
    previewLoading: affinityPreviewLoading,
    previewError: affinityPreviewError,
    isPreviewCurrent: affinityPreviewCurrent,
    hasLigand: affinityHasLigand,
    supportsActivity: affinitySupportsActivity,
    confidenceOnly: affinityConfidenceOnly,
    confidenceOnlyLocked: affinityConfidenceOnlyLocked,
    persistedUploads: affinityCurrentUploads,
    onTargetFileChange: onAffinityTargetFileChange,
    onLigandFileChange: onAffinityLigandFileChange,
    onConfidenceOnlyChange: onAffinityConfidenceOnlyChange,
    setLigandSmiles: setAffinityLigandSmiles,
    onAffinityModeChange,
    affinityDockPocket,
    onAffinityDockPocketChange,
    affinityDockBlind,
    onAffinityDockBlindChange
  } = useProjectAffinityWorkspace({
    isAffinityWorkflow,
    workspaceTab,
    projectId: project?.id || null,
    draft,
    affinityMode,
    setDraft,
    affinityUploadScopeTaskRowId,
    taskAffinityUploads,
    requestedStatusTaskRow,
    statusContextTaskRow,
    activeResultTask,
    computeUseMsaFlag,
    rememberAffinityUploadsForTaskRow
  });

  const { metadataOnlyDraftDirty, hasUnsavedChanges } = useProjectDirtyState({
    draft,
    proteinTemplates,
    affinityUploads: affinityCurrentUploads,
    savedDraftFingerprint,
    savedComputationFingerprint,
    savedTemplateFingerprint,
    savedAffinityUploadsFingerprint,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    createAffinityUploadsFingerprint
  });

  const {
    patch,
    patchTask,
    resolveEditableDraftTaskRowId,
    persistDraftTaskSnapshot,
    saveDraft,
    pullResultForViewer,
    refreshStatus
  } = useProjectTaskActions({
    project,
    projectTasks,
    draft,
    requestNewTask,
    locationSearch: location.search,
    workspaceTab,
    metadataOnlyDraftDirty,
    affinityLigandSmiles,
    affinityPreviewLigandSmiles: String(affinityPreview?.ligandSmiles || ''),
    affinityTargetFile,
    affinityLigandFile,
    affinityCurrentUploads,
    proteinTemplates,
    customResidueLibrary,
    statusRefreshInFlightRef,
    insertProjectTask,
    updateProject,
    updateProjectTask,
    sortProjectTasks,
    isDraftTaskSnapshot,
    normalizeConfigForBackend,
    nonEmptyComponents,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    createAffinityUploadsFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    setProject,
    setProjectTasks,
    setDraft: (value) => setDraft(value),
    setSaving,
    setError,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setSavedAffinityUploadsFingerprint,
    setRunMenuOpen,
    navigate,
    setStructureText,
    setStructureFormat,
    setStructureTaskId,
    setResultError,
    setStatusInfo
  });

  useEffect(() => {
    if (workflowKey !== 'lead_optimization') return;
    if (workspaceTab !== 'results') return;

    const sourceRow = requestedStatusTaskRow || activeResultTask || statusContextTaskRow || null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    const sourceTaskId = String(sourceRow?.task_id || '').trim();
    const sourceTaskState = String(sourceRow?.task_state || '').trim().toUpperCase();
    if (!sourceRowId || !sourceTaskId || sourceTaskState !== 'SUCCESS') return;
    if (hasLeadOptResultSummaryPayload(sourceRow) && readLeadOptQueryIdFromRow(sourceRow)) return;

    const marker = [sourceRowId, sourceTaskId, String(sourceRow?.updated_at || '').trim()].join('|');
    if (leadOptResultMaterializationRef.current[sourceRowId] === marker) return;
    leadOptResultMaterializationRef.current[sourceRowId] = marker;

    void refreshStatus({ silent: true, taskId: sourceTaskId });
  }, [activeResultTask, refreshStatus, requestedStatusTaskRow, statusContextTaskRow, workflowKey, workspaceTab]);

  useEffect(() => {
    if (workflowKey !== 'lead_optimization') return;
    const sourceRow = requestedStatusTaskRow || activeResultTask || statusContextTaskRow || null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    const sourceTaskId = String(sourceRow?.task_id || '').trim();
    const sourceTaskState = String(sourceRow?.task_state || '').trim().toUpperCase();
    if (!sourceRowId || !sourceTaskId) return;
    if (sourceTaskState !== 'SUCCESS' && sourceTaskState !== 'FAILURE' && sourceTaskState !== 'REVOKED') return;
    if (!isTransientRuntimeStatusText(sourceRow?.status_text)) return;

    const marker = ['terminal-status', sourceRowId, sourceTaskId, String(sourceRow?.updated_at || '').trim()].join('|');
    if (leadOptResultMaterializationRef.current[sourceRowId] === marker) return;
    leadOptResultMaterializationRef.current[sourceRowId] = marker;

    void refreshStatus({ silent: true, taskId: sourceTaskId });
  }, [activeResultTask, refreshStatus, requestedStatusTaskRow, statusContextTaskRow, workflowKey]);

  const showRunQueuedNotice = (message: string) => {
    showRunQueuedNoticeControl({
      message,
      runSuccessNoticeTimerRef,
      setRunSuccessNotice
    });
  };

  const syncWorkspaceTaskRow = useCallback(
    (taskRowId: string) => {
      const normalizedTaskRowId = String(taskRowId || '').trim();
      if (!projectId || !normalizedTaskRowId) return;
      const query = new URLSearchParams(location.search);
      if (query.get('task_row_id') === normalizedTaskRowId && query.get('new_task') !== '1') return;
      query.delete('new_task');
      query.delete('source_task_row_id');
      query.set('tab', workspaceTab);
      query.set('task_row_id', normalizedTaskRowId);
      navigate(`/projects/${projectId}?${query.toString()}`, { replace: true });
    },
    [location.search, navigate, projectId, workspaceTab]
  );

  // latest draft via ref: submitTask may run before setDraft's re-render commits
  const draftRef = useRef(draft);
  draftRef.current = draft;

  const { submitAffinityTask, submitPredictionTask } = createWorkflowSubmitters({
    sessionEmail: session?.email ?? null,
    project,
    draftRef,
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    workspaceTab,
    affinityTargetFile,
    affinityLigandFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreview,
    affinityPreviewError: String(affinityPreviewError || ''),
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityCurrentUploads,
    proteinTemplates,
    submitInFlightRef,
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    setWorkspaceTab,
    setSubmitting,
    setError,
    setRunRedirectTaskId,
    setRunSuccessNotice,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setSavedAffinityUploadsFingerprint,
    setRunMenuOpen,
    syncWorkspaceTaskRow,
    setProjectTasks,
    setProject,
    setStatusInfo,
    showRunQueuedNotice,
    normalizeConfigForBackend,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    createAffinityUploadsFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    rememberAffinityUploadsForTaskRow,
    rememberTemplatesForTaskRow,
    patch,
    patchTask,
    updateProjectTask,
    findProjectTaskByTaskId,
    deleteProjectTask,
    sortProjectTasks,
    saveProjectInputConfig,
    listIncompleteComponentOrders: (components: InputComponent[]) =>
      listIncompleteComponentOrders(components, {
        ignoreEmptyLigand: isPeptideDesignWorkflow
      }),
    validateComponents
  });

  const submitTask = async (notifyEmailOverride?: string) => {
    await submitTaskByWorkflow({
      notifyEmailOverride,
      project,
      draft: draftRef.current,
      submitInFlightRef,
      workflowKey,
      getWorkflowDefinition,
      setError,
      submitAffinityTask,
      submitPredictionTask
    });
  };

  useProjectRuntimeEffects({
    projectTaskId: statusContextTaskRow?.task_id || project?.task_id || null,
    projectTaskState: displayTaskState || project?.task_state || null,
    refreshStatus,
    statusContextTaskRow,
    runtimeResultTask,
    activeResultTask,
    structureTaskId,
    pullResultForViewer,
    isPeptideDesignWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    activeConstraintId,
    selectedContactConstraintIdsLength: selectedContactConstraintIds.length,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef
  });

  useProjectRunUiEffects({
    runRedirectTaskId,
    projectId: project?.id || null,
    navigate: (to: string) => navigate(to),
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    runMenuOpen,
    hasUnsavedChanges,
    submitting,
    saving,
    setRunMenuOpen,
    runActionRef,
    isPredictionWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    topRunButtonRef,
    setShowFloatingRunButton
  });

  const { confidenceBackend, projectBackend, hasProtenixConfidenceSignals, hasAf3ConfidenceSignals } =
    useProjectConfidenceSignals({
      snapshotConfidence: snapshotConfidence || null,
      projectBackendValue: project?.backend || null,
      draft,
      setDraft
    });

  return {
    ...local,
    projectId,
    locationSearch: location.search,
    navigate,
    hasExplicitWorkspaceQuery,
    requestNewTask,
    entryRoutingResolved,
    canEdit,
    workflowKey,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    displaySubmittedAt,
    progressPercent,
    isActiveRuntime,
    totalRuntimeSeconds,
    normalizedDraftComponents,
    leadOptPrimary,
    leadOptChainContext,
    leadOptPersistedUploads,
    componentCompletion,
    hasIncompleteComponents,
    allowedConstraintTypes,
    isBondOnlyBackend,
    componentTypeBuckets,
    constraintCount,
    runtimeResultTask,
    activeResultTask,
    affinityUploadScopeTaskRowId,
    resultChainIds,
    resultChainShortLabelById,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    selectedResultLigandComponent,
    selectedResultLigandSequence,
    overviewPrimaryLigand,
    snapshotConfidence,
    resultChainConsistencyWarning,
    snapshotAffinity,
    snapshotLigandAtomPlddts,
    snapshotLigandResiduePlddts,
    snapshotLigandMeanPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotPlddt,
    snapshotSelectedPairIptm,
    snapshotIptm,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotIc50Um,
    snapshotPic50,
    snapshotPic50Mw,
    snapshotIc50Error,
    snapshotPlddtTone,
    snapshotIptmTone,
    snapshotIc50Tone,
    snapshotBindingTone,
    activeConstraintIndex,
    selectedContactConstraintIdSet,
    activeChainInfos,
    chainInfoById,
    ligandChainOptions,
    workspaceTargetOptions,
    selectedWorkspaceTarget,
    workspaceLigandSelectableOptions,
    selectedWorkspaceLigand,
    canEnableAffinityFromWorkspace,
    affinityEnableDisabledReason,
    constraintTemplateOptions,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    resolveTemplateComponentIdForConstraint,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    constraintSelectedAtomRefs,
    loadProject,
    affinityTargetFile,
    affinityLigandFile,
    affinityLigandSmiles,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityPreview,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    affinityPreviewLoading,
    affinityPreviewError,
    affinityPreviewCurrent,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    affinityCurrentUploads,
    affinityMode,
    affinityDockPocket,
    affinityDockBlind,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    onAffinityModeChange,
    onAffinityDockPocketChange,
    onAffinityDockBlindChange,
    metadataOnlyDraftDirty,
    hasUnsavedChanges,
    patch,
    patchTask,
    resolveEditableDraftTaskRowId,
    persistDraftTaskSnapshot,
    saveDraft,
    pullResultForViewer,
    refreshStatus,
    submitAffinityTask,
    submitPredictionTask,
    submitTask,
    confidenceBackend,
    projectBackend,
    hasProtenixConfidenceSignals,
    hasAf3ConfidenceSignals
  };
}
