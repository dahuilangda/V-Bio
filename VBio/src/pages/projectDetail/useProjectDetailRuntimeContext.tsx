import { useEffect, useMemo } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import type { InputComponent } from '../../types/models';
import { useAuth } from '../../hooks/useAuth';
import {
  insertProjectTask,
  listProjectTasksCompact,
  listProjectTasksForList,
  updateProject,
  updateProjectTask
} from '../../api/supabaseLite';
import { saveProjectInputConfig } from '../../utils/projectInputs';
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
  readTaskLeadOptUploads,
} from './projectTaskSnapshot';
import {
  computeUseMsaFlag,
  createComputationFingerprint,
  createDraftFingerprint,
  createProteinTemplatesFingerprint,
  listIncompleteComponentOrders,
  nonEmptyComponents,
  normalizeConfigForBackend,
  sortProjectTasks
} from './projectDraftUtils';
import { useResultSnapshot } from './useResultSnapshot';
import { useProjectRunUiEffects } from './useProjectRunUiEffects';
import { useProjectRuntimeEffects } from './useProjectRuntimeEffects';
import { useProjectTaskStatusContext } from './useProjectTaskStatusContext';
import { useProjectWorkflowContext } from './useProjectWorkflowContext';
import { useConstraintTemplateContext } from './useConstraintTemplateContext';
import { useWorkspaceAffinitySelection } from './useWorkspaceAffinitySelection';
import { useProjectDraftSynchronizers } from './useProjectDraftSynchronizers';
import { useProjectWorkspaceRuntimeUi } from './useProjectWorkspaceRuntimeUi';
import { useProjectWorkspaceLoader } from './useProjectWorkspaceLoader';
import {
  showRunQueuedNotice as showRunQueuedNoticeControl,
  submitTaskByWorkflow
} from './runControls';
import { useProjectDirtyState } from './useProjectDirtyState';
import { useProjectConfidenceSignals } from './useProjectConfidenceSignals';
import { useComponentTypeBuckets } from './useComponentTypeBuckets';
import { useProjectDetailLocalState } from './useProjectDetailLocalState';

function buildTaskRuntimeSignature(
  rows: Array<{
    id?: string | null;
    task_id?: string | null;
    task_state?: string | null;
    status_text?: string | null;
    error_text?: string | null;
    updated_at?: string | null;
    completed_at?: string | null;
    duration_seconds?: number | null;
  }>
): string {
  return rows
    .map(
      (row) =>
        `${String(row.id || '').trim()}|${String(row.task_id || '').trim()}|${String(row.task_state || '').trim()}|${String(row.status_text || '').trim()}|${String(row.error_text || '').trim()}|${String(row.updated_at || '').trim()}|${String(row.completed_at || '').trim()}|${Number.isFinite(Number(row.duration_seconds)) ? Number(row.duration_seconds) : ''}`
    )
    .join('\n');
}

const TASK_STATE_PRIORITY: Record<string, number> = {
  DRAFT: 0,
  QUEUED: 1,
  RUNNING: 2,
  SUCCESS: 3,
  FAILURE: 3,
  REVOKED: 3,
};

function taskStatePriority(value: unknown): number {
  return TASK_STATE_PRIORITY[String(value || '').trim().toUpperCase()] ?? 0;
}

function mergeTaskRuntimeFields<
  T extends {
    task_id?: string | null;
    task_state?: string | null;
    status_text?: string | null;
    error_text?: string | null;
    completed_at?: string | null;
    duration_seconds?: number | null;
  },
  U extends {
  task_id?: string | null;
  task_state?: string | null;
  status_text?: string | null;
  error_text?: string | null;
  completed_at?: string | null;
  duration_seconds?: number | null;
}
>(next: T, prev: U): T {
  const nextTaskId = String(next.task_id || '').trim();
  const prevTaskId = String(prev.task_id || '').trim();
  if (!nextTaskId || !prevTaskId || nextTaskId !== prevTaskId) return next;
  const nextPriority = taskStatePriority(next.task_state);
  const prevPriority = taskStatePriority(prev.task_state);
  if (prevPriority < nextPriority) return next;
  if (prevPriority > nextPriority) {
    return {
      ...next,
      task_state: prev.task_state,
      status_text: prev.status_text,
      error_text: prev.error_text,
      completed_at: prev.completed_at || next.completed_at,
      duration_seconds: prev.duration_seconds ?? next.duration_seconds
    };
  }
  return {
    ...next,
    completed_at: next.completed_at || prev.completed_at,
    duration_seconds: next.duration_seconds ?? prev.duration_seconds,
    status_text: String(next.status_text || '').trim() || prev.status_text,
    error_text: String(next.error_text || '').trim() || prev.error_text
  };
}

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

  const local = useProjectDetailLocalState();
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
    structureText,
    setStructureText,
    setStructureFormat,
    structureTaskId,
    setStructureTaskId,
    statusInfo,
    setStatusInfo,
    nowTs,
    setNowTs,
    workspaceTab,
    setWorkspaceTab,
    savedDraftFingerprint,
    setSavedDraftFingerprint,
    savedComputationFingerprint,
    setSavedComputationFingerprint,
    savedTemplateFingerprint,
    setSavedTemplateFingerprint,
    runMenuOpen,
    setRunMenuOpen,
    proteinTemplates,
    setProteinTemplates,
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
    constraintPickModeEnabled,
    constraintPickSlotRef,
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
  const entryRoutingResolved = useEntryRoutingResolution({
    projectId,
    hasExplicitWorkspaceQuery,
    navigate,
    listProjectTasksCompact,
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

  const canEdit = useMemo(() => {
    if (!project || !session) return false;
    return project.user_id === session.userId;
  }, [project, session]);
  const workflowKey = useMemo(() => getWorkflowDefinition(project?.task_type).key, [project?.task_type]);
  const isPredictionWorkflow = isPredictionLikeWorkflowKey(workflowKey);
  const isPeptideDesignWorkflow = workflowKey === 'peptide_design';
  const isAffinityWorkflow = workflowKey === 'affinity';
  const isLeadOptimizationWorkflow = workflowKey === 'lead_optimization';
  const runtimeTaskSignature = useMemo(() => buildTaskRuntimeSignature(projectTasks), [projectTasks]);

  useEffect(() => {
    const projectIdValue = String(project?.id || '').trim();
    if (!projectIdValue) return;
    const hasRuntimeTasks = projectTasks.some((row) => {
      const taskId = String(row.task_id || '').trim();
      const taskState = String(row.task_state || '').toUpperCase();
      if (!taskId) return false;
      return taskState === 'QUEUED' || taskState === 'RUNNING';
    });
    if (!hasRuntimeTasks) return;

    let cancelled = false;
    let inFlight = false;

    const refreshTaskRows = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const shouldUseTaskListView = workflowKey === 'lead_optimization' || workflowKey === 'peptide_design';
        const rowsRaw =
          shouldUseTaskListView
            ? await listProjectTasksForList(projectIdValue, { includeComponents: false })
            : await listProjectTasksCompact(projectIdValue);
        if (cancelled) return;
        const nextRows = sortProjectTasks(rowsRaw);
        setProjectTasks((prev) => {
          const mergedRows = nextRows.map((row) => {
            const prevRow = prev.find((item) => item.id === row.id);
            if (!prevRow) return row;
            return mergeTaskRuntimeFields(row, prevRow);
          });
          return buildTaskRuntimeSignature(prev) === buildTaskRuntimeSignature(mergedRows) ? prev : mergedRows;
        });

        const activeTaskId = String(project?.task_id || '').trim();
        if (!activeTaskId) return;
        const activeRow = nextRows.find((row) => String(row.task_id || '').trim() === activeTaskId) || null;
        if (!activeRow) return;
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
      } catch {
        // keep local state and retry on next cycle
      } finally {
        inFlight = false;
      }
    };

    void refreshTaskRows();
    const timer = window.setInterval(() => {
      void refreshTaskRows();
    }, 4200);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [project?.id, project?.task_id, projectTasks, runtimeTaskSignature, setProject, setProjectTasks, workflowKey]);

  const {
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    progressPercent,
    waitingSeconds,
    isActiveRuntime,
    totalRuntimeSeconds
  } = useProjectTaskStatusContext({
    project,
    projectTasks,
    locationSearch: location.search,
    statusInfo,
    nowTs
  });

  useEffect(() => {
    const templateContextTask = requestedStatusTaskRow || activeStatusTaskRow;
    if (!templateContextTask || !isDraftTaskSnapshot(templateContextTask)) return;
    rememberTemplatesForTaskRow(templateContextTask.id, proteinTemplates);
  }, [requestedStatusTaskRow, activeStatusTaskRow, proteinTemplates, rememberTemplatesForTaskRow]);

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
    normalizedDraftComponents,
    workflowKey,
    isDraftTaskSnapshot: (task) => isDraftTaskSnapshot(task ?? null),
  });
  const leadOptPersistedUploads = useMemo(() => {
    const sourceTask = statusContextTaskRow || activeResultTask || requestedStatusTaskRow || null;
    return readTaskLeadOptUploads(sourceTask);
  }, [statusContextTaskRow, activeResultTask, requestedStatusTaskRow]);
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
    constraintViewerActiveResidue
  } = useConstraintTemplateContext({
    draft,
    proteinTemplates,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    activeConstraintId,
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
    constraintPickModeEnabled,
    constraintPickSlotRef,
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
    setError,
    setProjectTasks,
    setWorkspaceTab,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setRunMenuOpen,
    setProteinTemplates,
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
    workspaceTab,
    setWorkspaceTab,
    setNowTs,
    proteinTemplates,
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
    onAffinityUseMsaChange
  } = useProjectAffinityWorkspace({
    isAffinityWorkflow,
    workspaceTab,
    projectId: project?.id || null,
    draft,
    setDraft,
    affinityUploadScopeTaskRowId,
    taskAffinityUploads,
    statusContextTaskRow,
    activeResultTask,
    computeUseMsaFlag,
    rememberAffinityUploadsForTaskRow
  });

  const { metadataOnlyDraftDirty, hasUnsavedChanges } = useProjectDirtyState({
    draft,
    proteinTemplates,
    savedDraftFingerprint,
    savedComputationFingerprint,
    savedTemplateFingerprint,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint
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
    requestedStatusTaskRowId: requestedStatusTaskRow?.id || null,
    activeStatusTaskRowId: activeStatusTaskRow?.id || null,
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
    setRunMenuOpen,
    navigate,
    setStructureText,
    setStructureFormat,
    setStructureTaskId,
    setResultError,
    setStatusInfo
  });

  const showRunQueuedNotice = (message: string) => {
    showRunQueuedNoticeControl({
      message,
      runSuccessNoticeTimerRef,
      setRunSuccessNotice
    });
  };

  const { submitAffinityTask, submitPredictionTask } = createWorkflowSubmitters({
    project,
    draft,
    isPeptideDesignWorkflow,
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
    setRunMenuOpen,
    setProjectTasks,
    setProject,
    setStatusInfo,
    showRunQueuedNotice,
    normalizeConfigForBackend,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    rememberAffinityUploadsForTaskRow,
    rememberTemplatesForTaskRow,
    patch,
    patchTask,
    updateProjectTask,
    sortProjectTasks,
    saveProjectInputConfig,
    listIncompleteComponentOrders: (components: InputComponent[]) =>
      listIncompleteComponentOrders(components, {
        ignoreEmptyLigand: isPeptideDesignWorkflow
      }),
    validateComponents
  });

  const submitTask = async () => {
    await submitTaskByWorkflow({
      project,
      draft,
      submitInFlightRef,
      workflowKey,
      getWorkflowDefinition,
      setError,
      submitAffinityTask,
      submitPredictionTask
    });
  };

  useProjectRuntimeEffects({
    projectTaskId: project?.task_id || null,
    projectTaskState: project?.task_state || null,
    projectTasksDependency: projectTasks,
    refreshStatus,
    statusContextTaskRow,
    runtimeResultTask,
    structureTaskId,
    structureText,
    pullResultForViewer,
    isPeptideDesignWorkflow,
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
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    progressPercent,
    waitingSeconds,
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
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    onAffinityUseMsaChange,
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
